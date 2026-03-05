"""
Integration tests for the DatabaseManager.

These tests require a live PostgreSQL connection. They are marked with
@pytest.mark.integration so they can be skipped in environments without
a database (e.g. a quick local lint check).

Run with:
    make test-integration          # integration tests only
    make test                      # all tests including integration

In CI, GitHub Actions spins up a PostgreSQL service container automatically
so these tests always run there.
"""

from __future__ import annotations

import pytest
import psycopg2

from simulator.db import DatabaseManager
from simulator.exceptions import DatabaseConnectionError


# ── Basic connectivity ─────────────────────────────────────────────────────────


@pytest.mark.integration
class TestDatabaseManagerConnect:
    def test_connect_succeeds(self, db):
        """db fixture already calls connect() — if we reach here, it worked."""
        assert db is not None

    def test_connection_is_open(self, db):
        assert db._conn is not None
        assert not db._conn.closed

    def test_close_marks_connection_none(self, db):
        db.close()
        assert db._conn is None

    def test_invalid_host_raises_database_connection_error(self, retry_config):
        from simulator.config import DatabaseConfig
        bad_config = DatabaseConfig(
            host="nonexistent-host-that-does-not-exist",
            port=5432,
            dbname="test",
            user="test",
            password="test",
        )
        with pytest.raises(DatabaseConnectionError):
            with DatabaseManager(bad_config, retry_config) as _:
                pass


# ── Execute ────────────────────────────────────────────────────────────────────


@pytest.mark.integration
class TestExecute:
    def test_execute_insert_returns_rowcount(self, db):
        rows = db.execute(
            "INSERT INTO customers (first_name, last_name, email, country, phone, signup_date) "
            "VALUES (%s,%s,%s,%s,%s,now())",
            ("Test", "User", "test.execute@example.com", "Germany", "+49123"),
        )
        assert rows == 1

    def test_execute_update_returns_affected_rows(self, db):
        db.execute(
            "INSERT INTO customers (first_name, last_name, email, country, phone, signup_date) "
            "VALUES (%s,%s,%s,%s,%s,now())",
            ("Alice", "Smith", "alice.update@example.com", "France", "+33999"),
        )
        rows = db.execute(
            "UPDATE customers SET country = %s WHERE email = %s",
            ("Spain", "alice.update@example.com"),
        )
        assert rows == 1

    def test_execute_no_match_returns_zero(self, db):
        rows = db.execute(
            "UPDATE customers SET country = 'Italy' WHERE email = 'nobody@nowhere.com'"
        )
        assert rows == 0


# ── Execute many ───────────────────────────────────────────────────────────────


@pytest.mark.integration
class TestExecuteMany:
    def test_inserts_multiple_rows(self, db):
        rows = [
            ("Bob", "Jones", f"bob.{i}@example.com", "Germany", f"+49{i}", "2024-01-01")
            for i in range(10)
        ]
        db.execute_many(
            "INSERT INTO customers (first_name, last_name, email, country, phone, signup_date) "
            "VALUES %s",
            rows,
        )
        count = db.fetch_one("SELECT COUNT(*) FROM customers WHERE first_name = 'Bob'")
        assert count is not None
        assert count[0] == 10

    def test_empty_list_returns_zero(self, db):
        result = db.execute_many(
            "INSERT INTO customers (first_name, last_name, email, country, phone, signup_date) "
            "VALUES %s",
            [],
        )
        assert result == 0


# ── Fetch helpers ──────────────────────────────────────────────────────────────


@pytest.mark.integration
class TestFetchHelpers:
    def _seed_customer(self, db, email: str = "fetch.test@example.com") -> None:
        db.execute(
            "INSERT INTO customers (first_name, last_name, email, country, phone, signup_date) "
            "VALUES (%s,%s,%s,%s,%s,now())",
            ("Fetch", "Test", email, "Germany", "+49000"),
        )

    def test_fetch_one_returns_tuple(self, db):
        self._seed_customer(db)
        row = db.fetch_one("SELECT first_name FROM customers WHERE email = %s",
                           ("fetch.test@example.com",))
        assert row is not None
        assert row[0] == "Fetch"

    def test_fetch_one_returns_none_on_no_match(self, db):
        row = db.fetch_one("SELECT * FROM customers WHERE email = 'nobody@nothing.com'")
        assert row is None

    def test_fetch_all_returns_list(self, db):
        self._seed_customer(db, "all1@example.com")
        self._seed_customer(db, "all2@example.com")
        rows = db.fetch_all(
            "SELECT email FROM customers WHERE email IN (%s, %s)",
            ("all1@example.com", "all2@example.com"),
        )
        assert isinstance(rows, list)
        assert len(rows) == 2

    def test_fetch_column_returns_flat_list(self, db):
        self._seed_customer(db, "col1@example.com")
        self._seed_customer(db, "col2@example.com")
        emails = db.fetch_column(
            "SELECT email FROM customers WHERE email IN (%s, %s) ORDER BY email",
            ("col1@example.com", "col2@example.com"),
        )
        assert emails == ["col1@example.com", "col2@example.com"]


# ── Transaction rollback ───────────────────────────────────────────────────────


@pytest.mark.integration
class TestTransactionRollback:
    def test_exception_in_cursor_rolls_back(self, db):
        """
        If an exception is raised inside a cursor block, the transaction must
        be rolled back. The database should be in the same state as before.
        """
        count_before = db.fetch_one("SELECT COUNT(*) FROM customers")[0]

        with pytest.raises(psycopg2.Error):
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO customers (first_name, last_name, email, country, phone, signup_date) "
                    "VALUES (%s,%s,%s,%s,%s,now())",
                    ("Rollback", "Test", "rollback@example.com", "UK", "+44"),
                )
                # This will fail — intentional bad SQL to trigger rollback
                cur.execute("THIS IS NOT VALID SQL AND WILL FAIL")

        count_after = db.fetch_one("SELECT COUNT(*) FROM customers")[0]
        assert count_after == count_before, (
            "Row count changed after a failed transaction — rollback did not work"
        )
