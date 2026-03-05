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
- Environment (dev/staging/prod) drives record limits automatically.
  You never need to remember to change counts when switching environments.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Final

from dotenv import load_dotenv

from simulator.exceptions import ConfigurationError

# Load .env file if present. In production containers this is a no-op because
# the variables are already in the environment.
load_dotenv()


# ── Environment ───────────────────────────────────────────────────────────────


class Environment:
    DEV: Final = "dev"
    STAGING: Final = "staging"
    PROD: Final = "prod"

    ALL: Final = frozenset([DEV, STAGING, PROD])


# ── Per-environment record limits ─────────────────────────────────────────────
#
# These are the maximum number of orders allowed in the database per environment.
# Once the limit is reached, the simulator stops creating new orders but
# continues advancing existing ones through their lifecycle. This keeps
# resource usage predictable and cost-controlled across environments.


@dataclass(frozen=True)
class EnvironmentLimits:
    max_orders: int
    seed_customers: int
    seed_products: int
    seed_historical_orders: int


_ENVIRONMENT_LIMITS: Final[dict[str, EnvironmentLimits]] = {
    Environment.DEV: EnvironmentLimits(
        max_orders=5_000,
        seed_customers=500,
        seed_products=200,
        seed_historical_orders=2_000,
    ),
    Environment.STAGING: EnvironmentLimits(
        max_orders=10_000,
        seed_customers=1_000,
        seed_products=400,
        seed_historical_orders=5_000,
    ),
    Environment.PROD: EnvironmentLimits(
        max_orders=15_000,
        seed_customers=2_000,
        seed_products=800,
        seed_historical_orders=10_000,
    ),
}


def get_environment() -> str:
    """
    Read and validate the ENVIRONMENT variable.

    Raises ConfigurationError if the value is missing or not one of
    dev / staging / prod. Never returns an invalid value.
    """
    value = os.getenv("ENVIRONMENT", "").strip().lower()
    if not value:
        raise ConfigurationError(
            "ENVIRONMENT variable is not set. "
            f"Set it to one of: {sorted(Environment.ALL)}"
        )
    if value not in Environment.ALL:
        raise ConfigurationError(
            f"ENVIRONMENT={value!r} is not valid. "
            f"Must be one of: {sorted(Environment.ALL)}"
        )
    return value


def get_environment_limits(environment: str) -> EnvironmentLimits:
    """Return the record limits for the given environment."""
    if environment not in Environment.ALL:
        raise ConfigurationError(
            f"Cannot get limits for unknown environment {environment!r}. "
            f"Must be one of: {sorted(Environment.ALL)}"
        )
    return _ENVIRONMENT_LIMITS[environment]


# ── Domain constants ──────────────────────────────────────────────────────────
#
# Each class groups related string constants for a single domain concept.
# Use OrderStatus.PENDING instead of the string "pending" everywhere.
# This makes refactors safe and keeps IDEs happy.


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
    WEIGHTS: Final = [20, 25, 10, 10, 10, 10, 8, 7]


class ProductBrand:
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

CANCELLATION_RATE: Final[float] = 0.05
REFUND_RATE: Final[float] = 0.03
MAX_ITEMS_PER_ORDER: Final[int] = 6


# ── Config dataclasses ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str

    @classmethod
    def from_env(cls) -> DatabaseConfig:
        missing = [v for v in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")
                   if not os.getenv(v)]
        if missing:
            raise ConfigurationError(
                f"Missing required environment variables: {missing}. "
                "Copy .env.example to .env and fill in your values."
            )
        return cls(
            host=os.environ["DB_HOST"],
            port=int(os.getenv("DB_PORT", "5432")),
            dbname=os.environ["DB_NAME"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
        )

    def dsn(self) -> str:
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
    def from_env(cls, limits: EnvironmentLimits) -> SeedConfig:
        """
        Load seed config from env vars, falling back to per-environment defaults.

        This means dev automatically seeds 500 customers, staging seeds 1000,
        and prod seeds 2000 — without you having to remember to change the values.
        """
        return cls(
            num_customers=int(os.getenv("SEED_CUSTOMERS", str(limits.seed_customers))),
            num_products=int(os.getenv("SEED_PRODUCTS", str(limits.seed_products))),
            num_historical_orders=int(
                os.getenv("SEED_HISTORICAL_ORDERS", str(limits.seed_historical_orders))
            ),
            random_seed=int(os.getenv("SEED_RANDOM_SEED", "42")),
        )


@dataclass(frozen=True)
class SimulationConfig:
    tick_interval_seconds: float
    new_orders_per_tick: int
    max_orders: int

    @classmethod
    def from_env(cls, limits: EnvironmentLimits) -> SimulationConfig:
        """
        Load simulation config, using the environment's max_orders limit as default.

        The max_orders limit stops the simulator from creating new orders once
        the environment ceiling is reached (dev=5000, staging=10000, prod=15000).
        You can override any value via environment variables.
        """
        return cls(
            tick_interval_seconds=float(os.getenv("SIM_TICK_INTERVAL_SECONDS", "2")),
            new_orders_per_tick=int(os.getenv("SIM_NEW_ORDERS_PER_TICK", "3")),
            max_orders=int(os.getenv("SIM_MAX_ORDERS", str(limits.max_orders))),
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
