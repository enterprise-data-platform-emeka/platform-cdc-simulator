"""Verified, transactional migration of the known v1 synthetic payment bug.

No arbitrary SQL or replacement datasets. Every canonical source row is checked
against the deterministic generator under write locks before any mutation.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from simulator.config import SeedConfig
from simulator.db import DatabaseManager
from simulator.exceptions import SeedError
from simulator.history import TABLE_COLUMNS, HistoryGenerator, dataset_id, specification

logger = logging.getLogger(__name__)
VERSIONS = ("customer-history-v1", "customer-history-v2")


def normalize(value: Any) -> Any:
    if isinstance(value, float | Decimal):
        return str(Decimal(str(value)).normalize())
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [normalize(item) for item in value]
    return value


def row_bytes(table: str, row: tuple) -> bytes:
    values = list(row)
    if table == "seed_event_history" and isinstance(values[-1], str):
        values[-1] = json.loads(values[-1])
    return (json.dumps(normalize(values), sort_keys=True) + "\n").encode()


def legacy_row(table: str, row: tuple) -> tuple:
    values = list(row)
    if table == "payments":
        values[2] = "card"
    elif table == "seed_event_history" and values[1] == "payments":
        payload = json.loads(values[-1])
        payload["method"] = "card"
        values[-1] = json.dumps(payload, sort_keys=True)
    return tuple(values)


def expected(config: SeedConfig, legacy: bool):
    generator = HistoryGenerator(config)
    hashes = {table: hashlib.sha256() for table in TABLE_COLUMNS}
    legacy_digest = hashlib.sha256()
    for table, new in generator.rows():
        old = legacy_row(table, new)
        legacy_digest.update(
            (table + json.dumps(old, default=str, separators=(",", ":")) + "\n").encode()
        )
        hashes[table].update(row_bytes(table, old if legacy else new))
    return hashes, generator.counts, legacy_digest.hexdigest(), generator.digest.hexdigest()


def verify_rows(cur, hashes, counts):
    actual_counts: Counter[str] = Counter()
    for table, columns in TABLE_COLUMNS.items():
        digest = hashlib.sha256()
        # Named cursor streams large history tables instead of fetching into RAM.
        with cur.connection.cursor(name=f"verify_{table}") as reader:
            reader.itersize = 2000
            reader.execute(f"SELECT {columns} FROM {table} ORDER BY {columns.split(',')[0]}")
            for row in reader:
                digest.update(row_bytes(table, row))
                actual_counts[table] += 1
        if digest.digest() != hashes[table].digest():
            raise SeedError(f"Repair refused: {table} differs from the canonical seed")
    if dict(actual_counts) != dict(counts):
        raise SeedError("Repair refused: source row counts differ")


def repair_payment_method(db: DatabaseManager) -> None:
    try:
        with db.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '10s'")
            cur.execute("SELECT pg_advisory_xact_lock(364036)")
            tables = ",".join([*TABLE_COLUMNS, "seed_manifest"])
            cur.execute(f"LOCK TABLE {tables} IN SHARE ROW EXCLUSIVE MODE")
            cur.execute(
                "SELECT seed_id, specification, row_counts, content_sha256 FROM seed_manifest"
            )
            manifests = cur.fetchall()
            if len(manifests) != 1:
                raise SeedError("Repair requires exactly one completed source manifest")
            seed_id, spec, counts, checksum = manifests[0]
            if spec.get("generator_version") not in VERSIONS:
                raise SeedError("Repair supports only canonical customer-history v1 and v2")
            if spec.get("profile") not in ("smoke", "customer-intelligence-36m"):
                raise SeedError("Repair refused: unknown seed profile")
            config = SeedConfig(
                spec["customers"],
                spec["products"],
                spec["orders"],
                spec["random_seed"],
                profile=spec["profile"],
                start_date=spec["start"],
                end_date=spec["end_exclusive"],
            )
            current_spec = specification(config)
            original_spec = {**current_spec, "generator_version": spec["generator_version"]}
            original_id = hashlib.sha256(
                json.dumps(original_spec, sort_keys=True).encode()
            ).hexdigest()
            if spec != original_spec or seed_id != original_id:
                raise SeedError("Repair refused: incompatible source specification")
            legacy = spec["generator_version"] == VERSIONS[0]
            hashes, expected_counts, old_checksum, new_checksum = expected(config, legacy)
            if counts != dict(expected_counts) or checksum != (
                old_checksum if legacy else new_checksum
            ):
                raise SeedError("Repair refused: manifest checksum or counts are incompatible")
            verify_rows(cur, hashes, counts)
            if not legacy:
                logger.info("Canonical v2 source verified; repair already applied")
                _receipt(config, counts, new_checksum)
                return
            cur.execute("UPDATE payments SET method = 'credit_card' WHERE method = 'card'")
            if cur.rowcount != counts["payments"]:
                raise SeedError("Repair payment count mismatch")
            cur.execute("""
                UPDATE seed_event_history
                SET payload = jsonb_set(payload, '{method}', to_jsonb('credit_card'::text))
                WHERE entity_type = 'payments' AND payload->>'method' = 'card'
            """)
            cur.execute(
                """
                UPDATE seed_manifest SET seed_id = %s, specification = %s, content_sha256 = %s
                WHERE seed_id = %s
            """,
                (dataset_id(config), json.dumps(current_spec), new_checksum, seed_id),
            )
            if cur.rowcount != 1:
                raise SeedError("Repair manifest count mismatch")
            # Verify the changed representations before committing; all other rows
            # remain protected by the same write locks.
            cur.execute("SELECT COUNT(*) FROM payments WHERE method <> 'credit_card'")
            result = cur.fetchone()
            if result is None or result[0]:
                raise SeedError("Payment repair verification failed")
            cur.execute("""SELECT COUNT(*) FROM seed_event_history WHERE entity_type = 'payments'
                AND payload->>'method' IS DISTINCT FROM 'credit_card'""")
            result = cur.fetchone()
            if result is None or result[0]:
                raise SeedError("Payment history repair verification failed")
        logger.info("Payment seed repair committed; canonical v2 manifest recorded")
        _receipt(config, counts, new_checksum)
    except SeedError:
        raise
    except Exception as exc:
        raise SeedError(f"Payment repair rolled back ({type(exc).__name__})") from exc


def _receipt(config, counts, checksum):
    logger.info(
        "REPAIR_RECEIPT %s",
        json.dumps(
            {
                "seed_id": dataset_id(config),
                "specification": specification(config),
                "row_counts": dict(counts),
                "content_sha256": checksum,
            },
            sort_keys=True,
        ),
    )
