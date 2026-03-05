"""
Tests for data model classes.

These verify that:
- Each model's generate() factory produces a valid, fully populated object.
- as_insert_tuple() returns the correct number of columns in the right order.
- Computed fields (line_total) are calculated correctly.
- Faker's reproducible seed produces consistent output.

No database is needed — models are pure Python dataclasses.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from faker import Faker

from simulator.models import Customer, Order, OrderItem, Payment, Product, Shipment
from simulator.config import OrderStatus, PaymentStatus, DeliveryStatus


@pytest.fixture(scope="module")
def fake() -> Faker:
    """Seeded Faker for reproducible test data."""
    Faker.seed(0)
    return Faker()


# ── Customer ──────────────────────────────────────────────────────────────────


class TestCustomer:
    def test_generate_returns_customer(self, fake):
        customer = Customer.generate(fake)
        assert isinstance(customer, Customer)

    def test_all_fields_populated(self, fake):
        customer = Customer.generate(fake)
        assert customer.first_name
        assert customer.last_name
        assert customer.email
        assert customer.country
        assert customer.phone
        assert customer.signup_date is not None

    def test_email_contains_at_symbol(self, fake):
        customer = Customer.generate(fake)
        assert "@" in customer.email

    def test_as_insert_tuple_has_6_columns(self, fake):
        customer = Customer.generate(fake)
        assert len(customer.as_insert_tuple()) == 6

    def test_as_insert_tuple_column_order(self, fake):
        customer = Customer.generate(fake)
        t = customer.as_insert_tuple()
        # (first_name, last_name, email, country, phone, signup_date)
        assert t[0] == customer.first_name
        assert t[1] == customer.last_name
        assert t[2] == customer.email
        assert t[3] == customer.country
        assert t[4] == customer.phone
        assert t[5] == customer.signup_date

    def test_signup_date_is_timezone_aware(self, fake):
        customer = Customer.generate(fake)
        assert customer.signup_date.tzinfo is not None


# ── Product ───────────────────────────────────────────────────────────────────


class TestProduct:
    def test_generate_returns_product(self, fake):
        product = Product.generate(fake)
        assert isinstance(product, Product)

    def test_unit_price_is_positive(self, fake):
        for _ in range(20):
            product = Product.generate(fake)
            assert product.unit_price > 0

    def test_stock_qty_is_non_negative(self, fake):
        for _ in range(20):
            product = Product.generate(fake)
            assert product.stock_qty >= 0

    def test_as_insert_tuple_has_5_columns(self, fake):
        product = Product.generate(fake)
        assert len(product.as_insert_tuple()) == 5

    def test_as_insert_tuple_column_order(self, fake):
        product = Product.generate(fake)
        t = product.as_insert_tuple()
        # (name, category, brand, unit_price, stock_qty)
        assert t[0] == product.name
        assert t[1] == product.category
        assert t[2] == product.brand
        assert t[3] == product.unit_price
        assert t[4] == product.stock_qty


# ── Order ─────────────────────────────────────────────────────────────────────


class TestOrder:
    def test_generate_returns_order(self):
        order = Order.generate(customer_id=1)
        assert isinstance(order, Order)

    def test_default_status_is_pending(self):
        order = Order.generate(customer_id=1)
        assert order.order_status == OrderStatus.PENDING

    def test_custom_status_is_respected(self):
        order = Order.generate(customer_id=1, order_status=OrderStatus.SHIPPED)
        assert order.order_status == OrderStatus.SHIPPED

    def test_as_insert_tuple_has_3_columns(self):
        order = Order.generate(customer_id=1)
        assert len(order.as_insert_tuple()) == 3

    def test_as_insert_tuple_column_order(self):
        order = Order.generate(customer_id=42)
        t = order.as_insert_tuple()
        # (customer_id, order_date, order_status)
        assert t[0] == 42
        assert t[2] == OrderStatus.PENDING


# ── OrderItem ─────────────────────────────────────────────────────────────────


class TestOrderItem:
    def test_generate_returns_order_item(self):
        item = OrderItem.generate(order_id=1, product_id=1, unit_price=10.00)
        assert isinstance(item, OrderItem)

    def test_line_total_equals_quantity_times_unit_price(self):
        """line_total must equal quantity * unit_price, rounded to 2 decimal places."""
        for _ in range(50):
            item = OrderItem.generate(order_id=1, product_id=1, unit_price=9.99)
            expected = round(item.quantity * item.unit_price, 2)
            assert item.line_total == expected, (
                f"line_total={item.line_total} but quantity={item.quantity} * "
                f"unit_price={item.unit_price} = {expected}"
            )

    def test_quantity_is_at_least_1(self):
        for _ in range(20):
            item = OrderItem.generate(order_id=1, product_id=1, unit_price=5.00)
            assert item.quantity >= 1

    def test_as_insert_tuple_has_5_columns(self):
        item = OrderItem.generate(order_id=1, product_id=2, unit_price=19.99)
        assert len(item.as_insert_tuple()) == 5

    def test_as_insert_tuple_column_order(self):
        item = OrderItem.generate(order_id=10, product_id=20, unit_price=15.00)
        t = item.as_insert_tuple()
        # (order_id, product_id, quantity, unit_price, line_total)
        assert t[0] == 10
        assert t[1] == 20
        assert t[3] == 15.00


# ── Payment ───────────────────────────────────────────────────────────────────


class TestPayment:
    def test_generate_returns_payment(self):
        payment = Payment.generate(order_id=1, amount=99.99)
        assert isinstance(payment, Payment)

    def test_default_status_is_completed(self):
        payment = Payment.generate(order_id=1, amount=50.00)
        assert payment.status == PaymentStatus.COMPLETED

    def test_custom_status_is_respected(self):
        payment = Payment.generate(order_id=1, amount=50.00, status=PaymentStatus.REFUNDED)
        assert payment.status == PaymentStatus.REFUNDED

    def test_as_insert_tuple_has_5_columns(self):
        payment = Payment.generate(order_id=1, amount=100.00)
        assert len(payment.as_insert_tuple()) == 5

    def test_as_insert_tuple_column_order(self):
        now = datetime.now(tz=timezone.utc)
        payment = Payment.generate(order_id=5, amount=75.50, payment_date=now)
        t = payment.as_insert_tuple()
        # (order_id, method, amount, status, payment_date)
        assert t[0] == 5
        assert t[2] == 75.50
        assert t[4] == now


# ── Shipment ──────────────────────────────────────────────────────────────────


class TestShipment:
    def test_generate_returns_shipment(self):
        shipment = Shipment.generate(order_id=1)
        assert isinstance(shipment, Shipment)

    def test_default_delivery_status_is_pending(self):
        shipment = Shipment.generate(order_id=1)
        assert shipment.delivery_status == DeliveryStatus.PENDING

    def test_custom_delivery_status_is_respected(self):
        shipment = Shipment.generate(order_id=1, delivery_status=DeliveryStatus.IN_TRANSIT)
        assert shipment.delivery_status == DeliveryStatus.IN_TRANSIT

    def test_as_insert_tuple_has_5_columns(self):
        shipment = Shipment.generate(order_id=1)
        assert len(shipment.as_insert_tuple()) == 5

    def test_as_insert_tuple_column_order(self):
        now = datetime.now(tz=timezone.utc)
        shipment = Shipment.generate(order_id=7, shipped_date=now)
        t = shipment.as_insert_tuple()
        # (order_id, carrier, delivery_status, shipped_date, delivered_date)
        assert t[0] == 7
        assert t[3] == now
