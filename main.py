"""
CDC Simulator — CLI entry point.

Commands:
    schema      Create (or recreate) all tables, indexes, triggers, and
                set REPLICA IDENTITY FULL. Safe to run on an empty database.
    seed        Populate the database with historical data using the counts
                from environment variables (or .env file).
    simulate    Start the live simulation loop. Runs until Ctrl+C.
    reset       Drop all tables, recreate the schema, then reseed.
                WARNING: this destroys all existing data.

Usage:
    python main.py schema
    python main.py seed
    python main.py simulate
    python main.py reset
    python main.py simulate --log-level DEBUG

The simulator reads all configuration from environment variables. Copy
.env.example to .env and fill in your database credentials before running.
If you are connecting to the RDS instance inside the private VPC, use an
SSM port-forwarding tunnel — see the README for instructions.
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
)
from simulator.db import DatabaseManager
from simulator.schema import ALL_CREATE_STATEMENTS, ALL_DROP_STATEMENTS
from simulator.seed import Seeder
from simulator.simulate import Simulator


def cmd_schema(db: DatabaseManager) -> None:
    """Create the full schema."""
    for sql in ALL_CREATE_STATEMENTS:
        with db.cursor() as cur:
            cur.execute(sql)
    print("Schema created successfully.")


def cmd_seed(db: DatabaseManager, seed_config: SeedConfig) -> None:
    """Seed historical data."""
    seeder = Seeder(db, seed_config)
    seeder.run()
    print("Seeding complete.")


def cmd_simulate(db: DatabaseManager, sim_config: SimulationConfig) -> None:
    """Run the live simulation loop (blocks until Ctrl+C)."""
    sim = Simulator(db, sim_config)
    sim.run()


def cmd_reset(db: DatabaseManager, seed_config: SeedConfig) -> None:
    """Drop all tables, recreate the schema, and reseed. Destroys all data."""
    print("Resetting: dropping all tables...")
    for sql in ALL_DROP_STATEMENTS:
        with db.cursor() as cur:
            cur.execute(sql)
    print("Tables dropped. Recreating schema...")
    cmd_schema(db)
    print("Schema recreated. Seeding...")
    cmd_seed(db, seed_config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cdc-simulator",
        description="Generate realistic OLTP activity for AWS DMS CDC testing.",
    )
    parser.add_argument(
        "command",
        choices=["schema", "seed", "simulate", "reset"],
        help=(
            "schema: create tables and set REPLICA IDENTITY FULL. "
            "seed: populate with historical data. "
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

    logger = logging.getLogger(__name__)

    # Load all config from environment variables. Missing required vars
    # (DB_HOST, DB_NAME, DB_USER, DB_PASSWORD) raise KeyError with a clear
    # message pointing to the missing variable name.
    try:
        db_config = DatabaseConfig.from_env()
        retry_config = RetryConfig.from_env()
        seed_config = SeedConfig.from_env()
        sim_config = SimulationConfig.from_env()
    except KeyError as exc:
        logger.error(
            "Missing required environment variable: %s. "
            "Copy .env.example to .env and fill in your values.",
            exc,
        )
        return 1

    logger.info("Using %r", db_config)

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
        else:
            # argparse choices enforcement means we never reach here
            logger.error("Unknown command: %s", command)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
