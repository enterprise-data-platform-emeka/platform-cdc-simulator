"""
Shared pytest fixtures.

Unit test fixtures (no marker): available to all tests, no database required.
Integration fixtures (pytest.mark.integration): require a live PostgreSQL connection.

The integration fixtures read DB connection details from environment variables,
the same way the application does. In CI, these are set by the workflow.
Locally, set them in your .env file or export them in your terminal.
"""

from __future__ import annotations

import os

import pytest

from simulator.config import DatabaseConfig, EnvironmentLimits, RetryConfig, SeedConfig, SimulationConfig
from simulator.db import DatabaseManager
from simulator.schema import ALL_CREATE_STATEMENTS, ALL_DROP_STATEMENTS


# ── Shared config fixtures (no DB required) ───────────────────────────────────


@pytest.fixture(scope="session")
def dev_limits() -> EnvironmentLimits:
    return EnvironmentLimits(
        max_orders=5_000,
        seed_customers=500,
        seed_products=200,
        seed_historical_orders=2_000,
    )


@pytest.fixture(scope="session")
def retry_config() -> RetryConfig:
    """Fast retry config for tests — don't wait 30 seconds between attempts."""
    return RetryConfig(max_attempts=3, wait_min_seconds=0.1, wait_max_seconds=1.0)


@pytest.fixture(scope="session")
def db_config() -> DatabaseConfig:
    """
    Read DB config from environment for integration tests.

    Uses TEST_DB_NAME (not DB_NAME) so the test suite always connects to the
    dedicated ecommerce_test database, never the main ecommerce database.
    This means running the full test suite can never wipe simulator data.
    """
    return DatabaseConfig(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("TEST_DB_NAME", "ecommerce_test"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "localpass"),
    )


# ── Integration fixtures (require live PostgreSQL) ────────────────────────────


@pytest.fixture(scope="function")
def db(db_config: DatabaseConfig, retry_config: RetryConfig) -> DatabaseManager:
    """
    Provide a connected DatabaseManager for integration tests.

    Creates a fresh schema before each test and drops it after.
    This means every integration test starts with a clean database.
    """
    with DatabaseManager(db_config, retry_config) as database:
        # Set up schema
        for sql in ALL_CREATE_STATEMENTS:
            with database.cursor() as cur:
                cur.execute(sql)
        yield database
        # Tear down schema
        for sql in ALL_DROP_STATEMENTS:
            with database.cursor() as cur:
                cur.execute(sql)
