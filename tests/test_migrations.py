"""Schema versioning: fresh databases, in-place migration of legacy v0.1 files, idempotency and rollback."""
from __future__ import annotations

import sqlite3

import pytest

from app import db, ledger, schemas, service

TRANSFER_COLUMNS = {"id", "from_store_id", "to_store_id", "product_id", "quantity", "idempotency_key", "request_hash", "created_at"}
NEW_INDEXES = {"idx_orders_status_created", "idx_movements_reason_created", "idx_movements_reference"}
# (store_id, product_id, delta, reason, reference) inserted in this order -> ids 1..6
LEGACY_MOVEMENTS = [
    (1, 1, 20, "receipt", "opening"),
    (1, 1, -3, "sale", "order:1"),
    (2, 1, 4, "receipt", "opening"),
    (1, 2, 3, "receipt", "opening"),
    (1, 1, 5, "return", "cancel:1"),
    (1, 2, -3, "sale", "order:2"),
]
EXPECTED_BALANCES = {1: 20, 2: 17, 3: 4, 4: 3, 5: 22, 6: 0}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def _master(conn: sqlite3.Connection) -> list[tuple]:
    return [tuple(r) for r in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")]


def _legacy_v1(path) -> sqlite3.Connection:
    """A database exactly as v0.1.0 left it: v1 tables, real rows, user_version 0."""
    conn = db.connect(str(path))
    conn.executescript(db.SCHEMA_V1)
    conn.execute("INSERT INTO stores (code, name, region) VALUES ('S1','One','South'), ('S2','Two','West')")
    conn.execute(
        "INSERT INTO products (sku, name, category, price_cents, reorder_point) "
        "VALUES ('SKU-1','Widget','Home',1000,5), ('SKU-2','Gadget','Elec',2500,2)"
    )
    conn.executemany("INSERT INTO stock_movements (store_id, product_id, delta, reason, reference) VALUES (?,?,?,?,?)", LEGACY_MOVEMENTS)
    conn.executemany(
        "INSERT INTO inventory (store_id, product_id, on_hand, version) VALUES (?,?,?,?)", [(1, 1, 22, 3), (2, 1, 4, 1), (1, 2, 0, 2)]
    )
    conn.execute(
        "INSERT INTO orders (store_id, status, idempotency_key, total_cents, created_at) "
        "VALUES (1,'placed','k1',3000,'2026-01-02T03:04:05.678Z')"
    )
    conn.execute("INSERT INTO orders (store_id, status, idempotency_key, total_cents) VALUES (1,'cancelled',NULL,7500)")
    conn.executemany(
        "INSERT INTO order_lines (order_id, product_id, quantity, unit_price_cents) VALUES (?,?,?,?)",
        [(1, 1, 3, 1000), (2, 2, 3, 2500)],
    )
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    return conn


# --------------------------------------------------------------------------- constants
def test_schema_constants_and_frozen_v1_text():
    assert db.SCHEMA_VERSION == 2
    assert db.SCHEMA is db.SCHEMA_V1
    assert set(db.MIGRATIONS) == {2}
    assert all(callable(step) for step in db.MIGRATIONS.values())
    assert "CREATE TABLE IF NOT EXISTS order_lines" in db.SCHEMA_V1
    assert "CHECK (on_hand >= 0)" in db.SCHEMA_V1
    assert "balance_after" not in db.SCHEMA_V1
    assert "transfers" not in db.SCHEMA_V1


# --------------------------------------------------------------------------- fresh database
def test_fresh_db_migrates_to_v2_with_columns_indexes_and_transfers(tmp_path):
    conn = db.connect(str(tmp_path / "fresh.db"))
    assert db.schema_version(conn) == 0
    assert db.migrate(conn) == 2
    assert db.schema_version(conn) == 2
    assert {"request_hash", "updated_at"} <= _columns(conn, "orders")
    assert "updated_at" in _columns(conn, "products")
    assert "balance_after" in _columns(conn, "stock_movements")
    assert "transfers" in _tables(conn)
    assert _columns(conn, "transfers") == TRANSFER_COLUMNS
    assert NEW_INDEXES | {"idx_movements_sp"} <= _indexes(conn)
    assert conn.in_transaction is False


def test_init_schema_is_the_migrate_entry_point(tmp_path):
    conn = db.connect(str(tmp_path / "init.db"))
    db.init_schema(conn)
    assert db.schema_version(conn) == 2
    assert "balance_after" in _columns(conn, "stock_movements")


# --------------------------------------------------------------------------- legacy v0.1 file
def test_legacy_v1_file_migrates_in_place(tmp_path):
    conn = _legacy_v1(tmp_path / "legacy.db")
    tables = ("stores", "products", "inventory", "stock_movements", "orders", "order_lines")
    before_counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}

    assert db.migrate(conn) == 2
    assert db.schema_version(conn) == 2

    balances = {r[0]: r[1] for r in conn.execute("SELECT id, balance_after FROM stock_movements ORDER BY id")}
    assert balances == EXPECTED_BALANCES
    orders = conn.execute("SELECT id, created_at, updated_at, request_hash FROM orders ORDER BY id").fetchall()
    assert len(orders) == 2
    for row in orders:
        assert row["updated_at"] == row["created_at"]
        assert row["request_hash"] is None
    assert orders[0]["updated_at"] == "2026-01-02T03:04:05.678Z"
    assert conn.execute("SELECT updated_at FROM products WHERE id=1").fetchone()[0] is None
    after_counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before_counts}
    assert after_counts == before_counts
    assert [tuple(r) for r in conn.execute("SELECT store_id, product_id, on_hand, version FROM inventory ORDER BY store_id, product_id")] == [
        (1, 1, 22, 3), (1, 2, 0, 2), (2, 1, 4, 1),
    ]
    assert "transfers" in _tables(conn)
    assert NEW_INDEXES <= _indexes(conn)
    assert conn.in_transaction is False


