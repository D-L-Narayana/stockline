"""Demo seed: deterministic catalogue, back-dated order history, transfers, ledger consistency and speed.

Runs the seed exactly as the pool and the bridge do (``seed(conn)`` on a freshly migrated temporary database).
"""
from __future__ import annotations

import ast
import random
import re
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app import db, ledger, reports, seed

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
STORE_CODES = ["BLR-01", "HYD-01", "MUM-01"]
SKUS = [
    "GRO-RICE-5KG", "GRO-OIL-1L", "HH-DET-2KG", "HH-TOWEL-SET", "APP-TEE-M", "APP-JEAN-32",
    "ELE-EARBUD", "ELE-CHARGER", "BTY-SHAMPOO", "TOY-BLOCKS", "HOME-LAMP", "SPT-YOGA-MAT",
]
FACADE_NAMES = {"create_store", "create_product", "adjust_stock", "place_order", "cancel_order", "fulfil_order", "transfer"}
TABLES = ("stores", "products", "inventory", "stock_movements", "orders", "order_lines")


def _fresh(path) -> sqlite3.Connection:
    conn = db.connect(str(path))
    db.init_schema(conn)
    return conn


@pytest.fixture()
def conn(tmp_path):
    c = _fresh(tmp_path / "seed.db")
    yield c
    c.close()


@pytest.fixture()
def seeded(conn):
    seed.seed(conn)
    return conn


def _count(conn: sqlite3.Connection, table: str, where: str = "1=1", params: tuple = ()) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _orders(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM orders ORDER BY id")]


# --------------------------------------------------------------------------- catalogue
def test_seed_creates_exactly_three_stores_and_twelve_products(seeded):
    assert [r[0] for r in seeded.execute("SELECT code FROM stores ORDER BY id")] == STORE_CODES
    assert [r[0] for r in seeded.execute("SELECT sku FROM products ORDER BY id")] == SKUS
    assert _count(seeded, "products", "active = 1") == 12
    assert _count(seeded, "inventory") == 36
    opening = {
        (r["store_id"], r["product_id"]): r["delta"]
        for r in seeded.execute("SELECT store_id, product_id, delta FROM stock_movements WHERE reason='receipt' AND reference='opening-stock'")
    }
    assert len(opening) == 36
    for store_index in range(3):
        for product_index in range(12):
            assert opening[(store_index + 1, product_index + 1)] == 5 + ((store_index * 7 + product_index * 11) % 40)


def test_seed_is_idempotent_and_returns_early_when_stores_exist(seeded, tmp_path):
    before = {t: _count(seeded, t) for t in TABLES}
    seed.seed(seeded)
    seed.seed(seeded)
    assert {t: _count(seeded, t) for t in TABLES} == before
    other = _fresh(tmp_path / "prefilled.db")
    other.execute("INSERT INTO stores (code, name, region) VALUES ('X1', 'Existing', 'North')")
    seed.seed(other)
    assert _count(other, "stores") == 1 and _count(other, "products") == 0 and _count(other, "orders") == 0
    other.close()


# --------------------------------------------------------------------------- ledger
def test_seed_leaves_the_ledger_consistent(seeded):
    report = ledger.ledger_integrity(seeded)
    assert report["ok"] is True
    assert report["checked"] == 36
    assert report["mismatches"] == [] and report["negative"] == [] and report["chain_breaks"] == []
    assert report["order_total_mismatches"] == []
    assert seeded.in_transaction is False


def test_seed_relaxes_fsync_only_while_seeding(conn, monkeypatch):
    seen: list[int] = []
    real_populate = seed._populate

    def spying_populate(c, rng, now):
        seen.append(c.execute("PRAGMA synchronous").fetchone()[0])
        real_populate(c, rng, now)

    monkeypatch.setattr(seed, "_populate", spying_populate)
    before = conn.execute("PRAGMA synchronous").fetchone()[0]
    seed.seed(conn)
    assert seen == [1]  # NORMAL during the seed ...
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == before  # ... and the previous level restored afterwards
    seed.seed(conn)  # no-op path leaves the pragma alone too
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == before


def test_seed_keeps_stock_for_the_demo_scenarios(seeded):
    assert seeded.execute("SELECT on_hand FROM inventory WHERE store_id=1 AND product_id=1").fetchone()[0] >= 1
    assert seeded.execute("SELECT MIN(on_hand) FROM inventory").fetchone()[0] >= 1
    low = _count(seeded, "inventory i JOIN products p ON p.id = i.product_id", "i.on_hand <= p.reorder_point")
    assert low >= 5


# --------------------------------------------------------------------------- order history
def test_seed_spreads_about_forty_orders_over_the_last_45_days(seeded):
    orders = _orders(seeded)
    assert 36 <= len(orders) <= 44
    now = datetime.now(UTC)
    ages = [(now - _parse(o["created_at"])).total_seconds() / 86400 for o in orders]
    assert all(ISO_RE.match(o["created_at"]) for o in orders)
    assert all(0 <= age <= 45.1 for age in ages), (min(ages), max(ages))
    assert max(ages) >= 30
    assert min(ages) <= 3
    assert len({o["created_at"][:10] for o in orders}) >= 30
    assert all(o["created_at"][:10] < f"{now:%Y-%m-%d}" for o in orders)  # the history ends yesterday; today is for the demo user
    assert all("08:00" <= o["created_at"][11:16] <= "22:00" for o in orders)  # business hours (UTC)
    stamps = [o["created_at"] for o in orders]
    assert stamps == sorted(stamps)  # ids follow the timeline
    assert all(1 <= _count(seeded, "order_lines", "order_id = ?", (o["id"],)) <= 3 for o in orders)
    assert all(1 <= q <= 3 for (q,) in seeded.execute("SELECT quantity FROM order_lines"))
    keyed = [o["idempotency_key"] for o in orders if o["idempotency_key"]]
    assert 5 <= len(keyed) < len(orders) and len(set(keyed)) == len(keyed)


def test_seed_back_dates_movements_together_with_their_orders(seeded):
    orders = _orders(seeded)
    for o in orders:
        assert o["updated_at"] is not None and ISO_RE.match(o["updated_at"]) and o["updated_at"] >= o["created_at"]
        sale_stamps = {r[0] for r in seeded.execute("SELECT created_at FROM stock_movements WHERE reference = ?", (f"order:{o['id']}",))}
        assert sale_stamps == {o["created_at"]}, o
        cancel_stamps = {r[0] for r in seeded.execute("SELECT created_at FROM stock_movements WHERE reference = ?", (f"cancel:{o['id']}",))}
        if o["status"] == "cancelled":
            assert cancel_stamps == {o["updated_at"]}, o
            restocked = seeded.execute("SELECT SUM(delta) FROM stock_movements WHERE reference = ?", (f"cancel:{o['id']}",)).fetchone()[0]
            assert restocked == seeded.execute("SELECT SUM(quantity) FROM order_lines WHERE order_id = ?", (o["id"],)).fetchone()[0]
        else:
            assert cancel_stamps == set()
        if o["status"] == "placed":
            assert o["updated_at"] == o["created_at"]
    newest = max(o["updated_at"] for o in orders)
    assert newest <= f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%S}.999Z"


