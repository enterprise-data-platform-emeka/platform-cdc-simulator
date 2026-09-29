"""Versioned synthetic history. Business time is independent of the load clock.

Hidden activity regimes drive stochastic purchases but are never exported as
features or labels. Events carry simulated availability; updated_at in the OLTP
snapshot remains the actual ingestion clock. No wall-clock calls occur here.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from simulator.config import (
    CATEGORY_PRICE_RANGE,
    CUSTOMER_COUNTRIES,
    CUSTOMER_COUNTRY_WEIGHTS,
    ProductCategory,
    SeedConfig,
)

GENERATOR_VERSION = "customer-history-v1"
TABLE_COLUMNS = {
    "customers": "customer_id,first_name,last_name,email,country,phone,signup_date",
    "products": "product_id,name,category,brand,unit_price,stock_qty",
    "orders": "order_id,customer_id,order_date,order_status",
    "order_items": "order_item_id,order_id,product_id,quantity,unit_price,line_total",
    "payments": "payment_id,order_id,method,amount,status,payment_date",
    "shipments": "shipment_id,order_id,carrier,delivery_status,shipped_date,delivered_date",
    "seed_event_history": "event_id,entity_type,entity_id,event_time,available_time,payload",
}


def specification(config: SeedConfig) -> dict[str, Any]:
    return {
        "generator_version": GENERATOR_VERSION,
        "profile": config.profile,
        "start": config.start_date,
        "end_exclusive": config.end_date,
        "random_seed": config.random_seed,
        "customers": config.num_customers,
        "products": config.num_products,
        "orders": config.num_historical_orders,
        "event_time_semantics": "synthetic business time; available_time is simulated",
    }


def dataset_id(config: SeedConfig) -> str:
    content = json.dumps(specification(config), sort_keys=True).encode()
    return hashlib.sha256(content).hexdigest()


class HistoryGenerator:
    def __init__(self, config: SeedConfig):
        self.config = config
        self.rng = random.Random(config.random_seed)
        self.start = datetime.fromisoformat(config.start_date).replace(tzinfo=UTC)
        self.end = datetime.fromisoformat(config.end_date).replace(tzinfo=UTC)
        self.days = (self.end - self.start).days
        if self.days < 365 or config.num_customers < 2 or config.num_products < 8:
            raise ValueError("History needs at least one year, two customers and eight products")
        self.event_id = 0
        self.counts: Counter[str] = Counter()
        self.monthly_orders: Counter[str] = Counter()
        self.digest = hashlib.sha256()

    def _event(self, entity: str, key: int, time: datetime, data: dict[str, Any]):
        """Late events are available after occurrence; never pretend backfill arrived earlier."""
        self.event_id += 1
        delay = (
            timedelta(hours=self.rng.randint(1, 72)) if self.rng.random() < 0.02 else timedelta(0)
        )
        return (
            self.event_id,
            entity,
            key,
            time,
            time + delay,
            json.dumps(data, sort_keys=True, default=str),
        )

    def rows(self) -> Iterator[tuple[str, tuple]]:
        """Yield bounded-memory rows. Explicit IDs make reloads deterministic."""
        for table, row in self._rows():
            self.counts[table] += 1
            self.digest.update(
                (table + json.dumps(row, default=str, separators=(",", ":")) + "\n").encode()
            )
            yield table, row

    def _rows(self) -> Iterator[tuple[str, tuple]]:
        rng, cfg = self.rng, self.config
        row: tuple[Any, ...]
        customers = []
        weights = []
        for cid in range(1, cfg.num_customers + 1):
            signup = self.start + timedelta(days=rng.randrange(max(1, self.days - 30)))
            regime = rng.choices(
                ["regular", "occasional", "declining", "returning", "one_off", "new"],
                [25, 25, 20, 10, 15, 5],
            )[0]
            if regime == "new":
                signup = self.end - timedelta(days=rng.randint(1, 90))
            preference = rng.randrange(8)
            change = signup + (self.end - signup) * rng.uniform(0.3, 0.7)
            country = rng.choices(CUSTOMER_COUNTRIES, CUSTOMER_COUNTRY_WEIGHTS)[0]
            customers.append((signup, regime, preference, change))
            weights.append(
                0
                if regime == "one_off"
                else rng.lognormvariate(0, 1) * (self.end - signup).days / self.days
            )
            row = (
                cid,
                f"Customer{cid}",
                "Synthetic",
                f"customer{cid}@example.invalid",
                country,
                None,
                signup,
            )
            yield "customers", row
            yield (
                "seed_event_history",
                self._event(
                    "customers", cid, signup, dict(zip(TABLE_COLUMNS["customers"].split(","), row))
                ),
            )

        products = {}
        category_products: list[list[int]] = [[] for _ in range(8)]
        for pid in range(1, cfg.num_products + 1):
            category = (pid - 1) % 8
            low, high = CATEGORY_PRICE_RANGE[ProductCategory.ALL[category]]
            price = round(rng.uniform(low, high), 2)
            # Each category has launch-day products, plus later introductions.
            launch = (
                self.start
                if pid <= 8
                else self.start + timedelta(days=rng.randrange(self.days - 60))
            )
            stockout = launch + timedelta(days=rng.randint(30, 180))
            products[pid] = (price, launch, stockout)
            category_products[category].append(pid)
            stock = 0 if stockout <= self.end < stockout + timedelta(days=14) else 100
            row = (
                pid,
                f"Product {pid}",
                ProductCategory.ALL[category],
                f"Brand {category}",
                price,
                stock,
            )
            yield "products", row
            initial = dict(zip(TABLE_COLUMNS["products"].split(","), row))
            initial["stock_qty"] = 100
            yield "seed_event_history", self._event("products", pid, launch, initial)
            for time, qty in [(stockout, 0), (stockout + timedelta(days=14), 100)]:
                if time < self.end:
                    yield (
                        "seed_event_history",
                        self._event("products", pid, time, {**initial, "stock_qty": qty}),
                    )

        # Reserve one order for one-off customers, then distribute remaining volume
        # with a long tail of purchasing frequency. No equal-per-customer allocation.
        allocation = Counter({i: 1 for i, c in enumerate(customers) if c[1] == "one_off"})
        if sum(allocation.values()) > cfg.num_historical_orders:
            allocation = Counter(dict(list(allocation.items())[: cfg.num_historical_orders]))
        remaining = cfg.num_historical_orders - sum(allocation.values())
        allocation.update(rng.choices(range(len(customers)), weights=weights, k=remaining))
        oid = iid = payid = sid = 0
        for ci, count in sorted(allocation.items()):
            signup, regime, preference, change = customers[ci]
            dates = []
            for _ in range(count):
                for attempt in range(1000):
                    date = signup + timedelta(
                        seconds=rng.randrange(max(1, int((self.end - signup).total_seconds())))
                    )
                    activity = 0.12 if regime == "declining" and date > change else 1.0
                    if regime == "returning" and change < date < change + timedelta(days=120):
                        activity = 0.08
                    season = 1.0 if date.month in (11, 12) else 0.65
                    if preference in (3, 7) and date.month in (6, 7, 8):
                        season = 1.0
                    if rng.random() < activity * season:
                        break
                else:
                    raise ValueError("Could not sample a valid purchase date")
                dates.append(date)
            for date in sorted(dates):
                oid += 1
                self.monthly_orders[date.strftime("%Y-%m")] += 1
                cancel, refund = rng.random() < 0.05, rng.random() < 0.03
                paid = date + timedelta(minutes=rng.randint(5, 60))
                shipped = paid + timedelta(days=rng.randint(1, 3))
                delivered = shipped + timedelta(days=rng.randint(2, 8))
                refunded = delivered + timedelta(days=rng.randint(1, 20))
                transitions = [(date, "pending")]
                if cancel:
                    transitions.append((paid, "cancelled"))
                else:
                    transitions += [
                        (paid, "confirmed"),
                        (shipped, "shipped"),
                        (delivered, "delivered"),
                    ]
                    if refund:
                        transitions.append((refunded, "refunded"))
                observed = [(t, status) for t, status in transitions if t < self.end]
                yield "orders", (oid, ci + 1, date, observed[-1][1])
                for time, status in observed:
                    yield (
                        "seed_event_history",
                        self._event(
                            "orders",
                            oid,
                            time,
                            {
                                "order_id": oid,
                                "customer_id": ci + 1,
                                "order_date": date,
                                "order_status": status,
                            },
                        ),
                    )
                total = 0.0
                selected = set()
                for _ in range(rng.choice([2, 3])):
                    category = preference if rng.random() < 0.8 else rng.randrange(8)
                    candidates = [
                        p
                        for p in category_products[category]
                        if products[p][1] <= date
                        and not (products[p][2] <= date < products[p][2] + timedelta(days=14))
                        and p not in selected
                    ]
                    if not candidates:
                        candidates = [
                            p
                            for p in products
                            if products[p][1] <= date
                            and p not in selected
                            and not (products[p][2] <= date < products[p][2] + timedelta(days=14))
                        ]
                    if not candidates:
                        continue
                    # Stable within-category popularity; stochastic individual selections.
                    pid = rng.choices(
                        candidates, [1 / (1 + (p - 1) // 8) ** 0.5 for p in candidates]
                    )[0]
                    selected.add(pid)
                    iid += 1
                    quantity = rng.choices([1, 2, 3], [80, 15, 5])[0]
                    amount = round(products[pid][0] * quantity, 2)
                    total += amount
                    row = (iid, oid, pid, quantity, products[pid][0], amount)
                    yield "order_items", row
                    yield (
                        "seed_event_history",
                        self._event(
                            "order_items",
                            iid,
                            date,
                            dict(zip(TABLE_COLUMNS["order_items"].split(","), row)),
                        ),
                    )
                if not cancel and paid < self.end:
                    if rng.random() < 0.03:
                        payid += 1
                        row = (payid, oid, "card", round(total, 2), "failed", date)
                        yield "payments", row
                        yield (
                            "seed_event_history",
                            self._event(
                                "payments",
                                payid,
                                date,
                                dict(zip(TABLE_COLUMNS["payments"].split(","), row)),
                            ),
                        )
                    payid += 1
                    status = "refunded" if refund and refunded < self.end else "completed"
                    row = (payid, oid, "card", round(total, 2), status, paid)
                    yield "payments", row
                    payload = dict(zip(TABLE_COLUMNS["payments"].split(","), row))
                    yield (
                        "seed_event_history",
                        self._event("payments", payid, paid, {**payload, "status": "completed"}),
                    )
                    if status == "refunded":
                        yield (
                            "seed_event_history",
                            self._event("payments", payid, refunded, payload),
                        )
                if not cancel and shipped < self.end:
                    sid += 1
                    status = "delivered" if delivered < self.end else "in_transit"
                    row = (
                        sid,
                        oid,
                        "DHL",
                        status,
                        shipped,
                        delivered if delivered < self.end else None,
                    )
                    yield "shipments", row
                    payload = dict(zip(TABLE_COLUMNS["shipments"].split(","), row))
                    yield (
                        "seed_event_history",
                        self._event(
                            "shipments",
                            sid,
                            shipped,
                            {**payload, "delivery_status": "in_transit", "delivered_date": None},
                        ),
                    )
                    if delivered < self.end:
                        yield (
                            "seed_event_history",
                            self._event("shipments", sid, delivered, payload),
                        )
