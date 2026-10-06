"""Ledger core: ``apply_movement`` with ``balance_after``, integrity checks and cache rebuild."""
from __future__ import annotations

import sqlite3

import pytest

from app import db, ledger
from app.common import CONFLICT, INSUFFICIENT_STOCK, VALIDATION, ServiceError


def _seed_catalog(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO stores (code, name, region) VALUES ('S1','One','South'), ('S2','Two','West')")
    conn.execute(
        "INSERT INTO products (sku, name, category, price_cents, reorder_point) "
        "VALUES ('SKU-1','Widget','Home',1000,5), ('SKU-2','Gadget','Elec',2500,2)"
    )


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "ledger.db"))
    db.migrate(c)
    _seed_catalog(c)
    yield c
    c.close()


def _apply(
    conn: sqlite3.Connection, store_id: int, product_id: int, delta: int, reason: str = "adjustment", reference: str | None = None
) -> tuple[int, int]:
    with db.transaction(conn):
        return ledger.apply_movement(conn, store_id, product_id, delta, reason, reference)


def _inv(conn: sqlite3.Connection, store_id: int, product_id: int) -> tuple[int, int] | None:
    row = conn.execute("SELECT on_hand, version FROM inventory WHERE store_id=? AND product_id=?", (store_id, product_id)).fetchone()
    return (row[0], row[1]) if row else None


def _movement(conn: sqlite3.Connection, movement_id: int) -> dict:
    return dict(conn.execute("SELECT * FROM stock_movements WHERE id=?", (movement_id,)).fetchone())


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _raw_movement(conn: sqlite3.Connection, store_id: int, product_id: int, delta: int, reason: str = "adjustment") -> int:
    """A movement written the v0.1 way: no balance_after, cache untouched."""
    cur = conn.execute(
        "INSERT INTO stock_movements (store_id, product_id, delta, reason, reference) VALUES (?,?,?,?,?)",
        (store_id, product_id, delta, reason, "raw"),
    )
    return cur.lastrowid


# --------------------------------------------------------------------------- apply_movement
def test_apply_movement_writes_balance_after_and_bumps_version(conn):
    new, mid = _apply(conn, 1, 1, 10, "receipt", "r1")
    assert new == 10
    first = _movement(conn, mid)
    assert (first["store_id"], first["product_id"], first["delta"], first["reason"], first["reference"]) == (1, 1, 10, "receipt", "r1")
    assert first["balance_after"] == 10
    assert _inv(conn, 1, 1) == (10, 1)

    new2, mid2 = _apply(conn, 1, 1, -3, "sale", "order:1")
    assert (new2, mid2) == (7, mid + 1)
    assert _movement(conn, mid2)["balance_after"] == 7
    assert _inv(conn, 1, 1) == (7, 2)

    new3, mid3 = _apply(conn, 1, 1, 5, "return", "cancel:1")
    assert new3 == 12 and _movement(conn, mid3)["balance_after"] == 12
    assert _inv(conn, 1, 1) == (12, 3)
    assert _inv(conn, 1, 2) is None


def test_apply_movement_insufficient_stock_is_409_without_writes(conn):
    _apply(conn, 1, 1, 10, "receipt", "r1")
    with pytest.raises(ServiceError) as ei:
        _apply(conn, 1, 1, -11, "sale", "order:9")
    assert (ei.value.status, ei.value.code) == (409, INSUFFICIENT_STOCK)
    assert str(ei.value) == "insufficient stock for product 1 at store 1: have 10, need 11"
    assert _count(conn, "stock_movements") == 1
    assert _inv(conn, 1, 1) == (10, 1)
    assert conn.in_transaction is False

    with pytest.raises(ServiceError) as ei:
        _apply(conn, 2, 2, -1, "sale", "order:10")
    assert str(ei.value) == "insufficient stock for product 2 at store 2: have 0, need 1"
    assert _inv(conn, 2, 2) is None
    assert _count(conn, "stock_movements") == 1


def test_apply_movement_requires_an_open_transaction(conn):
    assert conn.in_transaction is False
    with pytest.raises(RuntimeError, match="transaction"):
        ledger.apply_movement(conn, 1, 1, 5, "receipt", None)
    assert _count(conn, "stock_movements") == 0
    assert _count(conn, "inventory") == 0


def test_apply_movement_rejects_zero_delta(conn):
    with pytest.raises(ServiceError) as ei:
        _apply(conn, 1, 1, 0, "adjustment", None)
    assert (ei.value.status, ei.value.code) == (422, VALIDATION)
    assert _count(conn, "stock_movements") == 0


