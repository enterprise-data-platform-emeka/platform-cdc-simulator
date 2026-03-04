"""
Configuration and domain constants for the CDC simulator.

Design rules:
- All config is loaded from environment variables so the simulator works
  identically in local dev (via .env) and in a container (via env vars).
- Config objects are frozen dataclasses: immutable after construction, safe to
  pass around without worrying about mutation.
- Domain constants are plain classes with class-level string attributes.
  This gives IDE autocompletion, prevents typos, and makes grep-able constants
  rather than magic strings scattered through the code.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Final

from dotenv import load_dotenv

# Load .env file if present. In production containers this is a no-op because
# the variables are already in the environment.
load_dotenv()


# ── Domain constants ──────────────────────────────────────────────────────────
#
# Each class groups related string constants for a single domain concept.
# Use OrderStatus.PENDING instead of the string "pending" everywhere in the
# codebase. This makes refactors safe and keeps IDEs happy.


class OrderStatus:
    PENDING: Final = "pending"
    CONFIRMED: Final = "confirmed"
    PROCESSING: Final = "processing"
    SHIPPED: Final = "shipped"
    DELIVERED: Final = "delivered"
    CANCELLED: Final = "cancelled"
    REFUNDED: Final = "refunded"

    # Defines the lifecycle order. An order can only advance forward (or cancel/refund).
    LIFECYCLE: Final = [PENDING, CONFIRMED, PROCESSING, SHIPPED, DELIVERED]

    # Statuses that are terminal — no further transitions are valid.
    TERMINAL: Final = frozenset([DELIVERED, CANCELLED, REFUNDED])


class PaymentMethod:
    CREDIT_CARD: Final = "credit_card"
    DEBIT_CARD: Final = "debit_card"
    PAYPAL: Final = "paypal"
    BANK_TRANSFER: Final = "bank_transfer"
    CRYPTO: Final = "crypto"

    ALL: Final = [CREDIT_CARD, DEBIT_CARD, PAYPAL, BANK_TRANSFER, CRYPTO]

    # Probability weights aligned with ALL list.
    # Credit/debit cards dominate; crypto is rare.
    WEIGHTS: Final = [40, 30, 15, 10, 5]


class PaymentStatus:
    PENDING: Final = "pending"
    COMPLETED: Final = "completed"
    FAILED: Final = "failed"
    REFUNDED: Final = "refunded"


class DeliveryStatus:
    PENDING: Final = "pending"
    IN_TRANSIT: Final = "in_transit"
    OUT_FOR_DELIVERY: Final = "out_for_delivery"
    DELIVERED: Final = "delivered"
    FAILED_DELIVERY: Final = "failed_delivery"
    RETURNED: Final = "returned"

    LIFECYCLE: Final = [PENDING, IN_TRANSIT, OUT_FOR_DELIVERY, DELIVERED]
    TERMINAL: Final = frozenset([DELIVERED, FAILED_DELIVERY, RETURNED])


class Carrier:
    DHL: Final = "DHL"
    FEDEX: Final = "FedEx"
    UPS: Final = "UPS"
    ROYAL_MAIL: Final = "Royal Mail"
    DPD: Final = "DPD"

    ALL: Final = [DHL, FEDEX, UPS, ROYAL_MAIL, DPD]
    WEIGHTS: Final = [25, 20, 20, 20, 15]


class ProductCategory:
    ELECTRONICS: Final = "Electronics"
    CLOTHING: Final = "Clothing"
    HOME_GARDEN: Final = "Home & Garden"
    SPORTS: Final = "Sports"
    BOOKS: Final = "Books"
    BEAUTY: Final = "Beauty"
    TOYS: Final = "Toys"
    FOOD: Final = "Food & Beverages"

    ALL: Final = [ELECTRONICS, CLOTHING, HOME_GARDEN, SPORTS, BOOKS, BEAUTY, TOYS, FOOD]

    # Higher weight = more products in that category
    WEIGHTS: Final = [20, 25, 10, 10, 10, 10, 8, 7]


class ProductBrand:
    # A representative sample of plausible brand names
    ALL: Final = [
        "Nexon", "PeakWear", "HomeFirst", "SwiftGear", "PageTurner",
        "GlowLab", "FunZone", "NutriCo", "TechPlus", "UrbanEdge",
        "AlphaCore", "BrightLeaf", "SkyLine", "EarthWorks", "PurePulse",
    ]


# Price ranges per category (min, max) in EUR
CATEGORY_PRICE_RANGE: Final[dict[str, tuple[float, float]]] = {
    ProductCategory.ELECTRONICS:  (29.99,  1499.99),
    ProductCategory.CLOTHING:     (9.99,   299.99),
    ProductCategory.HOME_GARDEN:  (4.99,   499.99),
    ProductCategory.SPORTS:       (14.99,  399.99),
    ProductCategory.BOOKS:        (4.99,   49.99),
    ProductCategory.BEAUTY:       (5.99,   149.99),
    ProductCategory.TOYS:         (7.99,   199.99),
    ProductCategory.FOOD:         (1.99,   59.99),
}

# Countries with relative weights reflecting typical European e-commerce traffic
CUSTOMER_COUNTRIES: Final[list[str]] = [
    "Germany", "France", "United Kingdom", "Netherlands", "Spain",
    "Italy", "Poland", "Belgium", "Sweden", "Austria",
    "Switzerland", "Portugal", "Denmark", "Finland", "Ireland",
]
CUSTOMER_COUNTRY_WEIGHTS: Final[list[int]] = [
    20, 15, 15, 8, 8, 8, 5, 4, 4, 3, 3, 2, 2, 2, 1,
]

# Probability that any given order will be cancelled before delivery
CANCELLATION_RATE: Final[float] = 0.05

# Probability that a delivered order will be refunded
REFUND_RATE: Final[float] = 0.03

# Max items per order
MAX_ITEMS_PER_ORDER: Final[int] = 6


# ── Config dataclasses ────────────────────────────────────────────────────────
#
# Frozen dataclasses act as immutable value objects. Load them once at startup
# and pass them into every class that needs configuration.


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str

    @classmethod
    def from_env(cls) -> DatabaseConfig:
        return cls(
            host=os.environ["DB_HOST"],
            port=int(os.getenv("DB_PORT", "5432")),
            dbname=os.environ["DB_NAME"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
        )

    def dsn(self) -> str:
        """Return a libpq-style DSN string (password is redacted in logs — use repr instead)."""
        return (
            f"host={self.host} port={self.port} dbname={self.dbname} "
            f"user={self.user} password={self.password}"
        )

    def __repr__(self) -> str:
        """Never include the password in repr output."""
        return (
            f"DatabaseConfig(host={self.host!r}, port={self.port}, "
            f"dbname={self.dbname!r}, user={self.user!r}, password='***')"
        )


@dataclass(frozen=True)
class SeedConfig:
    num_customers: int
    num_products: int
    num_historical_orders: int
    random_seed: int

    @classmethod
    def from_env(cls) -> SeedConfig:
        return cls(
            num_customers=int(os.getenv("SEED_CUSTOMERS", "500")),
            num_products=int(os.getenv("SEED_PRODUCTS", "200")),
            num_historical_orders=int(os.getenv("SEED_HISTORICAL_ORDERS", "2000")),
            random_seed=int(os.getenv("SEED_RANDOM_SEED", "42")),
        )


@dataclass(frozen=True)
class SimulationConfig:
    tick_interval_seconds: float
    new_orders_per_tick: int

    @classmethod
    def from_env(cls) -> SimulationConfig:
        return cls(
            tick_interval_seconds=float(os.getenv("SIM_TICK_INTERVAL_SECONDS", "2")),
            new_orders_per_tick=int(os.getenv("SIM_NEW_ORDERS_PER_TICK", "3")),
        )


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int
    wait_min_seconds: float
    wait_max_seconds: float

    @classmethod
    def from_env(cls) -> RetryConfig:
        return cls(
            max_attempts=int(os.getenv("RETRY_MAX_ATTEMPTS", "5")),
            wait_min_seconds=float(os.getenv("RETRY_WAIT_MIN_SECONDS", "1")),
            wait_max_seconds=float(os.getenv("RETRY_WAIT_MAX_SECONDS", "30")),
        )


# ── Logging ───────────────────────────────────────────────────────────────────


def configure_logging(level: int = logging.INFO) -> None:
    """
    Configure root logger with a consistent format.

    Call once at process startup (in main.py). All modules use
    `logging.getLogger(__name__)` and inherit this configuration.
    """
    logging.basicConfig(
        stream=sys.stdout,
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
