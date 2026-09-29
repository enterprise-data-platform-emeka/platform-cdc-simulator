"""Atomic, bounded-memory seed loader with a completion manifest."""

from __future__ import annotations

import json
import logging
from collections import defaultdict

from psycopg2.extras import execute_values

from simulator.config import SeedConfig
from simulator.db import DatabaseManager
from simulator.exceptions import SeedError
from simulator.history import TABLE_COLUMNS, HistoryGenerator, dataset_id, specification

logger = logging.getLogger(__name__)


class Seeder:
    def __init__(self, db: DatabaseManager, config: SeedConfig) -> None:
        self._db, self._config = db, config

    def run(self) -> None:
        """One transaction: either every table and manifest commit, or none do.

        Existing source data is reused only with the identical manifest and row
        counts. Dataset replacement requires an explicit reset outside this API.
        """
        try:
            with self._db.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(364036)")
                cur.execute("SELECT seed_id, row_counts FROM seed_manifest")
                manifests = cur.fetchall()
                counts = {}
                for table in TABLE_COLUMNS:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    count_row = cur.fetchone()
                    if count_row is None:
                        raise SeedError("Could not validate existing source counts")
                    counts[table] = count_row[0]
                if manifests or any(counts.values()):
                    if len(manifests) != 1 or manifests[0][0] != dataset_id(self._config):
                        raise SeedError(
                            "Source contains another or incomplete seed; reset explicitly"
                        )
                    if manifests[0][1] != counts:
                        raise SeedError(
                            "Source counts differ from the completed seed; reset explicitly"
                        )
                    logger.info("Identical completed seed already exists; no data changed")
                    return

                generator = HistoryGenerator(self._config)
                buffers: dict[str, list[tuple]] = defaultdict(list)
                for table, row in generator.rows():
                    buffers[table].append(row)
                    # Parent tables are flushed before dependent rows at each boundary.
                    if sum(map(len, buffers.values())) >= 5000:
                        self._flush(cur, buffers)
                self._flush(cur, buffers)
                for table, columns in TABLE_COLUMNS.items():
                    key = columns.split(",")[0]
                    cur.execute(
                        f"SELECT setval(pg_get_serial_sequence('{table}', '{key}'), "
                        f"COALESCE(MAX({key}),1), COUNT(*) > 0) FROM {table}"
                    )
                cur.execute(
                    "INSERT INTO seed_manifest (seed_id, specification, row_counts, content_sha256) "
                    "VALUES (%s,%s,%s,%s)",
                    (
                        dataset_id(self._config),
                        json.dumps(specification(self._config)),
                        json.dumps(generator.counts),
                        generator.digest.hexdigest(),
                    ),
                )
                logger.info(
                    "Completed seed: %s",
                    json.dumps(
                        {
                            "dataset_id": dataset_id(self._config),
                            "counts": generator.counts,
                            "monthly_orders": generator.monthly_orders,
                            "content_sha256": generator.digest.hexdigest(),
                        },
                        sort_keys=True,
                    ),
                )
        except SeedError:
            raise
        except Exception as exc:
            raise SeedError(f"Seed transaction failed ({type(exc).__name__}); rolled back") from exc

    @staticmethod
    def _flush(cur, buffers: dict[str, list[tuple]]) -> None:
        for table, columns in TABLE_COLUMNS.items():
            rows = buffers[table]
            if rows:
                execute_values(
                    cur, f"INSERT INTO {table} ({columns}) VALUES %s", rows, page_size=1000
                )
                rows.clear()
