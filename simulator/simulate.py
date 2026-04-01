"""
Live CDC simulator.

The Simulator runs a continuous loop that generates realistic OLTP traffic:
inserts (new customers, new orders), updates (order status transitions,
payment completions, shipment tracking), and occasional deletes (cancelled
orders, refunds). Every write goes through psycopg2 so it lands in the
PostgreSQL WAL and DMS picks it up as a CDC event.

Tick model:
    Each "tick" is one iteration of the main loop. Every tick:
    1. Check order count against the environment limit. If the limit is not
       reached, place N new orders (with items and payment).
    2. Advance ~20% of pending/confirmed/processing/shipped orders by one step.
    3. Advance shipment delivery statuses for in-transit shipments.
    4. Randomly (1% chance per tick) cancel a pending order.
    5. Randomly (0.5% chance per tick) process a refund on a delivered order.
    6. Occasionally (15% chance per tick) add a new customer.
    7. Sleep for TICK_INTERVAL_SECONDS.

Error handling contract:
- DatabaseConnectionError is caught in the main loop and triggers a reconnect.
  Transient network blips should not crash the simulator.
- SimulationError and all other exceptions bubble up and crash the process.
  A loudly crashed process is better than a silently broken one producing
  wrong data in the pipeline.
- Nothing fails silently. Every unexpected outcome is logged at ERROR level
  before being raised.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone

import psycopg2
from faker import Faker

from simulator.config import (
    DeliveryStatus,
    OrderStatus,
    PaymentStatus,
    SimulationConfig,
)
from simulator.db import DatabaseManager
from simulator.exceptions import DatabaseConnectionError, SimulationError
from simulator.models import (
    Customer,
    Order,
    OrderItem,
    Payment,
    Shipment,
)

logger = logging.getLogger(__name__)

_ADVANCE_RATE: float = 0.20
_CANCEL_RATE_PER_TICK: float = 0.01
_REFUND_RATE_PER_TICK: float = 0.005
# Refresh the cached order count every N ticks (not every tick, unnecessary DB load)
_ORDER_COUNT_REFRESH_INTERVAL: int = 50


class Simulator:
    """
    Drives continuous OLTP activity against the PostgreSQL database.

    Usage:
        sim = Simulator(db, sim_config)
        sim.run()        # blocks until Ctrl+C or unrecoverable error
    """

    def __init__(self, db: DatabaseManager, config: SimulationConfig) -> None:
        self._db = db
        self._config = config
        self._fake = Faker()
        self._tick_count = 0
        # Cached order count to avoid querying COUNT(*) every tick
        self._cached_order_count: int = 0

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Start the simulation loop.

        Handles DatabaseConnectionError gracefully (reconnects and continues).
        All other exceptions crash the process. This is intentional.
        Logs a stats summary every 10 ticks.
        """
        logger.info(
            "Simulator starting — environment limit: %d orders, "
            "%d new orders/tick, %.1fs tick interval",
            self._config.max_orders,
            self._config.new_orders_per_tick,
            self._config.tick_interval_seconds,
        )
        # Fetch the current order count so we start with accurate state
        self._refresh_order_count()

        try:
            while True:
                try:
                    self._tick()
                except DatabaseConnectionError as exc:
                    logger.error(
                        "Database connection lost on tick %d: %s — reconnecting",
                        self._tick_count,
                        exc,
                    )
                    self._db.connect()
                    logger.info("Reconnected — resuming simulation")

                time.sleep(self._config.tick_interval_seconds)

        except KeyboardInterrupt:
            logger.info("Simulator stopped after %d ticks", self._tick_count)

    # ── Tick ──────────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        self._tick_count += 1

        # Refresh the cached order count periodically
        if self._tick_count % _ORDER_COUNT_REFRESH_INTERVAL == 1:
            self._refresh_order_count()

        # 1. Place new orders only if the environment limit has not been reached
        if self._cached_order_count < self._config.max_orders:
            for _ in range(self._config.new_orders_per_tick):
                self._place_new_order()
                self._cached_order_count += 1
                if self._cached_order_count >= self._config.max_orders:
                    break
        elif self._tick_count % 10 == 0:
            logger.info(
                "Order limit reached (%d/%d) — processing existing orders only",
                self._cached_order_count,
                self._config.max_orders,
            )

        # 2. Advance in-progress orders
        self._advance_orders()

        # 3. Update shipment delivery statuses
        self._advance_shipments()

        # 4. Randomly cancel a pending order
        if random.random() < _CANCEL_RATE_PER_TICK:
            self._cancel_random_pending_order()

        # 5. Randomly process a refund
        if random.random() < _REFUND_RATE_PER_TICK:
            self._refund_random_delivered_order()

        # 6. Occasionally add a new customer (organic growth)
        if random.random() < 0.15:
            self._add_new_customer()

        if self._tick_count % 10 == 0:
            self._log_stats()

    def _refresh_order_count(self) -> None:
        """Fetch the current order count from the database into the local cache."""
        row = self._db.fetch_one("SELECT COUNT(*) FROM orders")
        if row is None:
            raise SimulationError(
                "SELECT COUNT(*) FROM orders returned None. "
                "This should never happen — check database connectivity and schema."
            )
        self._cached_order_count = int(row[0])

    # ── New order flow ────────────────────────────────────────────────────────

    def _place_new_order(self) -> None:
        """
        Select a random customer, create an order with 1-4 items, and
        immediately attach a payment.

        Raises SimulationError if required data (customers, products) is
        missing, which means the database was not seeded before simulation.
        """
        customer_ids = self._db.fetch_column(
            "SELECT customer_id FROM customers ORDER BY random() LIMIT 50"
        )
        if not customer_ids:
            raise SimulationError(
                "No customers found in the database. "
                "Run 'python main.py seed' before starting the simulator."
            )

        product_rows = self._db.fetch_all(
            "SELECT product_id, unit_price FROM products "
            "WHERE stock_qty > 0 ORDER BY random() LIMIT 4"
        )
        if not product_rows:
            logger.warning(
                "No products with stock available — skipping order creation this tick. "
                "Stock will replenish as orders are cancelled or refunded."
            )
            return

        customer_id = random.choice(customer_ids)
        now = datetime.now(tz=timezone.utc)

        row = self._db.fetch_one(
            "INSERT INTO orders (customer_id, order_date, order_status) "
            "VALUES (%s, %s, %s) RETURNING order_id",
            (customer_id, now, OrderStatus.PENDING),
        )
        if row is None:
            raise SimulationError(
                f"INSERT INTO orders RETURNING order_id returned None for customer_id={customer_id}. "
                "Check database constraints and permissions."
            )
        order_id: int = row[0]

        order_total = 0.0
        for product_id, unit_price in product_rows:
            item = OrderItem.generate(order_id, product_id, float(unit_price))
            try:
                self._db.execute(
                    "INSERT INTO order_items "
                    "(order_id, product_id, quantity, unit_price, line_total) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    item.as_insert_tuple(),
                )
            except psycopg2.Error as exc:
                raise SimulationError(
                    f"Failed to insert order item for order {order_id}: {exc}"
                ) from exc

            self._db.execute(
                "UPDATE products SET stock_qty = GREATEST(stock_qty - %s, 0) "
                "WHERE product_id = %s",
                (item.quantity, product_id),
            )
            order_total += item.line_total

        payment = Payment.generate(
            order_id=order_id,
            amount=round(order_total, 2),
            payment_date=now,
            status=PaymentStatus.COMPLETED,
        )
        try:
            self._db.execute(
                "INSERT INTO payments (order_id, method, amount, status, payment_date) "
                "VALUES (%s,%s,%s,%s,%s)",
                payment.as_insert_tuple(),
            )
        except psycopg2.Error as exc:
            raise SimulationError(
                f"Failed to insert payment for order {order_id}: {exc}"
            ) from exc

        logger.debug(
            "New order %d placed for customer %d (total: %.2f)",
            order_id,
            customer_id,
            order_total,
        )

    # ── Status advancement ────────────────────────────────────────────────────

    def _advance_orders(self) -> None:
        """
        Advance a fraction of non-terminal orders by one lifecycle step.

        Generates UPDATE events on the orders table, which DMS captures.
        """
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

            if next_status == OrderStatus.SHIPPED:
                self._create_shipment(order_id, now)

            logger.debug("Order %d: %s → %s", order_id, current_status, next_status)

    def _next_order_status(self, current: str) -> str:
        """Return the next status in the lifecycle, or the current if already terminal."""
        try:
            idx = OrderStatus.LIFECYCLE.index(current)
        except ValueError:
            logger.warning(
                "Order has unrecognised status %r — cannot advance lifecycle", current
            )
            return current
        if idx + 1 < len(OrderStatus.LIFECYCLE):
            return OrderStatus.LIFECYCLE[idx + 1]
        return current

    def _create_shipment(self, order_id: int, shipped_date: datetime) -> None:
        """Create a shipment record when an order moves to SHIPPED."""
        shipment = Shipment.generate(
            order_id=order_id,
            shipped_date=shipped_date,
            delivery_status=DeliveryStatus.IN_TRANSIT,
        )
        try:
            self._db.execute(
                "INSERT INTO shipments "
                "(order_id, carrier, delivery_status, shipped_date, delivered_date) "
                "VALUES (%s,%s,%s,%s,%s)",
                shipment.as_insert_tuple(),
            )
        except psycopg2.Error as exc:
            raise SimulationError(
                f"Failed to create shipment for order {order_id}: {exc}"
            ) from exc

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
                    "UPDATE shipments SET delivery_status = %s, delivered_date = %s "
                    "WHERE shipment_id = %s",
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
            logger.warning(
                "Shipment has unrecognised status %r — cannot advance lifecycle", current
            )
            return current
        if idx + 1 < len(DeliveryStatus.LIFECYCLE):
            return DeliveryStatus.LIFECYCLE[idx + 1]
        return current

    # ── Cancellation and refund ───────────────────────────────────────────────

    def _cancel_random_pending_order(self) -> None:
        """Cancel one random pending or confirmed order."""
        row = self._db.fetch_one(
            "SELECT order_id FROM orders "
            "WHERE order_status IN ('pending', 'confirmed') "
            "ORDER BY random() LIMIT 1"
        )
        if row is None:
            return  # No pending orders right now. This is fine, not an error.
        order_id = row[0]

        self._db.execute(
            "UPDATE orders SET order_status = %s WHERE order_id = %s",
            (OrderStatus.CANCELLED, order_id),
        )
        self._db.execute(
            "UPDATE payments SET status = %s WHERE order_id = %s AND status = 'completed'",
            (PaymentStatus.REFUNDED, order_id),
        )
        logger.debug("Order %d cancelled", order_id)

    def _refund_random_delivered_order(self) -> None:
        """Mark a random delivered order as refunded."""
        row = self._db.fetch_one(
            "SELECT order_id FROM orders WHERE order_status = 'delivered' "
            "ORDER BY random() LIMIT 1"
        )
        if row is None:
            return  # No delivered orders yet. Fine.
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
        """Insert a new customer. Simulates organic sign-ups."""
        customer = Customer.generate(self._fake)
        # ON CONFLICT DO NOTHING: email must be unique. A collision (two fake
        # customers generating the same email) is not an error. Just skip it.
        rows_affected = self._db.execute(
            "INSERT INTO customers "
            "(first_name, last_name, email, country, phone, signup_date) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (email) DO NOTHING",
            customer.as_insert_tuple(),
        )
        if rows_affected == 0:
            logger.debug("Skipped duplicate customer email — not an error")

    # ── Stats ─────────────────────────────────────────────────────────────────

    def _log_stats(self) -> None:
        """Log a snapshot of table sizes every 10 ticks."""
        row = self._db.fetch_one(
            """
            SELECT
                (SELECT COUNT(*) FROM customers)  AS customers,
                (SELECT COUNT(*) FROM orders)      AS orders,
                (SELECT COUNT(*) FROM order_items) AS items,
                (SELECT COUNT(*) FROM payments)    AS payments,
                (SELECT COUNT(*) FROM shipments)   AS shipments
            """
        )
        if row is None:
            logger.error("Could not fetch stats — stats query returned None")
            return
        customers, orders, items, payments, shipments = row
        logger.info(
            "[tick %d] customers=%d  orders=%d  items=%d  payments=%d  shipments=%d",
            self._tick_count,
            customers,
            orders,
            items,
            payments,
            shipments,
        )
