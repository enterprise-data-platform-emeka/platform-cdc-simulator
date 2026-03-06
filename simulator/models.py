"""
Data models for the e-commerce OLTP schema.

Each model is a dataclass that mirrors one database table row.
Every model has:

    generate(fake, ...) -> Model
        A classmethod factory that produces a realistic fake instance using
        Faker and the domain constants in config.py. All randomness goes
        through the Faker instance so callers control the seed.

    as_insert_tuple() -> tuple
        Returns a tuple whose column order matches the INSERT statement for
        that table. This keeps the mapping between Python objects and SQL in
        one place — not split between models.py and seed.py/simulate.py.

Models intentionally omit auto-generated fields (primary keys, default
timestamps) from as_insert_tuple() because PostgreSQL generates those.
"""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from faker import Faker

from simulator.config import (
    CANCELLATION_RATE,
    CATEGORY_PRICE_RANGE,
    CUSTOMER_COUNTRIES,
    CUSTOMER_COUNTRY_WEIGHTS,
    MAX_ITEMS_PER_ORDER,
    REFUND_RATE,
    Carrier,
    DeliveryStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    ProductBrand,
    ProductCategory,
)


def _weighted_choice(choices: list, weights: list) -> str:
    """Single weighted random choice. Wraps random.choices for readability."""
    return random.choices(choices, weights=weights, k=1)[0]


def _random_price(category: str) -> float:
    """Return a price within the realistic range for a product category."""
    lo, hi = CATEGORY_PRICE_RANGE[category]
    # Use a skewed distribution: most prices cluster toward the lower end.
    price = lo + (hi - lo) * (random.random() ** 1.5)
    return round(price, 2)


# ── Customer ──────────────────────────────────────────────────────────────────


@dataclass
class Customer:
    first_name: str
    last_name: str
    email: str
    country: str
    phone: str
    signup_date: datetime

    @classmethod
    def generate(cls, fake: Faker, signup_date: Optional[datetime] = None) -> Customer:
        first = fake.first_name()
        last = fake.last_name()
        # Build a realistic email from the name rather than using fake.email()
        # which produces obviously fake domains.
        domain = random.choice(["gmail.com", "outlook.com", "yahoo.com",
                                "hotmail.com", "icloud.com", "proton.me"])
        # secrets.token_hex(4) gives 8 hex characters (4 billion possibilities).
        # This guarantees uniqueness even at prod scale (15,000 customers).
        # A 2-digit suffix added 30% of the time (the previous approach) only
        # produced ~100 variations per name, causing UniqueViolation errors.
        unique_tag = secrets.token_hex(4)
        email = f"{first.lower()}.{last.lower()}.{unique_tag}@{domain}"
        country = _weighted_choice(CUSTOMER_COUNTRIES, CUSTOMER_COUNTRY_WEIGHTS)
        phone = fake.phone_number()
        if signup_date is None:
            signup_date = fake.date_time_between(start_date="-2y", end_date="now",
                                                  tzinfo=timezone.utc)
        return cls(
            first_name=first,
            last_name=last,
            email=email,
            country=country,
            phone=phone,
            signup_date=signup_date,
        )

    def as_insert_tuple(self) -> tuple:
        """
        Column order: (first_name, last_name, email, country, phone, signup_date)
        Matches: INSERT INTO customers (first_name, last_name, email, country, phone, signup_date)
        """
        return (
            self.first_name,
            self.last_name,
            self.email,
            self.country,
            self.phone,
            self.signup_date,
        )


# ── Product ───────────────────────────────────────────────────────────────────


