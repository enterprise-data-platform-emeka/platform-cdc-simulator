"""
CDC Simulator: CLI entry point.

Commands:
    bootstrap   Idempotently create the schema and seed historical data only
                when the source database is empty. Safe for promoted workflows.
    schema      Create all tables, indexes, triggers, and set REPLICA IDENTITY FULL.
                Safe to run on an empty database. Fails loudly if tables already exist
                and the schema differs. Use reset to start fresh.
    seed        Populate the database with historical data. The amount of data
                seeded depends on the ENVIRONMENT variable (dev/staging/prod).
    simulate    Start the live simulation loop. Runs until Ctrl+C or the optional
                --duration-seconds value elapses.
                Respects the per-environment order limit.
    reset       Drop all tables, recreate the schema, then reseed.
                WARNING: destroys all existing data.

Usage:
    python main.py bootstrap
    python main.py schema
    python main.py seed
    python main.py simulate
    python main.py reset
    python main.py simulate --duration-seconds 600 --log-level DEBUG

Environment variables:
    ENVIRONMENT   dev | staging | prod  (required, drives record limits)
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
from simulator.exceptions import ConfigurationError, SchemaError, SimulatorError
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


def _table_exists(db: DatabaseManager, table_name: str) -> bool:
    row = db.fetch_one(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = %s
        )
        """,
        (table_name,),
    )
    return bool(row and row[0])


def cmd_bootstrap(db: DatabaseManager, seed_config: SeedConfig) -> None:
    """
    Idempotently prepare an empty source database.

    This is the command CI/CD should use. It creates the schema when tables do
    not exist and seeds only when the core customer table is empty. It never
    drops existing data.
    """
    if not _table_exists(db, "customers"):
        logger.info("No source schema detected — creating schema")
        cmd_schema(db)
    else:
        logger.info("Source schema already exists — leaving it in place")

    row = db.fetch_one("SELECT COUNT(*) FROM customers")
    customer_count = int(row[0]) if row else 0
    if customer_count == 0:
        logger.info("Source tables are empty — seeding historical data")
        cmd_seed(db, seed_config)
    else:
        logger.info(
            "Source tables already contain %d customers — skipping seed",
            customer_count,
        )


def cmd_simulate(
    db: DatabaseManager,
    sim_config: SimulationConfig,
    duration_seconds: int | None = None,
) -> None:
    """Run the live simulation loop (blocks until Ctrl+C or unrecoverable error)."""
    sim = Simulator(db, sim_config)
    sim.run(duration_seconds=duration_seconds)


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
        choices=["bootstrap", "schema", "seed", "simulate", "reset"],
        help=(
            "bootstrap: create schema and seed only when empty. "
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
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=None,
        help=("Only for simulate: stop after this many seconds. " "Omit to run until interrupted."),
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
            if command == "bootstrap":
                cmd_bootstrap(db, seed_config)
            elif command == "schema":
                cmd_schema(db)
            elif command == "seed":
                cmd_seed(db, seed_config)
            elif command == "simulate":
                if args.duration_seconds is not None and args.duration_seconds <= 0:
                    raise ConfigurationError("--duration-seconds must be greater than 0")
                cmd_simulate(db, sim_config, duration_seconds=args.duration_seconds)
            elif command == "reset":
                cmd_reset(db, seed_config)
    except SimulatorError as exc:
        # Known failure: log the message and exit cleanly
        logger.error("%s: %s", type(exc).__name__, exc)
        return 1
    except Exception as exc:
        # Unknown failure: log with full traceback so it can be debugged
        logger.exception("Unexpected error: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
