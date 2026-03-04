"""
Live CDC simulator.

The Simulator runs a continuous loop that generates realistic OLTP traffic:
inserts (new customers, new orders), updates (order status transitions,
payment completions, shipment tracking), and occasional deletes (test record
cleanup). Every write goes through psycopg2 so it lands in the PostgreSQL
WAL (Write-Ahead Log) and DMS picks it up as a CDC event.

Tick model:
    Each "tick" is one iteration of the main loop. Every tick:
    1. Place N new orders (with items and payment).
    2. Advance ~20% of pending/confirmed/processing/shipped orders by one status.
    3. Update shipment delivery status for in-transit shipments.
    4. Occasionally (1% chance per tick) cancel a pending order.
    5. Occasionally (0.5% chance per tick) process a refund.
    6. Sleep for TICK_INTERVAL_SECONDS.

    The probabilities are chosen so that a realistic mix of events flows
    through DMS at a steady pace without flooding the database.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone

from simulator.config import (
    CANCELLATION_RATE,
    REFUND_RATE,
    DeliveryStatus,
    OrderStatus,
    PaymentStatus,
    SimulationConfig,
)
from simulator.db import DatabaseManager
from simulator.models import (
    Customer,
    Order,
    OrderItem,
    Payment,
    Shipment,
)

from faker import Faker

logger = logging.getLogger(__name__)

# Fraction of in-progress orders to advance per tick.
# 0.2 = 20% of pending/confirmed/processing/shipped orders move forward each tick.
_ADVANCE_RATE: float = 0.20

# Probability per tick that a random pending order gets cancelled.
_CANCEL_RATE_PER_TICK: float = 0.01

# Probability per tick that a random delivered order gets a refund request.
_REFUND_RATE_PER_TICK: float = 0.005


class Simulator:
    """
    Drives continuous OLTP activity against the PostgreSQL database.

    Usage:
        sim = Simulator(db, sim_config)
        sim.run()        # blocks forever — use Ctrl+C to stop
    """

    def __init__(self, db: DatabaseManager, config: SimulationConfig) -> None:
        self._db = db
        self._config = config
        self._fake = Faker()
        self._tick_count = 0

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Start the simulation loop.

        Runs until interrupted (KeyboardInterrupt / SIGINT).
        Logs a summary line every 10 ticks.
        """
        logger.info(
            "Simulator starting: %d new orders/tick, %.1fs tick interval",
            self._config.new_orders_per_tick,
            self._config.tick_interval_seconds,
        )
        try:
            while True:
                self._tick()
                time.sleep(self._config.tick_interval_seconds)
        except KeyboardInterrupt:
            logger.info("Simulator stopped after %d ticks", self._tick_count)

    # ── Tick ──────────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        self._tick_count += 1

        # 1. Place new orders
        for _ in range(self._config.new_orders_per_tick):
            self._place_new_order()

        # 2. Advance in-progress orders
        self._advance_orders()

        # 3. Update shipment statuses
        self._advance_shipments()

        # 4. Randomly cancel a pending order
        if random.random() < _CANCEL_RATE_PER_TICK:
            self._cancel_random_pending_order()

        # 5. Randomly process a refund
        if random.random() < _REFUND_RATE_PER_TICK:
            self._refund_random_delivered_order()

        # 6. Occasionally add a brand-new customer (organic growth)
        if random.random() < 0.15:
            self._add_new_customer()

        if self._tick_count % 10 == 0:
            self._log_stats()

    # ── New order flow ────────────────────────────────────────────────────────

    def _place_new_order(self) -> None:
        """
        Select a random customer, create an order with 1-4 items, and
        immediately attach a payment. The order starts in PENDING status —
        subsequent ticks will advance it through the lifecycle.
        """
        customer_ids = self._db.fetch_column("SELECT customer_id FROM customers ORDER BY random() LIMIT 50")
        if not customer_ids:
            logger.warning("No customers found — skipping new order")
            return
        customer_id = random.choice(customer_ids)

        # Pick 1-4 random products
        product_rows = self._db.fetch_all(
            "SELECT product_id, unit_price FROM products WHERE stock_qty > 0 ORDER BY random() LIMIT 4"
        )
        if not product_rows:
            logger.warning("No products with stock — skipping new order")
            return

        # Insert the order
        now = datetime.now(tz=timezone.utc)
        row = self._db.fetch_one(
            "INSERT INTO orders (customer_id, order_date, order_status) VALUES (%s, %s, %s) RETURNING order_id",
            (customer_id, now, OrderStatus.PENDING),
        )
        if row is None:
            return
        order_id: int = row[0]

        # Insert items and tally total
        order_total = 0.0
        for product_id, unit_price in product_rows:
            item = OrderItem.generate(order_id, product_id, float(unit_price))
            self._db.execute(
                "INSERT INTO order_items (order_id, product_id, quantity, unit_price, line_total) VALUES (%s,%s,%s,%s,%s)",
                item.as_insert_tuple(),
            )
            order_total += item.line_total
            # Decrement stock
            self._db.execute(
                "UPDATE products SET stock_qty = GREATEST(stock_qty - %s, 0) WHERE product_id = %s",
                (item.quantity, product_id),
            )

        # Insert payment (immediately completed — simulates online payment at checkout)
        payment = Payment.generate(
            order_id=order_id,
            amount=round(order_total, 2),
            payment_date=now,
            status=PaymentStatus.COMPLETED,
        )
        self._db.execute(
            "INSERT INTO payments (order_id, method, amount, status, payment_date) VALUES (%s,%s,%s,%s,%s)",
            payment.as_insert_tuple(),
        )

        logger.debug("New order %d placed for customer %d (total: %.2f)", order_id, customer_id, order_total)

    # ── Status advancement ────────────────────────────────────────────────────

    def _advance_orders(self) -> None:
        """
        Advance a fraction of non-terminal orders by one lifecycle step.

        This generates UPDATE events on the orders table, which DMS captures
        as change records.
        """
        # Fetch in-progress order IDs (not terminal)
        rows = self._db.fetch_all(
            """
            SELECT order_id, order_status
            FROM orders
            WHERE order_status NOT IN ('delivered', 'cancelled', 'refunded')
            ORDER BY random()
            LIMIT 100
            """
        )
        if not rows:
            return

        # Advance a fraction of them
        sample_size = max(1, int(len(rows) * _ADVANCE_RATE))
        to_advance = random.sample(rows, min(sample_size, len(rows)))

        now = datetime.now(tz=timezone.utc)
        for order_id, current_status in to_advance:
            next_status = self._next_order_status(current_status)
            if next_status == current_status:
                continue

            self._db.execute(
                "UPDATE orders SET order_status = %s WHERE order_id = %s",
                (next_status, order_id),
            )

            # When order moves to SHIPPED, create a shipment record
            if next_status == OrderStatus.SHIPPED:
                self._create_shipment(order_id, now)

            logger.debug("Order %d: %s → %s", order_id, current_status, next_status)

    def _next_order_status(self, current: str) -> str:
        """Return the next status in the lifecycle, or the same if already at end."""
        try:
            idx = OrderStatus.LIFECYCLE.index(current)
        except ValueError:
            return current
        if idx + 1 < len(OrderStatus.LIFECYCLE):
            return OrderStatus.LIFECYCLE[idx + 1]
        return current

    def _create_shipment(self, order_id: int, shipped_date: datetime) -> None:
        """Create a new shipment record when an order is first shipped."""
        shipment = Shipment.generate(
            order_id=order_id,
            shipped_date=shipped_date,
            delivery_status=DeliveryStatus.IN_TRANSIT,
        )
        self._db.execute(
            "INSERT INTO shipments (order_id, carrier, delivery_status, shipped_date, delivered_date) VALUES (%s,%s,%s,%s,%s)",
            shipment.as_insert_tuple(),
        )

    # ── Shipment advancement ──────────────────────────────────────────────────

    def _advance_shipments(self) -> None:
        """
        Advance a fraction of non-terminal shipments by one delivery step.

        Generates UPDATE events on the shipments table.
        """
        rows = self._db.fetch_all(
            """
            SELECT shipment_id, delivery_status
            FROM shipments
            WHERE delivery_status NOT IN ('delivered', 'failed_delivery', 'returned')
            ORDER BY random()
            LIMIT 50
            """
        )
        if not rows:
            return

        sample_size = max(1, int(len(rows) * _ADVANCE_RATE))
        to_advance = random.sample(rows, min(sample_size, len(rows)))

        now = datetime.now(tz=timezone.utc)
        for shipment_id, current_status in to_advance:
            next_status = self._next_delivery_status(current_status)
            if next_status == current_status:
                continue

            if next_status == DeliveryStatus.DELIVERED:
                self._db.execute(
                    "UPDATE shipments SET delivery_status = %s, delivered_date = %s WHERE shipment_id = %s",
                    (next_status, now, shipment_id),
                )
            else:
                self._db.execute(
                    "UPDATE shipments SET delivery_status = %s WHERE shipment_id = %s",
                    (next_status, shipment_id),
                )

            logger.debug("Shipment %d: %s → %s", shipment_id, current_status, next_status)

    def _next_delivery_status(self, current: str) -> str:
        """Return the next delivery status in the lifecycle."""
        try:
            idx = DeliveryStatus.LIFECYCLE.index(current)
        except ValueError:
            return current
        if idx + 1 < len(DeliveryStatus.LIFECYCLE):
            return DeliveryStatus.LIFECYCLE[idx + 1]
        return current

    # ── Cancellation and refund ───────────────────────────────────────────────

    def _cancel_random_pending_order(self) -> None:
        """Cancel one random pending or confirmed order."""
        row = self._db.fetch_one(
            """
            SELECT order_id FROM orders
            WHERE order_status IN ('pending', 'confirmed')
            ORDER BY random()
            LIMIT 1
            """
        )
        if row is None:
            return
        order_id = row[0]
        self._db.execute(
            "UPDATE orders SET order_status = %s WHERE order_id = %s",
            (OrderStatus.CANCELLED, order_id),
        )
        # Refund any payment
        self._db.execute(
            "UPDATE payments SET status = %s WHERE order_id = %s AND status = 'completed'",
            (PaymentStatus.REFUNDED, order_id),
        )
        logger.debug("Order %d cancelled", order_id)

    def _refund_random_delivered_order(self) -> None:
        """Mark a random delivered order as refunded."""
        row = self._db.fetch_one(
            """
            SELECT order_id FROM orders
            WHERE order_status = 'delivered'
            ORDER BY random()
            LIMIT 1
            """
        )
        if row is None:
            return
        order_id = row[0]
        self._db.execute(
            "UPDATE orders SET order_status = %s WHERE order_id = %s",
            (OrderStatus.REFUNDED, order_id),
        )
        self._db.execute(
            "UPDATE payments SET status = %s WHERE order_id = %s",
            (PaymentStatus.REFUNDED, order_id),
        )
        self._db.execute(
            "UPDATE shipments SET delivery_status = %s WHERE order_id = %s",
            (DeliveryStatus.RETURNED, order_id),
        )
        logger.debug("Order %d refunded", order_id)

    # ── Customer growth ───────────────────────────────────────────────────────

    def _add_new_customer(self) -> None:
        """Insert a new customer record — simulates organic sign-ups."""
        customer = Customer.generate(self._fake)
        self._db.execute(
            "INSERT INTO customers (first_name, last_name, email, country, phone, signup_date) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (email) DO NOTHING",
            customer.as_insert_tuple(),
        )

    # ── Stats ─────────────────────────────────────────────────────────────────

    def _log_stats(self) -> None:
        """Log a snapshot of table sizes every 10 ticks."""
        row = self._db.fetch_one(
            """
            SELECT
                (SELECT COUNT(*) FROM customers)   AS customers,
                (SELECT COUNT(*) FROM orders)       AS orders,
                (SELECT COUNT(*) FROM order_items)  AS items,
                (SELECT COUNT(*) FROM payments)     AS payments,
                (SELECT COUNT(*) FROM shipments)    AS shipments
            """
        )
        if row:
            customers, orders, items, payments, shipments = row
            logger.info(
                "[tick %d] customers=%d  orders=%d  items=%d  payments=%d  shipments=%d",
                self._tick_count,
                customers, orders, items, payments, shipments,
            )
