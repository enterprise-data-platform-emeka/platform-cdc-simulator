"""
Database connection management.

DatabaseManager wraps psycopg2 and provides:
- Automatic reconnection with exponential backoff (via tenacity) so the
  simulator survives transient network blips or a brief RDS restart.
- A context manager for cursors that auto-commits on success and rolls back
  on any exception, preventing partial writes from leaking into the WAL as
  incomplete transactions.
- Bulk insert helpers that use execute_values for efficient multi-row inserts.
- A simple query helper for read operations.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Generator, Sequence

import psycopg2
import psycopg2.extras
from tenacity import (
    after_log,
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from simulator.config import DatabaseConfig, RetryConfig

logger = logging.getLogger(__name__)


class DatabaseManager:
    """
    Manages a single psycopg2 connection to PostgreSQL.

    Usage:

        db = DatabaseManager(db_config, retry_config)
        db.connect()

        with db.cursor() as cur:
            cur.execute("SELECT 1")

        db.close()

    Or as a one-shot context manager:

        with DatabaseManager(db_config, retry_config) as db:
            with db.cursor() as cur:
                cur.execute("SELECT 1")
    """

    def __init__(self, db_config: DatabaseConfig, retry_config: RetryConfig) -> None:
        self._db_config = db_config
        self._retry_config = retry_config
        self._conn: psycopg2.extensions.connection | None = None

    # ── Connection lifecycle ──────────────────────────────────────────────────

    def connect(self) -> None:
        """
        Open a connection to PostgreSQL, retrying with exponential backoff if
        the server is temporarily unavailable.

        This is called explicitly (not lazily) so startup errors surface
        immediately rather than on the first query.
        """
        self._connect_with_retry()

    @property
    def _retry_decorator(self):
        """
        Build a tenacity retry decorator from the current RetryConfig.

        Defined as a property so it always reflects the live config values.
        """
        return retry(
            retry=retry_if_exception_type((psycopg2.OperationalError, psycopg2.InterfaceError)),
            stop=stop_after_attempt(self._retry_config.max_attempts),
            wait=wait_exponential(
                multiplier=1,
                min=self._retry_config.wait_min_seconds,
                max=self._retry_config.wait_max_seconds,
            ),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            after=after_log(logger, logging.INFO),
            reraise=True,
        )

    def _connect_with_retry(self) -> None:
        """Internal: attempt connection, with tenacity retry applied at call time."""

        @self._retry_decorator
        def _attempt() -> None:
            logger.info("Connecting to PostgreSQL at %s:%s/%s",
                        self._db_config.host, self._db_config.port, self._db_config.dbname)
            self._conn = psycopg2.connect(self._db_config.dsn())
            # Autocommit is OFF by default in psycopg2. We manage transactions
            # explicitly via the cursor context manager below.
            self._conn.autocommit = False
            logger.info("Connected to PostgreSQL")

        _attempt()

    def close(self) -> None:
        """Close the connection if open."""
        if self._conn and not self._conn.closed:
            self._conn.close()
            logger.info("PostgreSQL connection closed")
            self._conn = None

    def _ensure_connected(self) -> None:
        """
        Reconnect if the connection was dropped.

        psycopg2 sets connection.closed = 1 (or 2) when the connection is lost.
        We reconnect transparently so the caller does not have to handle this.
        """
        if self._conn is None or self._conn.closed:
            logger.warning("Connection lost — reconnecting")
            self._connect_with_retry()

    # ── Cursor context manager ────────────────────────────────────────────────

    @contextmanager
    def cursor(self) -> Generator[psycopg2.extensions.cursor, None, None]:
        """
        Yield a cursor within a transaction.

        Commits on clean exit, rolls back on any exception. This guarantees
        that the caller never accidentally leaves the connection in a failed
        transaction state.

        Example:

            with db.cursor() as cur:
                cur.execute("UPDATE orders SET status = %s WHERE order_id = %s",
                            ("confirmed", 42))
        """
        self._ensure_connected()
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    # ── Write helpers ─────────────────────────────────────────────────────────

    def execute_many(
        self,
        sql: str,
        rows: Sequence[tuple[Any, ...]],
        page_size: int = 1000,
    ) -> int:
        """
        Insert or update multiple rows using psycopg2's execute_values.

        execute_values is significantly faster than calling execute() in a loop
        because it batches rows into a single multi-row VALUES clause, reducing
        round-trips to the server and WAL write amplification.

        Args:
            sql:       An INSERT statement with a %s placeholder for the values,
                       e.g. "INSERT INTO customers (name, email) VALUES %s"
            rows:      A sequence of tuples, one per row.
            page_size: Number of rows per batch. 1000 is a safe default.

        Returns:
            The number of rows affected (rowcount of the final batch).
        """
        if not rows:
            return 0

        with self.cursor() as cur:
            psycopg2.extras.execute_values(cur, sql, rows, page_size=page_size)
            return cur.rowcount

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> int:
        """
        Execute a single DML statement (INSERT, UPDATE, DELETE).

        Returns the number of rows affected.
        """
        with self.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    # ── Read helpers ──────────────────────────────────────────────────────────

    def fetch_all(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
    ) -> list[tuple[Any, ...]]:
        """
        Execute a SELECT and return all rows as a list of tuples.

        The result set must fit in memory. For large tables use fetch_column
        or iterate with a server-side cursor.
        """
        with self.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def fetch_column(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
        col: int = 0,
    ) -> list[Any]:
        """
        Execute a SELECT and return a single column as a flat list.

        Useful for fetching IDs: `db.fetch_column("SELECT order_id FROM orders")`
        """
        rows = self.fetch_all(sql, params)
        return [row[col] for row in rows]

    def fetch_one(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
    ) -> tuple[Any, ...] | None:
        """Execute a SELECT and return the first row, or None if no rows."""
        with self.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    # ── Context manager support ───────────────────────────────────────────────

    def __enter__(self) -> DatabaseManager:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
