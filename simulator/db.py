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

Error handling contract:
- psycopg2.OperationalError and InterfaceError (connection lost) →
  raised as DatabaseConnectionError so the caller knows to reconnect.
- All other psycopg2 errors (bad SQL, constraint violations, etc.) →
  logged at ERROR level with context, then re-raised as-is.
  The caller decides whether a constraint violation is acceptable or fatal.
- Nothing is swallowed silently.
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
from simulator.exceptions import DatabaseConnectionError

logger = logging.getLogger(__name__)

# psycopg2 error codes that indicate a lost or broken connection,
# not a SQL or data problem.
_CONNECTION_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)


class DatabaseManager:
    """
    Manages a single psycopg2 connection to PostgreSQL.

    Usage as a context manager (recommended):

        with DatabaseManager(db_config, retry_config) as db:
            with db.cursor() as cur:
                cur.execute("SELECT 1")

    Usage with explicit lifecycle:

        db = DatabaseManager(db_config, retry_config)
        db.connect()
        with db.cursor() as cur:
            cur.execute("SELECT 1")
        db.close()
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

        Raises DatabaseConnectionError if all retry attempts fail.
        """
        try:
            self._connect_with_retry()
        except _CONNECTION_ERRORS as exc:
            raise DatabaseConnectionError(
                f"Could not connect to PostgreSQL at {self._db_config.host}:"
                f"{self._db_config.port}/{self._db_config.dbname} "
                f"after {self._retry_config.max_attempts} attempts: {exc}"
            ) from exc

    def _connect_with_retry(self) -> None:
        """Internal: attempt connection with tenacity retry applied at call time."""

        @retry(
            retry=retry_if_exception_type(_CONNECTION_ERRORS),
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
        def _attempt() -> None:
            logger.info(
                "Connecting to PostgreSQL at %s:%s/%s",
                self._db_config.host,
                self._db_config.port,
                self._db_config.dbname,
            )
            self._conn = psycopg2.connect(self._db_config.dsn())
            self._conn.autocommit = False
            logger.info("Connected to PostgreSQL")

        _attempt()

    def close(self) -> None:
        """Close the connection if open. Safe to call multiple times."""
        if self._conn and not self._conn.closed:
            self._conn.close()
            logger.info("PostgreSQL connection closed")
            self._conn = None

    def _ensure_connected(self) -> None:
        """
        Reconnect if the connection was dropped.

        psycopg2 sets connection.closed to a non-zero value when the
        connection is lost. We reconnect transparently.
        """
        if self._conn is None or self._conn.closed:
            logger.warning("Connection lost — reconnecting")
            self.connect()

    # ── Cursor context manager ────────────────────────────────────────────────

    @contextmanager
    def cursor(self) -> Generator[psycopg2.extensions.cursor, None, None]:
        """
        Yield a cursor within a transaction.

        Commits on clean exit. Rolls back and re-raises on any exception.
        The caller always gets a clean connection state after this returns.

        Distinguishes between connection errors (raises DatabaseConnectionError)
        and SQL/data errors (re-raises the original psycopg2 exception with
        context logged at ERROR level).

        Example:

            with db.cursor() as cur:
                cur.execute(
                    "UPDATE orders SET status = %s WHERE order_id = %s",
                    ("confirmed", 42),
                )
        """
        self._ensure_connected()
        cur = self._conn.cursor()  # type: ignore[union-attr]
        try:
            yield cur
            self._conn.commit()  # type: ignore[union-attr]
        except _CONNECTION_ERRORS as exc:
            try:
                self._conn.rollback()  # type: ignore[union-attr]
            except Exception:
                pass  # connection is already broken; rollback will also fail
            raise DatabaseConnectionError(
                f"Database connection lost during operation: {exc}"
            ) from exc
        except psycopg2.Error as exc:
            logger.error(
                "Database error (rolled back): %s — %s",
                type(exc).__name__,
                exc,
            )
            try:
                self._conn.rollback()  # type: ignore[union-attr]
            except Exception:
                pass
            raise
        except Exception:
            try:
                self._conn.rollback()  # type: ignore[union-attr]
            except Exception:
                pass
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
        round-trips and WAL write amplification.

        Args:
            sql:       INSERT statement with a %s placeholder for the values.
                       e.g. "INSERT INTO customers (name, email) VALUES %s"
            rows:      A sequence of tuples, one per row.
            page_size: Number of rows per batch. 1000 is a safe default.

        Returns:
            The number of rows affected.

        Raises:
            DatabaseConnectionError: if the connection is lost.
            psycopg2.Error: if the SQL or data is invalid.
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

        Raises:
            DatabaseConnectionError: if the connection is lost.
            psycopg2.Error: if the SQL or data is invalid.
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

        Raises:
            DatabaseConnectionError: if the connection is lost.
            psycopg2.Error: if the SQL is invalid.
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

        Useful for fetching IDs: db.fetch_column("SELECT order_id FROM orders")
        """
        rows = self.fetch_all(sql, params)
        return [row[col] for row in rows]

    def fetch_one(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
    ) -> tuple[Any, ...] | None:
        """Execute a SELECT and return the first row, or None if no rows match."""
        with self.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    # ── Context manager support ───────────────────────────────────────────────

    def __enter__(self) -> DatabaseManager:
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