def test_seed_status_mix(seeded):
    by_status = {r[0]: r[1] for r in seeded.execute("SELECT status, COUNT(*) FROM orders GROUP BY status")}
    assert set(by_status) == {"placed", "cancelled", "fulfilled"}
    assert 4 <= by_status["cancelled"] <= 8
    assert 16 <= by_status["fulfilled"] <= 24
    assert by_status["placed"] >= 8
    assert seeded.execute("SELECT status FROM orders WHERE id = 1").fetchone()[0] in ("placed", "cancelled", "fulfilled")


# --------------------------------------------------------------------------- transfers
def test_seed_makes_exactly_two_transfers(seeded):
    assert _count(seeded, "stock_movements", "reason = 'transfer_out'") == 2
    assert _count(seeded, "stock_movements", "reason = 'transfer_in'") == 2
    out_total = seeded.execute("SELECT SUM(delta) FROM stock_movements WHERE reason = 'transfer_out'").fetchone()[0]
    in_total = seeded.execute("SELECT SUM(delta) FROM stock_movements WHERE reason = 'transfer_in'").fetchone()[0]
    assert out_total < 0 and in_total == -out_total
    pairs = seeded.execute(
        "SELECT o.store_id, i.store_id, o.product_id FROM stock_movements o JOIN stock_movements i "
        "ON i.reason = 'transfer_in' AND i.reference = o.reference AND i.product_id = o.product_id WHERE o.reason = 'transfer_out'"
    ).fetchall()
    assert len(pairs) == 2 and all(src != dst for src, dst, _ in pairs)


