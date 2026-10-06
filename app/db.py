"""SQLite connection management, schema and forward-only migrations.

Design notes
------------
* Every stock mutation is written as an immutable row in ``stock_movements``;
  ``inventory.on_hand`` is a materialised cache updated in the same
  transaction, so the ledger can always rebuild the balance.
* ``orders.idempotency_key`` is UNIQUE — retrying a POST /orders with the same
  key returns the original order instead of double-reserving stock.
* ``inventory.version`` enables optimistic concurrency control on adjustments.
* The schema is versioned through ``PRAGMA user_version``. ``SCHEMA_V1`` is the
  frozen v0.1 text; later versions are additive steps in ``MIGRATIONS`` applied
  by ``migrate()`` (see ``docs/migrations.md``).
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.environ.get("STOCKLINE_DB", str(Path(__file__).resolve().parent.parent / "stockline.db"))

SCHEMA_V1 = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS stores (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    code     TEXT NOT NULL UNIQUE,
    name     TEXT NOT NULL,
    region   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sku           TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    category      TEXT NOT NULL,
    price_cents   INTEGER NOT NULL CHECK (price_cents >= 0),
    reorder_point INTEGER NOT NULL DEFAULT 10 CHECK (reorder_point >= 0),
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS inventory (
    store_id   INTEGER NOT NULL REFERENCES stores(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    on_hand    INTEGER NOT NULL DEFAULT 0 CHECK (on_hand >= 0),
    version    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (store_id, product_id)
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id   INTEGER NOT NULL REFERENCES stores(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    delta      INTEGER NOT NULL CHECK (delta <> 0),
    reason     TEXT NOT NULL CHECK (reason IN ('receipt','sale','return','adjustment','transfer_in','transfer_out')),
    reference  TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_movements_sp ON stock_movements(store_id, product_id, created_at);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    status          TEXT NOT NULL CHECK (status IN ('placed','cancelled','fulfilled')),
    idempotency_key TEXT UNIQUE,
    total_cents     INTEGER NOT NULL CHECK (total_cents >= 0),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS order_lines (
    order_id         INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id       INTEGER NOT NULL REFERENCES products(id),
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    PRIMARY KEY (order_id, product_id)
);
"""
SCHEMA = SCHEMA_V1  # compatibility alias; the v1 text is frozen

SCHEMA_VERSION = 2

# v2: request fingerprints for idempotency, status timestamps, running ledger balance,
# transfers as entities, and indexes for the feeds/reports. Every column is nullable,
# so v0.1 code (SELECT * + INSERT with explicit columns) keeps working on a v2 file.
V2_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE orders ADD COLUMN request_hash TEXT",
    "ALTER TABLE orders ADD COLUMN updated_at TEXT",
    "ALTER TABLE products ADD COLUMN updated_at TEXT",
    "ALTER TABLE stock_movements ADD COLUMN balance_after INTEGER",
    """CREATE TABLE IF NOT EXISTS transfers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    from_store_id   INTEGER NOT NULL REFERENCES stores(id),
    to_store_id     INTEGER NOT NULL REFERENCES stores(id),
    product_id      INTEGER NOT NULL REFERENCES products(id),
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    idempotency_key TEXT UNIQUE,
    request_hash    TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (from_store_id <> to_store_id)
)""",
    "CREATE INDEX IF NOT EXISTS idx_orders_status_created   ON orders(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_movements_reason_created ON stock_movements(reason, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_movements_reference      ON stock_movements(reference)",
    "UPDATE orders SET updated_at = created_at WHERE updated_at IS NULL",
    """UPDATE stock_movements SET balance_after = (
    SELECT SUM(m2.delta) FROM stock_movements m2
    WHERE m2.store_id = stock_movements.store_id AND m2.product_id = stock_movements.product_id
      AND m2.id <= stock_movements.id) WHERE balance_after IS NULL""",
)


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open a connection (autocommit mode, Row factory, foreign keys on, 5 s busy timeout)."""
    conn = sqlite3.connect(path or DB_PATH, isolation_level=None, check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    """Current schema version of the database file (``PRAGMA user_version``; 0 = never migrated)."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _migrate_v2(conn: sqlite3.Connection) -> None:
    for statement in V2_STATEMENTS:
        conn.execute(statement)


# target version -> step; each step runs inside one IMMEDIATE transaction with plain execute() calls.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {2: _migrate_v2}


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the database up to ``SCHEMA_VERSION`` and return the resulting version.

    * ``user_version == 0`` (fresh file *or* a legacy v0.1 file whose tables already exist):
      run ``SCHEMA_V1`` (all ``IF NOT EXISTS``, so existing tables and rows are untouched) and stamp version 1.
    * Then apply every registered step above the current version in order; each step and its
      ``PRAGMA user_version`` stamp commit together, so a failing step leaves the file at the previous version.
    * Files already at or beyond ``SCHEMA_VERSION`` are left alone.
    """
    if conn.in_transaction:
        raise RuntimeError("migrate() must be called outside a transaction")
    version = schema_version(conn)
    if version == 0:
        conn.executescript(SCHEMA_V1)
        conn.execute("PRAGMA user_version = 1")
        version = 1
    for target in range(version + 1, SCHEMA_VERSION + 1):
        step = MIGRATIONS.get(target)
        if step is None:
            raise RuntimeError(f"no migration registered for schema version {target}")
        with transaction(conn):
            step(conn)
            conn.execute(f"PRAGMA user_version = {int(target)}")
    return schema_version(conn)


def init_schema(conn: sqlite3.Connection) -> int:
    """Create or upgrade the schema (alias of ``migrate``, kept for the pool and the bridge)."""
    return migrate(conn)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """IMMEDIATE transaction: takes the write lock up-front so concurrent
    writers serialise instead of failing mid-way."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
