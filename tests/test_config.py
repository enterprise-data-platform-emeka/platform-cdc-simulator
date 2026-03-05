"""
Tests for configuration classes and domain constants.

These are pure unit tests — no database, no network, no file I/O.
They verify that:
- Domain constants are self-consistent (e.g. TERMINAL and LIFECYCLE agree).
- Per-environment limits have correct values and correct ordering.
- Config dataclasses load from environment variables correctly.
- Sensitive values (passwords) are never included in repr output.
- ConfigurationError is raised for invalid or missing configuration.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from simulator.config import (
    CATEGORY_PRICE_RANGE,
    CUSTOMER_COUNTRIES,
    CUSTOMER_COUNTRY_WEIGHTS,
    DatabaseConfig,
    DeliveryStatus,
    Environment,
    EnvironmentLimits,
    OrderStatus,
    PaymentMethod,
    ProductCategory,
    RetryConfig,
    SeedConfig,
    SimulationConfig,
    get_environment,
    get_environment_limits,
)
from simulator.exceptions import ConfigurationError


# ── OrderStatus ───────────────────────────────────────────────────────────────


class TestOrderStatus:
    def test_lifecycle_starts_with_pending(self):
        assert OrderStatus.LIFECYCLE[0] == OrderStatus.PENDING

    def test_lifecycle_ends_with_delivered(self):
        assert OrderStatus.LIFECYCLE[-1] == OrderStatus.DELIVERED

    def test_terminal_does_not_overlap_lifecycle(self):
        """No status should be both in-progress and terminal."""
        lifecycle_set = set(OrderStatus.LIFECYCLE[:-1])  # exclude DELIVERED
        assert lifecycle_set.isdisjoint(OrderStatus.TERMINAL - {OrderStatus.DELIVERED})

    def test_delivered_is_in_terminal(self):
        assert OrderStatus.DELIVERED in OrderStatus.TERMINAL

    def test_cancelled_is_in_terminal(self):
        assert OrderStatus.CANCELLED in OrderStatus.TERMINAL

    def test_refunded_is_in_terminal(self):
        assert OrderStatus.REFUNDED in OrderStatus.TERMINAL

    def test_pending_is_not_terminal(self):
        assert OrderStatus.PENDING not in OrderStatus.TERMINAL


# ── DeliveryStatus ────────────────────────────────────────────────────────────


class TestDeliveryStatus:
    def test_lifecycle_starts_with_pending(self):
        assert DeliveryStatus.LIFECYCLE[0] == DeliveryStatus.PENDING

    def test_lifecycle_ends_with_delivered(self):
        assert DeliveryStatus.LIFECYCLE[-1] == DeliveryStatus.DELIVERED

    def test_terminal_includes_delivered(self):
        assert DeliveryStatus.DELIVERED in DeliveryStatus.TERMINAL

    def test_terminal_includes_failed_delivery(self):
        assert DeliveryStatus.FAILED_DELIVERY in DeliveryStatus.TERMINAL


# ── PaymentMethod ─────────────────────────────────────────────────────────────


class TestPaymentMethod:
    def test_weights_match_all_list_length(self):
        assert len(PaymentMethod.ALL) == len(PaymentMethod.WEIGHTS)

    def test_weights_sum_to_100(self):
        assert sum(PaymentMethod.WEIGHTS) == 100


# ── ProductCategory ───────────────────────────────────────────────────────────


class TestProductCategory:
    def test_weights_match_all_list_length(self):
        assert len(ProductCategory.ALL) == len(ProductCategory.WEIGHTS)

    def test_every_category_has_price_range(self):
        for category in ProductCategory.ALL:
            assert category in CATEGORY_PRICE_RANGE, (
                f"Category {category!r} has no price range in CATEGORY_PRICE_RANGE"
            )

    def test_price_ranges_are_positive_and_ordered(self):
        for category, (lo, hi) in CATEGORY_PRICE_RANGE.items():
            assert lo > 0, f"{category}: min price must be > 0"
            assert hi > lo, f"{category}: max price must be > min price"


# ── Country weights ───────────────────────────────────────────────────────────


class TestCountryWeights:
    def test_country_and_weight_lists_same_length(self):
        assert len(CUSTOMER_COUNTRIES) == len(CUSTOMER_COUNTRY_WEIGHTS)

    def test_all_weights_positive(self):
        assert all(w > 0 for w in CUSTOMER_COUNTRY_WEIGHTS)


# ── Environment limits ────────────────────────────────────────────────────────


class TestEnvironmentLimits:
    def test_dev_limits(self):
        limits = get_environment_limits(Environment.DEV)
        assert limits.max_orders == 5_000
        assert limits.seed_customers == 500
        assert limits.seed_products == 200
        assert limits.seed_historical_orders == 2_000

    def test_staging_limits(self):
        limits = get_environment_limits(Environment.STAGING)
        assert limits.max_orders == 10_000
        assert limits.seed_customers == 1_000
        assert limits.seed_products == 400
        assert limits.seed_historical_orders == 5_000

    def test_prod_limits(self):
        limits = get_environment_limits(Environment.PROD)
        assert limits.max_orders == 15_000
        assert limits.seed_customers == 2_000
        assert limits.seed_products == 800
        assert limits.seed_historical_orders == 10_000

    def test_limits_increase_from_dev_to_prod(self):
        dev = get_environment_limits(Environment.DEV)
        staging = get_environment_limits(Environment.STAGING)
        prod = get_environment_limits(Environment.PROD)
        assert dev.max_orders < staging.max_orders < prod.max_orders
        assert dev.seed_customers < staging.seed_customers < prod.seed_customers

    def test_invalid_environment_raises(self):
        with pytest.raises(ConfigurationError, match="unknown environment"):
            get_environment_limits("production")


# ── get_environment ───────────────────────────────────────────────────────────


class TestGetEnvironment:
    def test_returns_dev(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "dev"}):
            assert get_environment() == "dev"

    def test_returns_staging(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
            assert get_environment() == "staging"

    def test_returns_prod(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "prod"}):
            assert get_environment() == "prod"

    def test_strips_whitespace(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "  dev  "}):
            assert get_environment() == "dev"

    def test_case_insensitive(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "DEV"}):
            assert get_environment() == "dev"

    def test_missing_env_var_raises(self):
        env = {k: v for k, v in os.environ.items() if k != "ENVIRONMENT"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigurationError, match="ENVIRONMENT variable is not set"):
                get_environment()

    def test_invalid_value_raises(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            with pytest.raises(ConfigurationError, match="not valid"):
                get_environment()


# ── DatabaseConfig ────────────────────────────────────────────────────────────


class TestDatabaseConfig:
    def _env(self) -> dict[str, str]:
        return {
            "DB_HOST": "myhost",
            "DB_PORT": "5433",
            "DB_NAME": "mydb",
            "DB_USER": "myuser",
            "DB_PASSWORD": "supersecret",
        }

    def test_loads_from_env(self):
        with patch.dict(os.environ, self._env()):
            cfg = DatabaseConfig.from_env()
        assert cfg.host == "myhost"
        assert cfg.port == 5433
        assert cfg.dbname == "mydb"
        assert cfg.user == "myuser"
        assert cfg.password == "supersecret"

    def test_default_port_is_5432(self):
        env = {k: v for k, v in self._env().items() if k != "DB_PORT"}
        with patch.dict(os.environ, env):
            cfg = DatabaseConfig.from_env()
        assert cfg.port == 5432

    def test_repr_hides_password(self):
        with patch.dict(os.environ, self._env()):
            cfg = DatabaseConfig.from_env()
        assert "supersecret" not in repr(cfg)
        assert "***" in repr(cfg)

    def test_dsn_contains_password(self):
        """dsn() is used internally for the connection — it must include the password."""
        with patch.dict(os.environ, self._env()):
            cfg = DatabaseConfig.from_env()
        assert "supersecret" in cfg.dsn()

    def test_missing_required_var_raises(self):
        env = {k: v for k, v in self._env().items() if k != "DB_HOST"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigurationError, match="Missing required"):
                DatabaseConfig.from_env()


# ── SeedConfig ────────────────────────────────────────────────────────────────


class TestSeedConfig:
    def test_uses_environment_defaults_when_no_env_vars(self, dev_limits):
        env = {k: v for k, v in os.environ.items()
               if k not in ("SEED_CUSTOMERS", "SEED_PRODUCTS",
                            "SEED_HISTORICAL_ORDERS", "SEED_RANDOM_SEED")}
        with patch.dict(os.environ, env, clear=True):
            cfg = SeedConfig.from_env(dev_limits)
        assert cfg.num_customers == dev_limits.seed_customers
        assert cfg.num_products == dev_limits.seed_products
        assert cfg.num_historical_orders == dev_limits.seed_historical_orders

    def test_env_vars_override_defaults(self, dev_limits):
        with patch.dict(os.environ, {"SEED_CUSTOMERS": "999"}):
            cfg = SeedConfig.from_env(dev_limits)
        assert cfg.num_customers == 999

    def test_default_random_seed_is_42(self, dev_limits):
        env = {k: v for k, v in os.environ.items() if k != "SEED_RANDOM_SEED"}
        with patch.dict(os.environ, env, clear=True):
            cfg = SeedConfig.from_env(dev_limits)
        assert cfg.random_seed == 42


# ── SimulationConfig ──────────────────────────────────────────────────────────


class TestSimulationConfig:
    def test_uses_environment_max_orders_by_default(self, dev_limits):
        env = {k: v for k, v in os.environ.items() if k != "SIM_MAX_ORDERS"}
        with patch.dict(os.environ, env, clear=True):
            cfg = SimulationConfig.from_env(dev_limits)
        assert cfg.max_orders == dev_limits.max_orders

    def test_env_var_overrides_max_orders(self, dev_limits):
        with patch.dict(os.environ, {"SIM_MAX_ORDERS": "100"}):
            cfg = SimulationConfig.from_env(dev_limits)
        assert cfg.max_orders == 100

    def test_default_tick_interval_is_2_seconds(self, dev_limits):
        env = {k: v for k, v in os.environ.items() if k != "SIM_TICK_INTERVAL_SECONDS"}
        with patch.dict(os.environ, env, clear=True):
            cfg = SimulationConfig.from_env(dev_limits)
        assert cfg.tick_interval_seconds == 2.0