@dataclass
class Product:
    name: str
    category: str
    brand: str
    unit_price: float
    stock_qty: int

    @classmethod
    def generate(cls, fake: Faker) -> Product:
        category = _weighted_choice(ProductCategory.ALL, ProductCategory.WEIGHTS)
        brand = random.choice(ProductBrand.ALL)
        # Build a product name from brand + adjective + category noun
        adjective = random.choice(["Pro", "Ultra", "Lite", "Max", "Plus", "Elite", "Eco"])
        category_noun = category.split("&")[0].strip().rstrip("s")  # e.g. "Electronic"
        name = f"{brand} {adjective} {category_noun}"
        unit_price = _random_price(category)
        stock_qty = random.randint(0, 500)
        return cls(
            name=name,
            category=category,
            brand=brand,
            unit_price=unit_price,
            stock_qty=stock_qty,
        )

    def as_insert_tuple(self) -> tuple:
        """Column order: (name, category, brand, unit_price, stock_qty)"""
        return (self.name, self.category, self.brand, self.unit_price, self.stock_qty)


# ── Order ─────────────────────────────────────────────────────────────────────


@dataclass
class Order:
    customer_id: int
    order_date: datetime
    order_status: str

    @classmethod
    def generate(
        cls,
        customer_id: int,
        order_date: Optional[datetime] = None,
        order_status: str = OrderStatus.PENDING,
    ) -> Order:
        if order_date is None:
            order_date = datetime.now(tz=timezone.utc)
        return cls(
            customer_id=customer_id,
            order_date=order_date,
            order_status=order_status,
        )

    def as_insert_tuple(self) -> tuple:
        """Column order: (customer_id, order_date, order_status)"""
        return (self.customer_id, self.order_date, self.order_status)


# ── OrderItem ─────────────────────────────────────────────────────────────────


@dataclass
class OrderItem:
    order_id: int
    product_id: int
    quantity: int
    unit_price: float
    line_total: float

    @classmethod
    def generate(cls, order_id: int, product_id: int, unit_price: float) -> OrderItem:
        quantity = random.randint(1, MAX_ITEMS_PER_ORDER)
        line_total = round(quantity * unit_price, 2)
        return cls(
            order_id=order_id,
            product_id=product_id,
            quantity=quantity,
            unit_price=unit_price,
            line_total=line_total,
        )

    def as_insert_tuple(self) -> tuple:
        """Column order: (order_id, product_id, quantity, unit_price, line_total)"""
        return (
            self.order_id,
            self.product_id,
            self.quantity,
            self.unit_price,
            self.line_total,
        )


# ── Payment ───────────────────────────────────────────────────────────────────


@dataclass
class Payment:
    order_id: int
    method: str
    amount: float
    status: str
    payment_date: datetime

    @classmethod
    def generate(
        cls,
        order_id: int,
        amount: float,
        payment_date: Optional[datetime] = None,
        status: str = PaymentStatus.COMPLETED,
    ) -> Payment:
        method = _weighted_choice(PaymentMethod.ALL, PaymentMethod.WEIGHTS)
        if payment_date is None:
            payment_date = datetime.now(tz=timezone.utc)
        return cls(
            order_id=order_id,
            method=method,
            amount=amount,
            status=status,
            payment_date=payment_date,
        )

    def as_insert_tuple(self) -> tuple:
        """Column order: (order_id, method, amount, status, payment_date)"""
        return (
            self.order_id,
            self.method,
            self.amount,
            self.status,
            self.payment_date,
        )


# ── Shipment ──────────────────────────────────────────────────────────────────


@dataclass
class Shipment:
    order_id: int
    carrier: str
    delivery_status: str
    shipped_date: Optional[datetime]
    delivered_date: Optional[datetime]

    @classmethod
    def generate(
        cls,
        order_id: int,
        shipped_date: Optional[datetime] = None,
        delivered_date: Optional[datetime] = None,
        delivery_status: str = DeliveryStatus.PENDING,
    ) -> Shipment:
        carrier = _weighted_choice(Carrier.ALL, Carrier.WEIGHTS)
        return cls(
            order_id=order_id,
            carrier=carrier,
            delivery_status=delivery_status,
            shipped_date=shipped_date,
            delivered_date=delivered_date,
        )

    def as_insert_tuple(self) -> tuple:
        """Column order: (order_id, carrier, delivery_status, shipped_date, delivered_date)"""
        return (
            self.order_id,
            self.carrier,
            self.delivery_status,
            self.shipped_date,
            self.delivered_date,
        )