# --------------------------------------------------------------------------- determinism and speed
def test_seed_is_deterministic(tmp_path):
    snapshots = []
    for name in ("a.db", "b.db"):
        c = _fresh(tmp_path / name)
        seed.seed(c)
        orders = _orders(c)
        base = _parse(orders[0]["created_at"])
        snapshots.append(
            {
                "inventory": [tuple(r) for r in c.execute("SELECT store_id, product_id, on_hand, version FROM inventory ORDER BY 1, 2")],
                "orders": [(o["id"], o["store_id"], o["status"], o["total_cents"], o["idempotency_key"]) for o in orders],
                "offsets": [(_parse(o["created_at"]) - base).total_seconds() for o in orders],
                "lines": [tuple(r) for r in c.execute("SELECT order_id, product_id, quantity, unit_price_cents FROM order_lines ORDER BY 1, 2")],
                "movements": [tuple(r) for r in c.execute("SELECT store_id, product_id, delta, reason FROM stock_movements ORDER BY id")],
            }
        )
        c.close()
    assert snapshots[0] == snapshots[1]  # the whole history, timestamps included, is a function of the seed and the seeding date
    assert getattr(seed, "RANDOM_SEED", None) == 42


@pytest.mark.parametrize("hour, minute", [(0, 10), (3, 0), (6, 0), (9, 30), (13, 55), (18, 0), (21, 0), (23, 50)])
def test_plan_history_is_independent_of_the_wall_clock_hour(hour, minute):
    """The calendar spread of the history must not depend on the time of day the database is seeded."""
    now = datetime(2026, 10, 5, hour, minute, tzinfo=UTC)
    orders, transfers = seed.plan_history(random.Random(seed.RANDOM_SEED), now)
    assert len(orders) == seed.ORDER_COUNT
    assert len({o.placed_at.date() for o in orders}) == seed.BUSY_DAYS
    assert all(o.placed_at <= now for o in orders)
    assert all(o.closed_at is None or o.placed_at <= o.closed_at <= now for o in orders)
    assert all((now - o.placed_at) < timedelta(days=seed.HISTORY_DAYS + 1) for o in orders)
    assert len(transfers) == 2
    # the same history at every hour: identical stores, lines, outcomes and calendar offsets
    reference_orders, reference_transfers = seed.plan_history(random.Random(seed.RANDOM_SEED), datetime(2026, 10, 5, 12, 0, tzinfo=UTC))
    key = [(o.store, tuple(o.lines), o.status, o.key or "", (now.date() - o.placed_at.date()).days) for o in orders]
    reference_key = [(o.store, tuple(o.lines), o.status, o.key or "", (date(2026, 10, 5) - o.placed_at.date()).days) for o in reference_orders]
    assert sorted(key) == sorted(reference_key)
    assert transfers == reference_transfers


def test_seed_runs_in_under_a_second(conn):
    t0 = time.perf_counter()
    seed.seed(conn)
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, f"seed took {elapsed:.2f}s"
    assert _count(conn, "orders") >= 36


# --------------------------------------------------------------------------- reports on the seeded data
def test_seed_feeds_the_reports(seeded):
    reorder = reports.reorder_report(seeded)
    assert len(reorder) >= 5
    assert any(row["sold_window"] > 0 for row in reorder)
    assert any(row["days_of_cover"] is not None for row in reorder)
    by_day = reports.sales_report(seeded, days=45)
    assert len(by_day) >= 25
    assert [r["day"] for r in by_day] == sorted(r["day"] for r in by_day)
    by_product = reports.sales_report(seeded, days=45, group_by="product")
    assert len(by_product) >= 8
    summary = reports.summary_report(seeded)
    assert [s["store_code"] for s in summary["stores"]] == STORE_CODES
    assert summary["totals"]["skus"] == 36
    assert summary["products"] == {"active": 12, "inactive": 0}
    assert sum(summary["orders"].values()) == _count(seeded, "orders")


# --------------------------------------------------------------------------- façade discipline
def test_seed_calls_only_facade_names_and_stays_framework_free():
    tree = ast.parse(Path(seed.__file__).read_text(encoding="utf-8"))
    called: set[str] = set()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "service":
                called.add(node.func.attr)
        elif isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0] if not node.level else f".{node.module or ''}")
            if node.level and not node.module:
                imported.update(f".{alias.name}" for alias in node.names)
    assert called == FACADE_NAMES
    assert not imported & {"fastapi", "starlette", "uvicorn", "threading", "queue"}
    assert not imported & {".deps", ".main", ".observability", ".security", ".ledger", ".orders", ".inventory", ".catalog"}
    assert len(seed.STORES) == 3 and len(seed.PRODUCTS) == 12
