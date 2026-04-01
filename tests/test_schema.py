"""
Tests for the SQL schema module.

These are pure unit tests. They verify the SQL strings are well-formed and
contain what they should, without executing them against a database.
Integration tests that actually create and drop the schema live in test_db.py.
"""

from __future__ import annotations

import pytest

from simulator.schema import (
    ALL_CREATE_STATEMENTS,
    ALL_DROP_STATEMENTS,
    CREATE_TABLES_SQL,
    DROP_TABLES_SQL,
    SET_REPLICA_IDENTITY_SQL,
)

# All six table names that must appear in the schema
ALL_TABLES = ["customers", "products", "orders", "order_items", "payments", "shipments"]


class TestCreateTablesSQL:
    def test_all_tables_are_created(self):
        for table in ALL_TABLES:
            assert table in CREATE_TABLES_SQL, (
                f"Table '{table}' not found in CREATE_TABLES_SQL"
            )

    def test_all_tables_have_primary_key(self):
        assert CREATE_TABLES_SQL.count("PRIMARY KEY") == len(ALL_TABLES)

    def test_updated_at_on_all_tables(self):
        """Every table must have an updated_at column for CDC change tracking."""
        assert CREATE_TABLES_SQL.count("updated_at") >= len(ALL_TABLES)

    def test_foreign_key_references_exist(self):
        assert "REFERENCES customers" in CREATE_TABLES_SQL
        assert "REFERENCES orders" in CREATE_TABLES_SQL
        assert "REFERENCES products" in CREATE_TABLES_SQL


class TestReplicaIdentitySQL:
    def test_all_tables_get_replica_identity_full(self):
        """
        REPLICA IDENTITY FULL is required on every table for DMS to capture
        complete UPDATE and DELETE events from the WAL.
        """
        for table in ALL_TABLES:
            assert table in SET_REPLICA_IDENTITY_SQL, (
                f"REPLICA IDENTITY FULL not set for table '{table}'"
            )

    def test_uses_full_not_default(self):
        """REPLICA IDENTITY DEFAULT only captures the PK. We need FULL."""
        assert "REPLICA IDENTITY FULL" in SET_REPLICA_IDENTITY_SQL
        assert "REPLICA IDENTITY DEFAULT" not in SET_REPLICA_IDENTITY_SQL


class TestDropTablesSQL:
    def test_all_tables_are_dropped(self):
        for table in ALL_TABLES:
            assert table in DROP_TABLES_SQL, (
                f"Table '{table}' not found in DROP_TABLES_SQL"
            )

    def test_uses_cascade(self):
        """CASCADE is required to drop tables with foreign key references."""
        assert "CASCADE" in DROP_TABLES_SQL

    def test_uses_if_exists(self):
        """IF EXISTS prevents errors when dropping on a clean database."""
        assert "IF EXISTS" in DROP_TABLES_SQL


class TestStatementCollections:
    def test_all_create_statements_is_tuple(self):
        """Tuple enforces ordered execution. A dict or set would not."""
        assert isinstance(ALL_CREATE_STATEMENTS, tuple)

    def test_all_drop_statements_is_tuple(self):
        assert isinstance(ALL_DROP_STATEMENTS, tuple)

    def test_create_statements_not_empty(self):
        assert len(ALL_CREATE_STATEMENTS) > 0

    def test_drop_statements_not_empty(self):
        assert len(ALL_DROP_STATEMENTS) > 0

    def test_replica_identity_is_in_create_statements(self):
        """REPLICA IDENTITY FULL must be applied as part of schema creation."""
        combined = " ".join(ALL_CREATE_STATEMENTS)
        assert "REPLICA IDENTITY FULL" in combined
