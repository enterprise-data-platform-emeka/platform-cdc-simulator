"""
Historical data seeder.

The Seeder populates the database with two years of realistic historical data
before the live simulation starts. This gives DMS and downstream processing
something meaningful to work with from day one, rather than starting with an
empty database where every query returns zero rows.

Error handling contract:
- Every failure raises SeedError with a descriptive message.
- Nothing is skipped silently. If one order fails to insert, the error is
  raised immediately. A partial seed is worse than no seed because it
  produces misleading data in the pipeline.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg2
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
from simulator.exceptions import SeedError
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
        """
        Seed all tables in order.

        Raises SeedError on any failure. A partial seed is not left behind:
        either the full seed succeeds or the caller should reset and retry.
        """
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
        try:
            inserted = self._db.execute_many(
                "INSERT INTO customers "
                "(first_name, last_name, email, country, phone, signup_date) VALUES %s",
                customers,
            )
        except psycopg2.Error as exc:
            raise SeedError(f"Failed to seed customers: {exc}") from exc
        logger.info("Inserted %d customers", inserted)

    # ── Products ──────────────────────────────────────────────────────────────

    def _seed_products(self) -> None:
        logger.info("Seeding %d products", self._config.num_products)
        products = [
            Product.generate(self._fake).as_insert_tuple()
            for _ in range(self._config.num_products)
        ]
        try:
            inserted = self._db.execute_many(
                "INSERT INTO products (name, category, brand, unit_price, stock_qty) VALUES %s",
                products,
            )
        except psycopg2.Error as exc:
            raise SeedError(f"Failed to seed products: {exc}") from exc
        logger.info("Inserted %d products", inserted)

    # ── Historical orders ─────────────────────────────────────────────────────

    def _seed_historical_orders(self) -> None:
        """
        Create historical orders spread over the last 2 years.

        Orders placed more than 14 days ago are in terminal state (delivered,
        cancelled, or refunded). More recent orders may still be in transit.
        """
        logger.info("Seeding %d historical orders", self._config.num_historical_orders)

        customer_ids = self._db.fetch_column("SELECT customer_id FROM customers")
        product_rows = self._db.fetch_all("SELECT product_id, unit_price FROM products")

        if not customer_ids:
            raise SeedError(
                "Cannot seed orders: customers table is empty. "
                "Seed customers first or check that the schema was applied."
            )
        if not product_rows:
            raise SeedError(
                "Cannot seed orders: products table is empty. "
                "Seed products first or check that the schema was applied."
            )

        product_map = {row[0]: float(row[1]) for row in product_rows}
        product_ids = list(product_map.keys())
        orders_inserted = 0

        for i in range(self._config.num_historical_orders):
            order_date = self._fake.date_time_between(
                start_date="-2y", end_date="-1d", tzinfo=timezone.utc
            )
            days_ago = (datetime.now(tz=timezone.utc) - order_date).days
            customer_id = random.choice(customer_ids)
            final_status, payment_status, delivery_status = self._determine_lifecycle(days_ago)

            try:
                order_id = self._insert_order(customer_id, order_date, final_status)
            except psycopg2.Error as exc:
                raise SeedError(
                    f"Failed to insert historical order {i + 1}/{self._config.num_historical_orders}: {exc}"
                ) from exc

            num_items = random.randint(1, 4)
            selected = random.sample(product_ids, min(num_items, len(product_ids)))
            order_total = 0.0

            for product_id in selected:
                item = OrderItem.generate(order_id, product_id, product_map[product_id])
                try:
                    self._db.execute(
                        "INSERT INTO order_items "
                        "(order_id, product_id, quantity, unit_price, line_total) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        item.as_insert_tuple(),
                    )
                except psycopg2.Error as exc:
                    raise SeedError(
                        f"Failed to insert order item for order {order_id}: {exc}"
                    ) from exc
                order_total += item.line_total

            order_total = round(order_total, 2)

            if final_status != OrderStatus.CANCELLED:
                payment_date = order_date + timedelta(minutes=random.randint(5, 60))
                payment = Payment.generate(
                    order_id=order_id,
                    amount=order_total,
                    payment_date=payment_date,
                    status=payment_status,
                )
                try:
                    self._db.execute(
                        "INSERT INTO payments (order_id, method, amount, status, payment_date) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        payment.as_insert_tuple(),
                    )
                except psycopg2.Error as exc:
                    raise SeedError(
                        f"Failed to insert payment for order {order_id}: {exc}"
                    ) from exc

            if final_status in (OrderStatus.SHIPPED, OrderStatus.DELIVERED, OrderStatus.REFUNDED):
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
                try:
                    self._db.execute(
                        "INSERT INTO shipments "
                        "(order_id, carrier, delivery_status, shipped_date, delivered_date) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        shipment.as_insert_tuple(),
                    )
                except psycopg2.Error as exc:
                    raise SeedError(
                        f"Failed to insert shipment for order {order_id}: {exc}"
                    ) from exc

            orders_inserted += 1

        logger.info(
            "Inserted %d historical orders (with items, payments, shipments)",
            orders_inserted,
        )

    def _insert_order(
        self, customer_id: int, order_date: datetime, order_status: str
    ) -> int:
        """
        Insert a single order and return its generated order_id.

        Raises psycopg2.Error on failure (caller wraps in SeedError).
        Never returns None. A failed INSERT raises rather than returning silently.
        """
        row = self._db.fetch_one(
            "INSERT INTO orders (customer_id, order_date, order_status) "
            "VALUES (%s, %s, %s) RETURNING order_id",
            (customer_id, order_date, order_status),
        )
        if row is None:
            raise SeedError(
                f"INSERT INTO orders returned no row for customer_id={customer_id}. "
                "This should never happen — check database permissions and constraints."
            )
        return int(row[0])

    def _determine_lifecycle(
        self, days_ago: int
    ) -> tuple[str, str, str]:
        """
        Decide the final state of a historical order based on its age.

        Returns (order_status, payment_status, delivery_status).

        Orders older than 14 days are almost always in a terminal state.
        More recent orders reflect the in-progress reality of a live pipeline.
        """
        if days_ago > 14:
            roll = random.random()
            if roll < CANCELLATION_RATE:
                return OrderStatus.CANCELLED, PaymentStatus.REFUNDED, DeliveryStatus.PENDING
            if roll < CANCELLATION_RATE + REFUND_RATE:
                return OrderStatus.REFUNDED, PaymentStatus.REFUNDED, DeliveryStatus.RETURNED
            if roll < CANCELLATION_RATE + REFUND_RATE + 0.03:
                return OrderStatus.SHIPPED, PaymentStatus.COMPLETED, DeliveryStatus.FAILED_DELIVERY
            return OrderStatus.DELIVERED, PaymentStatus.COMPLETED, DeliveryStatus.DELIVERED

        if days_ago > 7:
            status = random.choice([OrderStatus.DELIVERED, OrderStatus.SHIPPED, OrderStatus.PROCESSING])
        elif days_ago > 3:
            status = random.choice([OrderStatus.PROCESSING, OrderStatus.CONFIRMED, OrderStatus.SHIPPED])
        else:
            status = random.choice([OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.PROCESSING])

        delivery_status = {
            OrderStatus.PENDING:    DeliveryStatus.PENDING,
            OrderStatus.CONFIRMED:  DeliveryStatus.PENDING,
            OrderStatus.PROCESSING: DeliveryStatus.PENDING,
            OrderStatus.SHIPPED:    DeliveryStatus.IN_TRANSIT,
            OrderStatus.DELIVERED:  DeliveryStatus.DELIVERED,
        }.get(status, DeliveryStatus.PENDING)

        return status, PaymentStatus.COMPLETED, delivery_status
