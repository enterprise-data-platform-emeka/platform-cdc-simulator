"""
CDC Simulator — CLI entry point.

Commands:
    schema      Create all tables, indexes, triggers, and set REPLICA IDENTITY FULL.
                Safe to run on an empty database. Fails loudly if tables already exist
                and the schema differs — use reset to start fresh.
    seed        Populate the database with historical data. The amount of data
                seeded depends on the ENVIRONMENT variable (dev/staging/prod).
    simulate    Start the live simulation loop. Runs until Ctrl+C.
                Respects the per-environment order limit.
    reset       Drop all tables, recreate the schema, then reseed.
                WARNING: destroys all existing data.

Usage:
    python main.py schema
    python main.py seed
    python main.py simulate
    python main.py reset
    python main.py simulate --log-level DEBUG

Environment variables:
    ENVIRONMENT   dev | staging | prod  (required — drives record limits)
    DB_HOST       PostgreSQL hostname   (required)
    DB_NAME       Database name        (required)
    DB_USER       Database user        (required)
    DB_PASSWORD   Database password    (required)

Copy .env.example to .env and fill in your values before running.
"""

from __future__ import annotations

import argparse
import logging
import sys

from simulator.config import (
    DatabaseConfig,
    RetryConfig,
    SeedConfig,
    SimulationConfig,
    configure_logging,
    get_environment,
    get_environment_limits,
)
from simulator.db import DatabaseManager
from simulator.exceptions import ConfigurationError, SchemaError, SeedError, SimulatorError
from simulator.schema import ALL_CREATE_STATEMENTS, ALL_DROP_STATEMENTS
from simulator.seed import Seeder
from simulator.simulate import Simulator

logger = logging.getLogger(__name__)


# ── Command implementations ───────────────────────────────────────────────────


def cmd_schema(db: DatabaseManager) -> None:
    """Create the full schema. Raises SchemaError on failure."""
    logger.info("Applying schema")
    for sql in ALL_CREATE_STATEMENTS:
        try:
            with db.cursor() as cur:
                cur.execute(sql)
        except Exception as exc:
            raise SchemaError(f"Schema creation failed: {exc}") from exc
    logger.info("Schema applied successfully")


def cmd_seed(db: DatabaseManager, seed_config: SeedConfig) -> None:
    """Seed historical data. Raises SeedError on failure."""
    seeder = Seeder(db, seed_config)
    seeder.run()


def cmd_simulate(db: DatabaseManager, sim_config: SimulationConfig) -> None:
    """Run the live simulation loop (blocks until Ctrl+C or unrecoverable error)."""
    sim = Simulator(db, sim_config)
    sim.run()


def cmd_reset(db: DatabaseManager, seed_config: SeedConfig) -> None:
    """Drop all tables, recreate the schema, and reseed. Destroys all data."""
    logger.warning("Resetting: dropping all tables — all data will be lost")
    for sql in ALL_DROP_STATEMENTS:
        try:
            with db.cursor() as cur:
                cur.execute(sql)
        except Exception as exc:
            raise SchemaError(f"Schema teardown failed: {exc}") from exc
    logger.info("Tables dropped")
    cmd_schema(db)
    cmd_seed(db, seed_config)


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cdc-simulator",
        description=(
            "Generate realistic OLTP activity for AWS DMS CDC testing. "
            "Requires ENVIRONMENT, DB_HOST, DB_NAME, DB_USER, DB_PASSWORD env vars."
        ),
    )
    parser.add_argument(
        "command",
        choices=["schema", "seed", "simulate", "reset"],
        help=(
            "schema: create tables. "
            "seed: populate historical data. "
            "simulate: run live traffic loop. "
            "reset: drop + recreate + reseed (destroys data)."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity (default: INFO)",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    log_level = getattr(logging, args.log_level)
    configure_logging(level=log_level)

    # ── Load and validate all configuration up front ──────────────────────────
    # We do this before opening a database connection so misconfiguration is
    # caught immediately with a clear error message, not buried in a traceback.
    try:
        environment = get_environment()
        limits = get_environment_limits(environment)
        db_config = DatabaseConfig.from_env()
        retry_config = RetryConfig.from_env()
        seed_config = SeedConfig.from_env(limits)
        sim_config = SimulationConfig.from_env(limits)
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        return 1

    logger.info(
        "Environment: %s | Max orders: %d | %r",
        environment,
        sim_config.max_orders,
        db_config,
    )

    # ── Run the requested command ─────────────────────────────────────────────
    try:
        with DatabaseManager(db_config, retry_config) as db:
            command = args.command
            if command == "schema":
                cmd_schema(db)
            elif command == "seed":
                cmd_seed(db, seed_config)
            elif command == "simulate":
                cmd_simulate(db, sim_config)
            elif command == "reset":
                cmd_reset(db, seed_config)
    except SimulatorError as exc:
        # Known failure — log the message and exit cleanly
        logger.error("%s: %s", type(exc).__name__, exc)
        return 1
    except Exception as exc:
        # Unknown failure — log with full traceback so it can be debugged
        logger.exception("Unexpected error: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
