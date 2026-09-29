"""Business logic. Every function takes an open connection and is transactional
where it mutates state. Raises ``ServiceError`` with an HTTP-ish status code so
the API layer stays thin.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import schemas
from .db import transaction


@dataclass
class ServiceError(Exception):
    status: int
    detail: str

    def __str__(self) -> str:
        return self.detail


# --------------------------------------------------------------------------- stores / products
def create_store(conn: sqlite3.Connection, s: schemas.StoreIn) -> dict:
    try:
        cur = conn.execute("INSERT INTO stores (code, name, region) VALUES (?,?,?)", (s.code, s.name, s.region))
    except sqlite3.IntegrityError:
        raise ServiceError(409, f"store code {s.code!r} already exists")
    return dict(conn.execute("SELECT * FROM stores WHERE id=?", (cur.lastrowid,)).fetchone())


def list_stores(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM stores ORDER BY id")]


def create_product(conn: sqlite3.Connection, p: schemas.ProductIn) -> dict:
    try:
        cur = conn.execute(
            "INSERT INTO products (sku, name, category, price_cents, reorder_point) VALUES (?,?,?,?,?)",
            (p.sku, p.name, p.category, p.price_cents, p.reorder_point),
        )
    except sqlite3.IntegrityError:
        raise ServiceError(409, f"sku {p.sku!r} already exists")
    return get_product(conn, cur.lastrowid)


def get_product(conn: sqlite3.Connection, pid: int) -> dict:
    row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not row:
        raise ServiceError(404, f"product {pid} not found")
    d = dict(row)
    d["active"] = bool(d["active"])
    return d


def list_products(conn: sqlite3.Connection, q: str | None, category: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    where, params = ["active = 1"], []
    if q:
        where.append("(name LIKE ? OR sku LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if category:
        where.append("category = ?")
        params.append(category)
    w = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM products WHERE {w}", params).fetchone()[0]
    rows = conn.execute(f"SELECT * FROM products WHERE {w} ORDER BY id LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["active"] = bool(d["active"])
        out.append(d)
    return out, total


# --------------------------------------------------------------------------- inventory
def _ensure_exists(conn: sqlite3.Connection, table: str, id_: int) -> None:
    if not conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (id_,)).fetchone():
        raise ServiceError(404, f"{table[:-1]} {id_} not found")


def _apply_movement(conn: sqlite3.Connection, store_id: int, product_id: int, delta: int, reason: str, reference: str | None) -> int:
    """Append to the ledger and update the cached balance. Must run inside a transaction.
    Returns new on_hand. Raises 409 on insufficient stock."""
    row = conn.execute("SELECT on_hand FROM inventory WHERE store_id=? AND product_id=?", (store_id, product_id)).fetchone()
    current = row["on_hand"] if row else 0
    new = current + delta
    if new < 0:
        raise ServiceError(409, f"insufficient stock for product {product_id} at store {store_id}: have {current}, need {-delta}")
    conn.execute(
        """INSERT INTO inventory (store_id, product_id, on_hand, version) VALUES (?,?,?,1)
           ON CONFLICT(store_id, product_id) DO UPDATE SET on_hand = excluded.on_hand, version = version + 1""",
        (store_id, product_id, new),
    )
    conn.execute(
        "INSERT INTO stock_movements (store_id, product_id, delta, reason, reference) VALUES (?,?,?,?,?)",
        (store_id, product_id, delta, reason, reference),
    )
    return new


def adjust_stock(conn: sqlite3.Connection, store_id: int, product_id: int, adj: schemas.StockAdjust) -> dict:
    with transaction(conn):
        _ensure_exists(conn, "stores", store_id)
        _ensure_exists(conn, "products", product_id)
        if adj.expected_version is not None:
            row = conn.execute("SELECT version FROM inventory WHERE store_id=? AND product_id=?", (store_id, product_id)).fetchone()
            current_version = row["version"] if row else 0
            if current_version != adj.expected_version:
                raise ServiceError(409, f"version conflict: expected {adj.expected_version}, current {current_version}")
        _apply_movement(conn, store_id, product_id, adj.delta, adj.reason, adj.reference)
    return get_inventory_row(conn, store_id, product_id)


def get_inventory_row(conn: sqlite3.Connection, store_id: int, product_id: int) -> dict:
    row = conn.execute(
        """SELECT i.store_id, s.code AS store_code, i.product_id, p.sku, p.name, i.on_hand, p.reorder_point, i.version,
                  (i.on_hand <= p.reorder_point) AS below_reorder
           FROM inventory i JOIN stores s ON s.id=i.store_id JOIN products p ON p.id=i.product_id
           WHERE i.store_id=? AND i.product_id=?""",
        (store_id, product_id),
    ).fetchone()
    if not row:
        raise ServiceError(404, "no inventory record")
    d = dict(row)
    d["below_reorder"] = bool(d["below_reorder"])
    return d


def list_inventory(conn: sqlite3.Connection, store_id: int | None, low_stock_only: bool, limit: int, offset: int) -> tuple[list[dict], int]:
    where, params = ["1=1"], []
    if store_id is not None:
        where.append("i.store_id = ?")
        params.append(store_id)
    if low_stock_only:
        where.append("i.on_hand <= p.reorder_point")
    w = " AND ".join(where)
    base = "FROM inventory i JOIN stores s ON s.id=i.store_id JOIN products p ON p.id=i.product_id WHERE " + w
    total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT i.store_id, s.code AS store_code, i.product_id, p.sku, p.name, i.on_hand, p.reorder_point, i.version,
                   (i.on_hand <= p.reorder_point) AS below_reorder {base}
            ORDER BY i.store_id, i.product_id LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["below_reorder"] = bool(d["below_reorder"])
        out.append(d)
    return out, total


def transfer(conn: sqlite3.Connection, t: schemas.TransferIn) -> dict:
    if t.from_store_id == t.to_store_id:
        raise ServiceError(422, "from_store_id and to_store_id must differ")
    ref = f"xfer:{t.from_store_id}->{t.to_store_id}"
    with transaction(conn):
        _ensure_exists(conn, "stores", t.from_store_id)
        _ensure_exists(conn, "stores", t.to_store_id)
        _ensure_exists(conn, "products", t.product_id)
        _apply_movement(conn, t.from_store_id, t.product_id, -t.quantity, "transfer_out", ref)
        _apply_movement(conn, t.to_store_id, t.product_id, t.quantity, "transfer_in", ref)
    return {
        "from": get_inventory_row(conn, t.from_store_id, t.product_id),
        "to": get_inventory_row(conn, t.to_store_id, t.product_id),
    }


def movements(conn: sqlite3.Connection, store_id: int, product_id: int, limit: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM stock_movements WHERE store_id=? AND product_id=? ORDER BY id DESC LIMIT ?",
            (store_id, product_id, limit),
        )
    ]


# --------------------------------------------------------------------------- orders
def get_order(conn: sqlite3.Connection, order_id: int) -> dict:
    o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o:
        raise ServiceError(404, f"order {order_id} not found")
    lines = conn.execute(
        """SELECT ol.product_id, p.sku, ol.quantity, ol.unit_price_cents, ol.quantity*ol.unit_price_cents AS line_total_cents
           FROM order_lines ol JOIN products p ON p.id=ol.product_id WHERE ol.order_id=? ORDER BY ol.product_id""",
        (order_id,),
    ).fetchall()
    d = dict(o)
    d["lines"] = [dict(l) for l in lines]
    return d


def place_order(conn: sqlite3.Connection, o: schemas.OrderIn, idempotency_key: str | None) -> tuple[dict, bool]:
    """Returns (order, created). If the idempotency key was seen before, returns
    the original order with created=False and performs no writes."""
    if idempotency_key:
        existing = conn.execute("SELECT id FROM orders WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing:
            return get_order(conn, existing["id"]), False

    with transaction(conn):
        _ensure_exists(conn, "stores", o.store_id)
        # Lock order is deterministic (sorted by product_id) — avoids deadlocks in
        # engines with row locks; harmless in SQLite but a good habit.
        total = 0
        priced: list[tuple[int, int, int]] = []
        for line in sorted(o.lines, key=lambda l: l.product_id):
            p = conn.execute("SELECT price_cents, active FROM products WHERE id=?", (line.product_id,)).fetchone()
            if not p or not p["active"]:
                raise ServiceError(404, f"product {line.product_id} not found or inactive")
            priced.append((line.product_id, line.quantity, p["price_cents"]))
            total += line.quantity * p["price_cents"]

        try:
            cur = conn.execute(
                "INSERT INTO orders (store_id, status, idempotency_key, total_cents) VALUES (?,?,?,?)",
                (o.store_id, "placed", idempotency_key, total),
            )
        except sqlite3.IntegrityError:
            # Lost a race with an identical request; surface the winner.
            raise ServiceError(409, "duplicate idempotency key (concurrent request)")
        order_id = cur.lastrowid
        for pid, qty, price in priced:
            _apply_movement(conn, o.store_id, pid, -qty, "sale", f"order:{order_id}")
            conn.execute(
                "INSERT INTO order_lines (order_id, product_id, quantity, unit_price_cents) VALUES (?,?,?,?)",
                (order_id, pid, qty, price),
            )
    return get_order(conn, order_id), True


def cancel_order(conn: sqlite3.Connection, order_id: int) -> dict:
    with transaction(conn):
        o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not o:
            raise ServiceError(404, f"order {order_id} not found")
        if o["status"] == "cancelled":
            return get_order(conn, order_id)  # idempotent
        if o["status"] == "fulfilled":
            raise ServiceError(409, "fulfilled orders cannot be cancelled")
        for l in conn.execute("SELECT product_id, quantity FROM order_lines WHERE order_id=?", (order_id,)):
            _apply_movement(conn, o["store_id"], l["product_id"], l["quantity"], "return", f"cancel:{order_id}")
        conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
    return get_order(conn, order_id)


def fulfil_order(conn: sqlite3.Connection, order_id: int) -> dict:
    with transaction(conn):
        o = conn.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
        if not o:
            raise ServiceError(404, f"order {order_id} not found")
        if o["status"] == "cancelled":
            raise ServiceError(409, "cancelled orders cannot be fulfilled")
        conn.execute("UPDATE orders SET status='fulfilled' WHERE id=?", (order_id,))
    return get_order(conn, order_id)


def list_orders(conn: sqlite3.Connection, store_id: int | None, status: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    where, params = ["1=1"], []
    if store_id is not None:
        where.append("store_id=?"); params.append(store_id)
    if status:
        where.append("status=?"); params.append(status)
    w = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM orders WHERE {w}", params).fetchone()[0]
    ids = [r["id"] for r in conn.execute(f"SELECT id FROM orders WHERE {w} ORDER BY id DESC LIMIT ? OFFSET ?", params + [limit, offset])]
    return [get_order(conn, i) for i in ids], total


# --------------------------------------------------------------------------- reports
def reorder_report(conn: sqlite3.Connection) -> list[dict]:
    """SKUs at/below reorder point, with 30-day sales velocity and suggested order qty."""
    return [
        dict(r)
        for r in conn.execute(
            """WITH velocity AS (
                   SELECT store_id, product_id, -SUM(delta) AS sold_30d
                   FROM stock_movements
                   WHERE reason='sale' AND created_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days')
                   GROUP BY store_id, product_id)
               SELECT s.code AS store_code, p.sku, p.name, i.on_hand, p.reorder_point,
                      COALESCE(v.sold_30d, 0) AS sold_30d,
                      MAX(p.reorder_point * 2 - i.on_hand, COALESCE(v.sold_30d,0)) AS suggested_qty
               FROM inventory i
               JOIN products p ON p.id=i.product_id
               JOIN stores s ON s.id=i.store_id
               LEFT JOIN velocity v ON v.store_id=i.store_id AND v.product_id=i.product_id
               WHERE i.on_hand <= p.reorder_point
               ORDER BY (p.reorder_point - i.on_hand) DESC, s.code"""
        )
    ]


def ledger_integrity(conn: sqlite3.Connection) -> dict:
    """Verify cached on_hand equals the ledger sum for every (store, product)."""
    bad = conn.execute(
        """SELECT i.store_id, i.product_id, i.on_hand, COALESCE(SUM(m.delta),0) AS ledger
           FROM inventory i LEFT JOIN stock_movements m ON m.store_id=i.store_id AND m.product_id=i.product_id
           GROUP BY i.store_id, i.product_id HAVING i.on_hand <> ledger"""
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
    return {"checked": total, "mismatches": [dict(b) for b in bad], "ok": not bad}