# --------------------------------------------------------------------------- ledger_integrity
def test_integrity_ok_on_consistent_ledger(conn):
    _apply(conn, 1, 1, 10, "receipt")
    _apply(conn, 1, 1, -3, "sale")
    _apply(conn, 1, 2, 4, "receipt")
    rep = ledger.ledger_integrity(conn)
    assert rep["ok"] is True
    assert rep["checked"] == 2
    assert rep["schema_version"] == 2
    assert rep["mismatches"] == []
    assert rep["negative"] == []
    assert rep["chain_breaks"] == []
    assert rep["order_total_mismatches"] == []
    assert rep["missing_balance_after"] == 0


def test_integrity_detects_cache_mismatch(conn):
    _apply(conn, 1, 1, 10, "receipt")
    _apply(conn, 1, 2, 4, "receipt")
    conn.execute("UPDATE inventory SET on_hand = on_hand + 5 WHERE store_id=1 AND product_id=1")
    rep = ledger.ledger_integrity(conn)
    assert rep["ok"] is False
    assert rep["mismatches"] == [{"store_id": 1, "product_id": 1, "on_hand": 15, "ledger": 10}]
    assert rep["chain_breaks"] == []
    assert rep["negative"] == []
    assert rep["checked"] == 2


def test_integrity_detects_chain_break(conn):
    _, m1 = _apply(conn, 1, 1, 10, "receipt")
    _, m2 = _apply(conn, 1, 1, -3, "sale")
    _, m3 = _apply(conn, 1, 1, 5, "return")
    conn.execute("UPDATE stock_movements SET balance_after = 8 WHERE id=?", (m2,))
    rep = ledger.ledger_integrity(conn)
    assert rep["ok"] is False
    assert rep["chain_breaks"] == [{"store_id": 1, "product_id": 1, "movement_id": m2, "expected": 7, "actual": 8}]
    assert rep["mismatches"] == []  # the cache (12) still equals the ledger sum
    assert m1 < m2 < m3 and rep["missing_balance_after"] == 0


def test_integrity_detects_order_total_mismatch(conn):
    conn.execute("INSERT INTO orders (store_id, status, total_cents) VALUES (1, 'placed', 3000)")
    bad = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.executemany(
        "INSERT INTO order_lines (order_id, product_id, quantity, unit_price_cents) VALUES (?,?,?,?)",
        [(bad, 1, 2, 1000), (bad, 2, 1, 2500)],
    )
    conn.execute("INSERT INTO orders (store_id, status, total_cents) VALUES (1, 'placed', 2500)")
    good = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO order_lines (order_id, product_id, quantity, unit_price_cents) VALUES (?,?,?,?)", (good, 2, 1, 2500))
    rep = ledger.ledger_integrity(conn)
    assert rep["ok"] is False
    assert rep["order_total_mismatches"] == [{"order_id": bad, "total_cents": 3000, "lines_total_cents": 4500}]


def test_negative_cache_is_rejected_by_schema_but_negative_ledger_is_reported(conn):
    _apply(conn, 1, 1, 10, "receipt")
    # the v1 CHECK (on_hand >= 0) makes a negative cache impossible on this schema ...
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE inventory SET on_hand = -1 WHERE store_id=1 AND product_id=1")
    # ... so a negative balance can only come from the ledger itself (raw writes bypassing apply_movement);
    # "negative" then reports the ledger balance, because the cache is pinned at zero by the constraint
    _raw_movement(conn, 2, 2, -4)
    rep = ledger.ledger_integrity(conn)
    assert rep["ok"] is False
    assert rep["checked"] == 2
    assert rep["negative"] == [{"store_id": 2, "product_id": 2, "on_hand": -4}]
    assert rep["mismatches"] == [{"store_id": 2, "product_id": 2, "on_hand": 0, "ledger": -4}]
    assert rep["missing_balance_after"] == 1


def test_negative_cache_is_reported_when_the_schema_allows_it(tmp_path):
    conn = db.connect(str(tmp_path / "nocheck.db"))
    conn.executescript(db.SCHEMA_V1.replace("CHECK (on_hand >= 0)", ""))  # a foreign v1 file without the CHECK
    assert db.migrate(conn) == 2
    _seed_catalog(conn)
    _apply(conn, 1, 1, 10, "receipt")
    conn.execute("UPDATE inventory SET on_hand = -2 WHERE store_id=1 AND product_id=1")
    rep = ledger.ledger_integrity(conn)
    assert rep["ok"] is False
    assert rep["negative"] == [{"store_id": 1, "product_id": 1, "on_hand": -2}]
    assert rep["mismatches"] == [{"store_id": 1, "product_id": 1, "on_hand": -2, "ledger": 10}]
    assert ledger.rebuild_balances(conn)["fixed"] == [{"store_id": 1, "product_id": 1, "before": -2, "after": 10}]
    assert ledger.ledger_integrity(conn)["ok"] is True


