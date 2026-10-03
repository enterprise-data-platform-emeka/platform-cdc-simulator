import json
from collections import defaultdict
from datetime import datetime

import pytest

from simulator.config import PaymentMethod, SeedConfig
from simulator.history import HistoryGenerator, dataset_id


@pytest.fixture(scope="module")
def history():
    generator = HistoryGenerator(SeedConfig(500, 200, 2000, 42))
    data = defaultdict(list)
    for table, row in generator.rows():
        data[table].append(row)
    return generator, data


def test_reproducible_without_wall_clock(history):
    first, data = history
    second = HistoryGenerator(SeedConfig(500, 200, 2000, 42))
    for _ in second.rows():
        pass
    assert first.digest.hexdigest() == second.digest.hexdigest()
    assert first.counts == second.counts
    assert len(first.monthly_orders) == 36
    assert first.counts["orders"] == 2000
    assert 4000 <= first.counts["order_items"] <= 6000


def test_dates_and_relationships(history):
    generator, data = history
    signup = {x[0]: x[6] for x in data["customers"]}
    orders = {x[0]: x for x in data["orders"]}
    launches = {}
    for _, entity, key, occurred, available, payload in data["seed_event_history"]:
        assert occurred < generator.end
        assert available >= occurred
        assert not {"regime", "preference", "churn_label"} & json.loads(payload).keys()
        if entity == "products":
            launches[key] = min(launches.get(key, occurred), occurred)
    for _, customer, date, _ in data["orders"]:
        assert signup[customer] <= date < generator.end
    for _, order, product, qty, price, total in data["order_items"]:
        assert launches[product] <= orders[order][2]
        assert total == round(qty * price, 2)
    for _, order, _, _, _, date in data["payments"]:
        assert orders[order][2] <= date < generator.end
    for _, order, _, _, shipped, delivered in data["shipments"]:
        assert orders[order][2] <= shipped < generator.end
        assert delivered is None or shipped <= delivered < generator.end


def test_realistic_variation(history):
    generator, data = history
    counts = defaultdict(int)
    for row in data["orders"]:
        counts[row[1]] += 1
    assert any(v == 1 for v in counts.values())
    assert max(counts.values()) > 10
    assert len(counts) < len(data["customers"])
    assert any(row[4] > row[3] for row in data["seed_event_history"])
    assert any(row[4] == "failed" for row in data["payments"])


def test_dataset_version_changes_with_parameters():
    assert dataset_id(SeedConfig(500, 200, 2000, 42)) != dataset_id(SeedConfig(500, 200, 2000, 43))


@pytest.mark.integration
def test_seed_atomic_idempotent_and_mismatch(db):
    from simulator.exceptions import SeedError
    from simulator.seed import Seeder

    config = SeedConfig(30, 16, 100, 42)
    Seeder(db, config).run()
    Seeder(db, config).run()
    assert db.fetch_one("SELECT COUNT(*) FROM orders")[0] == 100
    assert db.fetch_one("SELECT COUNT(*) FROM seed_manifest")[0] == 1
    with pytest.raises(SeedError):
        Seeder(db, SeedConfig(30, 16, 101, 42)).run()
    assert db.fetch_one("SELECT COUNT(*) FROM orders")[0] == 100


@pytest.mark.integration
def test_seed_failure_rolls_back(db, monkeypatch):
    from simulator.exceptions import SeedError
    from simulator.seed import Seeder

    def fail(self):
        # Cross the loader's 5,000-row flush boundary before failing.
        for customer_id in range(1, 5002):
            yield (
                "customers",
                (
                    customer_id,
                    "Test",
                    "User",
                    f"test{customer_id}@example.invalid",
                    "Germany",
                    None,
                    datetime.fromisoformat("2024-01-01"),
                ),
            )
        raise ValueError("injected")

    monkeypatch.setattr(HistoryGenerator, "rows", fail)
    with pytest.raises(SeedError):
        Seeder(db, SeedConfig(30, 16, 100, 42)).run()
    assert db.fetch_one("SELECT COUNT(*) FROM customers")[0] == 0
    assert db.fetch_one("SELECT COUNT(*) FROM seed_manifest")[0] == 0


def test_payment_methods_match_platform_contract_in_snapshot_and_history(history):
    _, data = history
    # Both failed attempts and their settled payments must use canonical values.
    allowed = set(PaymentMethod.ALL)
    payments = data["payments"]
    assert {row[4] for row in payments} >= {"failed", "completed", "refunded"}
    assert all(row[2] in allowed for row in payments)
    payment_events = [
        json.loads(row[5]) for row in data["seed_event_history"] if row[1] == "payments"
    ]
    assert payment_events
    assert all(event["method"] in allowed for event in payment_events)
    snapshot_methods = {row[0]: row[2] for row in payments}
    assert all(event["method"] == snapshot_methods[event["payment_id"]] for event in payment_events)


def test_corrected_dataset_identity_differs_from_legacy_seed():
    import hashlib

    from simulator.history import specification

    config = SeedConfig(500, 200, 2000, 42)
    legacy = {**specification(config), "generator_version": "customer-history-v1"}
    legacy_id = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
    assert dataset_id(config) != legacy_id