def test_migrate_twice_is_a_noop(tmp_path):
    conn = _legacy_v1(tmp_path / "twice.db")
    assert db.migrate(conn) == 2
    master = _master(conn)
    balances = [tuple(r) for r in conn.execute("SELECT id, balance_after FROM stock_movements ORDER BY id")]
    assert db.migrate(conn) == 2
    assert db.init_schema(conn) == 2
    assert _master(conn) == master
    assert [tuple(r) for r in conn.execute("SELECT id, balance_after FROM stock_movements ORDER BY id")] == balances
    assert db.schema_version(conn) == 2


def test_v01_code_keeps_working_on_v2_schema(tmp_path):
    """A v0.1 process and v0.2 code can share one migrated file (rollback scenario in docs/migrations.md).

    "v0.1 code" is simulated faithfully with the exact SQL that v0.1's ``service._apply_movement`` and
    ``place_order`` issued: explicit column lists that know nothing about the v2 columns.
    """
    conn = _legacy_v1(tmp_path / "compat.db")
    assert db.migrate(conn) == 2

    # --- v0.1 code: every new column is nullable, so the old statements still succeed -------------
    conn.execute("INSERT INTO orders (store_id, status, idempotency_key, total_cents) VALUES (?,?,?,?)", (1, "placed", "k-v01", 0))
    row = conn.execute("SELECT request_hash, updated_at FROM orders ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(row) == (None, None)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO inventory (store_id, product_id, on_hand, version) VALUES (?,?,?,1) "
            "ON CONFLICT(store_id, product_id) DO UPDATE SET on_hand = excluded.on_hand, version = version + 1",
            (2, 1, 4 + 6),
        )
        cur = conn.execute(
            "INSERT INTO stock_movements (store_id, product_id, delta, reason, reference) VALUES (?,?,?,?,?)",
            (2, 1, 6, "receipt", "v01-receipt"),
        )
    legacy_movement_id = cur.lastrowid
    assert conn.execute("SELECT balance_after FROM stock_movements WHERE id=?", (legacy_movement_id,)).fetchone()[0] is None
    assert tuple(conn.execute("SELECT on_hand, version FROM inventory WHERE store_id=2 AND product_id=1").fetchone()) == (10, 2)
    rep = ledger.ledger_integrity(conn)
    assert rep["ok"] is True  # cache == ledger; the missing running balance is informational only
    assert rep["missing_balance_after"] == 1
    assert rep["chain_breaks"] == [] and rep["mismatches"] == []

    # --- v0.2 code on the same file writes balance_after -------------------------------------------
    store = service.create_store(conn, schemas.StoreIn(code="S3", name="Three", region="North"))
    inv = service.adjust_stock(conn, store["id"], 1, schemas.StockAdjust(delta=5, reason="receipt", reference="opening"))
    assert inv["on_hand"] == 5
    order_in = schemas.OrderIn(store_id=store["id"], lines=[schemas.OrderLineIn(product_id=1, quantity=2)])
    order, created = service.place_order(conn, order_in, "k-compat")
    assert created and order["total_cents"] == 2000
    assert service.get_inventory_row(conn, store["id"], 1)["on_hand"] == 3
    newest = conn.execute("SELECT store_id, product_id, delta, balance_after FROM stock_movements ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(newest) == (store["id"], 1, -2, 3)  # balance_after == on_hand right after the movement
    assert service.ledger_integrity(conn)["ok"] is True

    # --- the rebuild backfills the v0.1 row from the running ledger sum ----------------------------
    out = ledger.rebuild_balances(conn)
    assert (out["backfilled"], out["fixed"], out["ok"], out["checked"]) == (1, [], True, 4)
    assert conn.execute("SELECT balance_after FROM stock_movements WHERE id=?", (legacy_movement_id,)).fetchone()[0] == 10  # 4 legacy + 6
    after = ledger.ledger_integrity(conn)
    assert after["ok"] is True and after["missing_balance_after"] == 0 and after["chain_breaks"] == []


def test_transfers_table_constraints(tmp_path):
    conn = db.connect(str(tmp_path / "xfer.db"))
    assert db.migrate(conn) == 2
    conn.execute("INSERT INTO stores (code, name, region) VALUES ('S1','One','South'), ('S2','Two','West')")
    conn.execute("INSERT INTO products (sku, name, category, price_cents) VALUES ('SKU-1','Widget','Home',1000)")
    conn.execute("INSERT INTO transfers (from_store_id, to_store_id, product_id, quantity, idempotency_key) VALUES (1, 2, 1, 3, 'k1')")
    conn.execute("INSERT INTO transfers (from_store_id, to_store_id, product_id, quantity) VALUES (1, 2, 1, 3)")
    conn.execute("INSERT INTO transfers (from_store_id, to_store_id, product_id, quantity) VALUES (2, 1, 1, 1)")
    row = conn.execute("SELECT created_at, request_hash FROM transfers WHERE id=1").fetchone()
    assert row["created_at"].endswith("Z") and row["request_hash"] is None
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO transfers (from_store_id, to_store_id, product_id, quantity) VALUES (1, 1, 1, 3)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO transfers (from_store_id, to_store_id, product_id, quantity) VALUES (1, 2, 1, 0)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO transfers (from_store_id, to_store_id, product_id, quantity, idempotency_key) VALUES (1, 2, 1, 3, 'k1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO transfers (from_store_id, to_store_id, product_id, quantity) VALUES (1, 99, 1, 3)")


# --------------------------------------------------------------------------- failure handling
def test_failed_step_rolls_back_ddl_and_version(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "broken.db"))
    real = dict(db.MIGRATIONS)

    def broken(c: sqlite3.Connection) -> None:
        c.execute("ALTER TABLE orders ADD COLUMN request_hash TEXT")
        c.execute("CREATE TABLE IF NOT EXISTS transfers (id INTEGER PRIMARY KEY)")
        raise RuntimeError("simulated failure midway through v2")

    monkeypatch.setattr(db, "MIGRATIONS", {**real, 2: broken})
    with pytest.raises(RuntimeError, match="midway"):
        db.migrate(conn)
    assert db.schema_version(conn) == 1
    assert "request_hash" not in _columns(conn, "orders")
    assert "transfers" not in _tables(conn)
    assert conn.in_transaction is False

    monkeypatch.setattr(db, "MIGRATIONS", real)
    assert db.migrate(conn) == 2
    assert "request_hash" in _columns(conn, "orders")
    assert _columns(conn, "transfers") == TRANSFER_COLUMNS


def test_failed_later_step_leaves_current_version_intact(tmp_path, monkeypatch):
    conn = _legacy_v1(tmp_path / "v3.db")
    assert db.migrate(conn) == 2

    def broken_v3(c: sqlite3.Connection) -> None:
        c.execute("CREATE TABLE IF NOT EXISTS probe_v3 (id INTEGER PRIMARY KEY)")
        c.execute("INSERT INTO probe_v3 (id) VALUES (1)")
        raise RuntimeError("v3 exploded")

    monkeypatch.setattr(db, "MIGRATIONS", {**db.MIGRATIONS, 3: broken_v3})
    monkeypatch.setattr(db, "SCHEMA_VERSION", 3)
    with pytest.raises(RuntimeError, match="v3 exploded"):
        db.migrate(conn)
    assert db.schema_version(conn) == 2
    assert "probe_v3" not in _tables(conn)
    assert conn.in_transaction is False
    assert {r[0]: r[1] for r in conn.execute("SELECT id, balance_after FROM stock_movements")} == EXPECTED_BALANCES


def test_newer_database_version_is_left_alone(tmp_path):
    conn = db.connect(str(tmp_path / "newer.db"))
    db.migrate(conn)
    conn.execute("PRAGMA user_version = 99")
    master = _master(conn)
    assert db.migrate(conn) == 99
    assert _master(conn) == master
