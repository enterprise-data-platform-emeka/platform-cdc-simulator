"""
Historical data seeder.

The Seeder populates the database with two years of realistic historical data
before the live simulation starts. This gives DMS and downstream processing
something meaningful to work with from day one, rather than starting with an
empty database where every query returns zero rows.

What the seeder creates and why:
- Customers spread across the last 2 years (not all signing up today)
- Products with varied stock levels
- Historical orders at realistic lifecycle stages:
    ~70% fully delivered, ~10% in various in-progress states,
    ~10% cancelled, ~5% refunded, ~5% with failed deliveries
  This reflects a real order book — most old orders are complete, but some
  are still in flight and a realistic fraction went wrong.

The seeder is idempotent when called with the same random seed and the same
customer/product counts. Re-running it will insert duplicate rows if the tables
are not empty first. Use `main.py reset` to wipe and re-seed cleanly.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from faker import Faker

from simulator.config import (
    CANCELLATION_RATE,
    REFUND_RATE,
    DeliveryStatus,
    OrderStatus,
    PaymentStatus,
    SeedConfig,
)
from simulator.db import DatabaseManager
from simulator.models import (
    Customer,
    Order,
    OrderItem,
    Payment,
    Product,
    Shipment,
)

logger = logging.getLogger(__name__)


class Seeder:
    """
    Seeds the database with historical data.

    Usage:
        seeder = Seeder(db, seed_config)
        seeder.run()
    """

    def __init__(self, db: DatabaseManager, config: SeedConfig) -> None:
        self._db = db
        self._config = config
        self._fake = Faker()
        Faker.seed(config.random_seed)
        random.seed(config.random_seed)

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self) -> None:
        """Seed all tables. Steps are logged so progress is visible."""
        logger.info(
            "Seeding: %d customers, %d products, %d historical orders",
            self._config.num_customers,
            self._config.num_products,
            self._config.num_historical_orders,
        )
        self._seed_customers()
        self._seed_products()
        self._seed_historical_orders()
        logger.info("Seeding complete")

    # ── Customers ─────────────────────────────────────────────────────────────

    def _seed_customers(self) -> None:
        logger.info("Seeding %d customers", self._config.num_customers)
        customers = [
            Customer.generate(self._fake).as_insert_tuple()
            for _ in range(self._config.num_customers)
        ]
        inserted = self._db.execute_many(
            "INSERT INTO customers (first_name, last_name, email, country, phone, signup_date) VALUES %s",
            customers,
        )
        logger.info("Inserted %d customers", inserted)

    # ── Products ──────────────────────────────────────────────────────────────

    def _seed_products(self) -> None:
        logger.info("Seeding %d products", self._config.num_products)
        products = [
            Product.generate(self._fake).as_insert_tuple()
            for _ in range(self._config.num_products)
        ]
        inserted = self._db.execute_many(
            "INSERT INTO products (name, category, brand, unit_price, stock_qty) VALUES %s",
            products,
        )
        logger.info("Inserted %d products", inserted)

    # ── Historical orders ─────────────────────────────────────────────────────

    def _seed_historical_orders(self) -> None:
        """
        Create historical orders spread over the last 2 years.

        Each order is placed at a random point in the past. The order's final
        state depends on how old it is and a random roll:
        - Orders placed > 14 days ago are almost always complete or cancelled.
        - More recent orders may still be in transit.
        """
        logger.info("Seeding %d historical orders", self._config.num_historical_orders)

        customer_ids = self._db.fetch_column("SELECT customer_id FROM customers")
        product_rows = self._db.fetch_all("SELECT product_id, unit_price FROM products")

        if not customer_ids or not product_rows:
            logger.error("Cannot seed orders: customers or products table is empty")
            return

        product_map = {row[0]: float(row[1]) for row in product_rows}
        product_ids = list(product_map.keys())

        orders_inserted = 0
        for _ in range(self._config.num_historical_orders):
            # Place the order at a random time in the past 2 years
            order_date = self._fake.date_time_between(
                start_date="-2y", end_date="-1d", tzinfo=timezone.utc
            )
            days_ago = (datetime.now(tz=timezone.utc) - order_date).days

            customer_id = random.choice(customer_ids)
            final_status, payment_status, delivery_status = self._determine_lifecycle(days_ago)

            # Insert the order and get its generated ID
            order_id = self._insert_order(customer_id, order_date, final_status)
            if order_id is None:
                continue

            # Insert 1–4 items for this order
            num_items = random.randint(1, 4)
            selected_products = random.sample(product_ids, min(num_items, len(product_ids)))
            order_total = 0.0
            for product_id in selected_products:
                item = OrderItem.generate(order_id, product_id, product_map[product_id])
                self._db.execute(
                    "INSERT INTO order_items (order_id, product_id, quantity, unit_price, line_total) VALUES (%s,%s,%s,%s,%s)",
                    item.as_insert_tuple(),
                )
                order_total += item.line_total

            order_total = round(order_total, 2)

            # Insert payment if the order was not cancelled before payment
            if final_status not in (OrderStatus.CANCELLED,):
                payment_date = order_date + timedelta(minutes=random.randint(5, 60))
                payment = Payment.generate(
                    order_id=order_id,
                    amount=order_total,
                    payment_date=payment_date,
                    status=payment_status,
                )
                self._db.execute(
                    "INSERT INTO payments (order_id, method, amount, status, payment_date) VALUES (%s,%s,%s,%s,%s)",
                    payment.as_insert_tuple(),
                )

            # Insert shipment if the order reached shipping stage
            if final_status in (
                OrderStatus.SHIPPED, OrderStatus.DELIVERED, OrderStatus.REFUNDED
            ):
                shipped_date = order_date + timedelta(days=random.randint(1, 3))
                delivered_date = (
                    shipped_date + timedelta(days=random.randint(2, 7))
                    if delivery_status == DeliveryStatus.DELIVERED
                    else None
                )
                shipment = Shipment.generate(
                    order_id=order_id,
                    shipped_date=shipped_date,
                    delivered_date=delivered_date,
                    delivery_status=delivery_status,
                )
                self._db.execute(
                    "INSERT INTO shipments (order_id, carrier, delivery_status, shipped_date, delivered_date) VALUES (%s,%s,%s,%s,%s)",
                    shipment.as_insert_tuple(),
                )

            orders_inserted += 1

        logger.info("Inserted %d historical orders (with items, payments, shipments)", orders_inserted)

    def _insert_order(
        self, customer_id: int, order_date: datetime, order_status: str
    ) -> Optional[int]:
        """Insert a single order and return its generated order_id."""
        row = self._db.fetch_one(
            "INSERT INTO orders (customer_id, order_date, order_status) VALUES (%s, %s, %s) RETURNING order_id",
            (customer_id, order_date, order_status),
        )
        return row[0] if row else None

    def _determine_lifecycle(
        self, days_ago: int
    ) -> tuple[str, str, str]:
        """
        Decide the final state of a historical order based on its age.

        Returns (order_status, payment_status, delivery_status).
        """
        # Old orders (> 14 days) should be in terminal state
        if days_ago > 14:
            roll = random.random()
            if roll < CANCELLATION_RATE:
                return OrderStatus.CANCELLED, PaymentStatus.REFUNDED, DeliveryStatus.PENDING
            if roll < CANCELLATION_RATE + REFUND_RATE:
                return OrderStatus.REFUNDED, PaymentStatus.REFUNDED, DeliveryStatus.RETURNED
            # A small fraction had delivery failures
            if roll < CANCELLATION_RATE + REFUND_RATE + 0.03:
                return OrderStatus.SHIPPED, PaymentStatus.COMPLETED, DeliveryStatus.FAILED_DELIVERY
            return OrderStatus.DELIVERED, PaymentStatus.COMPLETED, DeliveryStatus.DELIVERED

        # Recent orders can be at any lifecycle stage
        if days_ago > 7:
            status = random.choice([OrderStatus.DELIVERED, OrderStatus.SHIPPED, OrderStatus.PROCESSING])
        elif days_ago > 3:
            status = random.choice([OrderStatus.PROCESSING, OrderStatus.CONFIRMED, OrderStatus.SHIPPED])
        else:
            status = random.choice([OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.PROCESSING])

        payment_status = PaymentStatus.COMPLETED
        delivery_status = {
            OrderStatus.PENDING:    DeliveryStatus.PENDING,
            OrderStatus.CONFIRMED:  DeliveryStatus.PENDING,
            OrderStatus.PROCESSING: DeliveryStatus.PENDING,
            OrderStatus.SHIPPED:    DeliveryStatus.IN_TRANSIT,
            OrderStatus.DELIVERED:  DeliveryStatus.DELIVERED,
        }.get(status, DeliveryStatus.PENDING)

        return status, payment_status, delivery_status
