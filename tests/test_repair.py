import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from simulator.config import SeedConfig
from simulator.exceptions import SeedError
from simulator.history import TABLE_COLUMNS, HistoryGenerator, dataset_id, specification
from simulator.repair import expected, legacy_row, repair_payment_method, row_bytes
from simulator.seed import Seeder


@pytest.fixture
def config():
    return SeedConfig(30, 16, 100, 42)


def test_database_types_hash_like_generator():
    assert row_bytes("payments", (1, 1.5, datetime(2026, 1, 1, tzinfo=UTC))) == row_bytes(
        "payments", (1, Decimal("1.50"), datetime(2026, 1, 1, tzinfo=UTC))
    )
    assert row_bytes("seed_event_history", (1, '{"amount": 1.5}')) == row_bytes(
        "seed_event_history", (1, {"amount": 1.5})
    )


def install_legacy(db, config, monkeypatch):
    original = HistoryGenerator.rows

    def old_rows(self):
        for table, row in original(self):
            yield table, legacy_row(table, row)

    # Seeder's manifest is then replaced with the original version-1 identity.
    with monkeypatch.context() as patch:
        patch.setattr(HistoryGenerator, "rows", old_rows)
        Seeder(db, config).run()
    spec = {**specification(config), "generator_version": "customer-history-v1"}
    old_id = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    _, _, checksum, _ = expected(config, True)
    db.execute(
        "UPDATE seed_manifest SET seed_id=%s, specification=%s, content_sha256=%s",
        (old_id, json.dumps(spec), checksum),
    )


@pytest.mark.integration
def test_repair_is_exact_and_idempotent(db, config, monkeypatch):
    install_legacy(db, config, monkeypatch)
    repair_payment_method(db)
    repair_payment_method(db)
    assert db.fetch_column("SELECT DISTINCT method FROM payments") == ["credit_card"]
    assert db.fetch_one("SELECT seed_id FROM seed_manifest")[0] == dataset_id(config)
    hashes, counts, _, checksum = expected(config, False)
    assert db.fetch_one("SELECT content_sha256 FROM seed_manifest")[0] == checksum
    for table, columns in TABLE_COLUMNS.items():
        digest = hashlib.sha256()
        rows = db.fetch_all(f'SELECT {columns} FROM {table} ORDER BY {columns.split(",")[0]}')
        for row in rows:
            digest.update(row_bytes(table, row))
        assert len(rows) == counts[table]
        assert digest.digest() == hashes[table].digest()


@pytest.mark.integration
def test_mutated_source_is_refused_without_partial_repair(db, config, monkeypatch):
    install_legacy(db, config, monkeypatch)
    db.execute("UPDATE customers SET first_name='Changed' WHERE customer_id=1")
    with pytest.raises(SeedError, match="differs from the canonical seed"):
        repair_payment_method(db)
    assert db.fetch_column("SELECT DISTINCT method FROM payments") == ["card"]
    assert (
        db.fetch_one("SELECT specification->>'generator_version' FROM seed_manifest")[0]
        == "customer-history-v1"
    )


@pytest.mark.integration
def test_repair_failure_after_update_rolls_back(db, config, monkeypatch):
    install_legacy(db, config, monkeypatch)
    db.execute("""CREATE FUNCTION reject_repair() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'injected repair failure'; END $$""")
    db.execute(
        "CREATE TRIGGER reject_repair BEFORE UPDATE ON seed_manifest FOR EACH ROW EXECUTE FUNCTION reject_repair()"
    )
    try:
        with pytest.raises(SeedError, match="rolled back"):
            repair_payment_method(db)
        assert db.fetch_column("SELECT DISTINCT method FROM payments") == ["card"]
        assert (
            db.fetch_one(
                "SELECT COUNT(*) FROM seed_event_history WHERE entity_type='payments' AND payload->>'method'='credit_card'"
            )[0]
            == 0
        )
    finally:
        db.execute("DROP TRIGGER reject_repair ON seed_manifest")
        db.execute("DROP FUNCTION reject_repair()")