def test_missing_balance_after_is_informational(conn):
    _apply(conn, 1, 1, 10, "receipt")
    _raw_movement(conn, 1, 1, 2)  # legacy-style row: NULL balance_after
    conn.execute("UPDATE inventory SET on_hand = 12, version = version + 1 WHERE store_id=1 AND product_id=1")
    rep = ledger.ledger_integrity(conn)
    assert rep["ok"] is True
    assert rep["missing_balance_after"] == 1
    assert rep["chain_breaks"] == []
    # subsequent movements continue the running sum from the cache
    new, mid = _apply(conn, 1, 1, -1, "sale")
    assert new == 11 and _movement(conn, mid)["balance_after"] == 11
    assert ledger.ledger_integrity(conn)["chain_breaks"] == []


# --------------------------------------------------------------------------- rebuild_balances
def test_rebuild_repairs_mismatch_and_bumps_version_only_for_changed_rows(conn):
    _apply(conn, 1, 1, 10, "receipt")
    _apply(conn, 1, 2, 4, "receipt")
    conn.execute("UPDATE inventory SET on_hand = 15 WHERE store_id=1 AND product_id=1")
    assert ledger.ledger_integrity(conn)["ok"] is False
    rep = ledger.rebuild_balances(conn)
    assert rep == {"checked": 2, "fixed": [{"store_id": 1, "product_id": 1, "before": 15, "after": 10}], "backfilled": 0, "ok": True}
    assert _inv(conn, 1, 1) == (10, 2)
    assert _inv(conn, 1, 2) == (4, 1)
    assert ledger.ledger_integrity(conn)["ok"] is True
    assert conn.in_transaction is False
    # idempotent
    assert ledger.rebuild_balances(conn) == {"checked": 2, "fixed": [], "backfilled": 0, "ok": True}
    assert _inv(conn, 1, 1) == (10, 2)


def test_rebuild_backfills_null_balance_after_and_inserts_missing_rows(conn):
    _, m1 = _apply(conn, 1, 1, 10, "receipt")
    r1 = _raw_movement(conn, 1, 1, 1)
    r2 = _raw_movement(conn, 2, 1, 7)
    r3 = _raw_movement(conn, 2, 1, -2)
    rep = ledger.ledger_integrity(conn)
    assert rep["missing_balance_after"] == 3
    assert rep["mismatches"] == [
        {"store_id": 1, "product_id": 1, "on_hand": 10, "ledger": 11},
        {"store_id": 2, "product_id": 1, "on_hand": 0, "ledger": 5},
    ]

    out = ledger.rebuild_balances(conn)
    assert out["checked"] == 2 and out["backfilled"] == 3 and out["ok"] is True
    assert out["fixed"] == [
        {"store_id": 1, "product_id": 1, "before": 10, "after": 11},
        {"store_id": 2, "product_id": 1, "before": 0, "after": 5},
    ]
    assert _movement(conn, m1)["balance_after"] == 10
    assert _movement(conn, r1)["balance_after"] == 11
    assert _movement(conn, r2)["balance_after"] == 7
    assert _movement(conn, r3)["balance_after"] == 5
    assert _inv(conn, 1, 1) == (11, 2)
    assert _inv(conn, 2, 1) == (5, 1)
    after = ledger.ledger_integrity(conn)
    assert after["ok"] is True and after["missing_balance_after"] == 0 and after["checked"] == 2
    assert ledger.rebuild_balances(conn) == {"checked": 2, "fixed": [], "backfilled": 0, "ok": True}


def test_rebuild_refuses_a_negative_ledger_balance(conn):
    _apply(conn, 1, 1, 10, "receipt")
    _raw_movement(conn, 2, 2, -4)
    with pytest.raises(ServiceError) as ei:
        ledger.rebuild_balances(conn)
    assert (ei.value.status, ei.value.code) == (409, CONFLICT)
    assert "product 2" in str(ei.value) and "store 2" in str(ei.value)
    assert _inv(conn, 2, 2) is None
    assert conn.in_transaction is False
    assert _inv(conn, 1, 1) == (10, 1)
