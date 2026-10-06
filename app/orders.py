"""Orders domain: placement with fingerprinted idempotency, cancel/fulfil, listing and CSV export.

Browser-safe (standard library + pydantic, no framework imports). Every function takes an open
connection first and raises ``ServiceError`` with the machine-readable codes from ``app.common``.
``BRIDGE_ROUTES`` at the bottom mirrors ``app/routers/orders.py`` — same paths, methods and query
bounds — so the Pyodide demo runs this exact module.

Idempotency (one rule, applied twice)
-------------------------------------
A request carrying ``Idempotency-Key`` is fingerprinted with ``request_hash`` of
``{"store_id": s, "lines": [[product_id, quantity], ...]}`` (lines sorted by product id, so their
order in the request is irrelevant) and the fingerprint is stored next to the key.  A later request
with the same key replays the stored order — ``(order, False)``, nothing written — when the stored
fingerprint is equal or ``NULL`` (rows written before fingerprints existed); a different fingerprint
is rejected with 422 ``idempotency_key_reuse``.  The lookup runs once lock-free (fast path) and again
inside ``BEGIN IMMEDIATE`` (authoritative), so a concurrent retry can never reserve stock twice; the
UNIQUE constraint on the key stays as the last line of defence (409 ``idempotency_conflict``).
Keys are global (not per store) and never expire.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from pydantic import BaseModel

from . import common, schemas
from .common import (
    IDEMPOTENCY_CONFLICT,
    IDEMPOTENCY_KEY_REUSE,
    INVALID_STATE,
    PRODUCT_INACTIVE,
    VALIDATION,
    BridgeCall,
    BridgeRoute,
    ServiceError,
    csv_filename,
    csv_text,
    not_found,
    now_iso,
    page,
    request_hash,
)
from .db import transaction
from .ledger import apply_movement

ORDER_STATUSES: tuple[str, ...] = ("placed", "cancelled", "fulfilled")
ORDER_CSV_COLUMNS: list[str] = [
    "order_id", "store_id", "status", "created_at", "updated_at", "idempotency_key",
    "product_id", "sku", "quantity", "unit_price_cents", "line_total_cents", "order_total_cents",
]
KEY_MAX_LENGTH = 64  # Idempotency-Key bound, enforced by the router (Header max_length) and the bridge alike
LIST_LIMIT_DEFAULT = 20
LIST_LIMIT_MAX = 100
CSV_STEM = "orders"

# ``request_hash`` is internal: it is never selected, so it can never leak into a response.
_ORDER_SELECT = "SELECT id, store_id, status, idempotency_key, total_cents, created_at, updated_at FROM orders"
_LINES_SELECT = """SELECT ol.order_id, ol.product_id, p.sku, ol.quantity, ol.unit_price_cents,
                          ol.quantity * ol.unit_price_cents AS line_total_cents
                   FROM order_lines ol JOIN products p ON p.id = ol.product_id
                   WHERE ol.order_id IN ({placeholders}) ORDER BY ol.order_id, ol.product_id"""
_EXPORT_SELECT = """SELECT o.id AS order_id, o.store_id, o.status, o.created_at, o.updated_at, o.idempotency_key,
                           ol.product_id, p.sku, ol.quantity, ol.unit_price_cents,
                           ol.quantity * ol.unit_price_cents AS line_total_cents, o.total_cents AS order_total_cents
                    FROM orders o JOIN order_lines ol ON ol.order_id = o.id JOIN products p ON p.id = ol.product_id
                    WHERE {where} ORDER BY o.created_at DESC, o.id DESC, ol.product_id LIMIT ?"""


class OrderOut(schemas.Order):
    """Order as returned by the API: the v0.1 ``Order`` plus ``updated_at`` (set on every status change)."""

    updated_at: str | None = None


class OrderPage(BaseModel):
    """One page of orders; ``items`` is typed so the OpenAPI schema shows real objects."""

    items: list[OrderOut]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- internals
def _canonical(o: schemas.OrderIn) -> dict:
    """Fingerprint input: the store plus ``[product_id, quantity]`` pairs sorted by product id."""
    return {"store_id": o.store_id, "lines": sorted(([ln.product_id, ln.quantity] for ln in o.lines), key=lambda pair: pair[0])}


def _attach_lines(conn: sqlite3.Connection, items: list[dict]) -> list[dict]:
    """Add ``lines`` to every order dict with ONE query (``WHERE order_id IN (...)``) — no N+1."""
    if not items:
        return items
    ids = [item["id"] for item in items]
    by_order: dict[int, list[dict]] = {order_id: [] for order_id in ids}
    for row in conn.execute(_LINES_SELECT.format(placeholders=",".join("?" * len(ids))), ids):
        line = dict(row)
        by_order[line.pop("order_id")].append(line)
    for item in items:
        item["lines"] = by_order[item["id"]]
    return items


def _find_replay(conn: sqlite3.Connection, key: str, fingerprint: str) -> int | None:
    """Id of the order stored under ``key`` when this request may replay it; ``None`` when the key is unused.

    A stored fingerprint that differs from ``fingerprint`` raises 422 ``idempotency_key_reuse``; a ``NULL``
    fingerprint (row written before fingerprints existed) replays unconditionally.  Fingerprints are never
    rewritten, so the verdict is final whether it is reached outside or inside the write lock.
    """
    row = conn.execute("SELECT id, request_hash FROM orders WHERE idempotency_key = ?", (key,)).fetchone()
    if row is None:
        return None
    if row["request_hash"] is not None and row["request_hash"] != fingerprint:
        raise ServiceError(422, "Idempotency-Key reused with a different request body", IDEMPOTENCY_KEY_REUSE)
    return int(row["id"])


def _ensure_store(conn: sqlite3.Connection, store_id: int) -> None:
    if conn.execute("SELECT 1 FROM stores WHERE id = ?", (store_id,)).fetchone() is None:
        raise not_found("store", store_id)


def _price_lines(conn: sqlite3.Connection, o: schemas.OrderIn) -> list[tuple[int, int, int]]:
    """``(product_id, quantity, unit_price_cents)`` per line in product-id order (deterministic lock order).

    Unknown product → 404 ``not_found``; inactive product → 409 ``product_inactive``.
    """
    priced: list[tuple[int, int, int]] = []
    for line in sorted(o.lines, key=lambda ln: ln.product_id):
        product = conn.execute("SELECT price_cents, active FROM products WHERE id = ?", (line.product_id,)).fetchone()
        if product is None:
            raise not_found("product", line.product_id)
        if not product["active"]:
            raise ServiceError(409, f"product {line.product_id} is inactive", PRODUCT_INACTIVE)
        priced.append((line.product_id, line.quantity, int(product["price_cents"])))
    return priced


def _filters(store_id: int | None, status: str | None, prefix: str = "") -> tuple[str, list[Any]]:
    """``WHERE`` clause and parameters shared by listing and export; an unknown status is a 422."""
    if status and status not in ORDER_STATUSES:
        raise ServiceError(422, f"status must be one of {', '.join(ORDER_STATUSES)}", VALIDATION)
    where, params = ["1=1"], []
    if store_id is not None:
        where.append(f"{prefix}store_id = ?")
        params.append(store_id)
    if status:
        where.append(f"{prefix}status = ?")
        params.append(status)
    return " AND ".join(where), params


# --------------------------------------------------------------------------- public API
def get_order(conn: sqlite3.Connection, order_id: int) -> dict:
    """One order with its lines (``updated_at`` included, ``request_hash`` never); 404 ``not_found`` when missing."""
    row = conn.execute(f"{_ORDER_SELECT} WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise not_found("order", order_id)
    return _attach_lines(conn, [dict(row)])[0]


def place_order(conn: sqlite3.Connection, o: schemas.OrderIn, idempotency_key: str | None) -> tuple[dict, bool]:
    """Reserve stock for every line in one IMMEDIATE transaction; returns ``(order, created)``.

    Prices are snapshotted server-side. With an idempotency key the request is fingerprinted and checked
    lock-free first, then again under the write lock (see the module docstring); a replay returns the stored
    order with ``created=False`` and writes nothing. Errors: 404 ``not_found`` (store or product), 409
    ``product_inactive``, 409 ``insufficient_stock`` (nothing written — all lines or none), 422
    ``idempotency_key_reuse``, 409 ``idempotency_conflict`` (lost race on the UNIQUE key).
    """
    key = idempotency_key or None
    fingerprint = request_hash(_canonical(o))
    if key:
        existing = _find_replay(conn, key, fingerprint)
        if existing is not None:
            return get_order(conn, existing), False
    with transaction(conn):
        if key:
            existing = _find_replay(conn, key, fingerprint)  # authoritative: nobody can insert this key while we hold the lock
            if existing is not None:
                return get_order(conn, existing), False
        _ensure_store(conn, o.store_id)
        priced = _price_lines(conn, o)
        stamp = now_iso()
        try:
            cur = conn.execute(
                "INSERT INTO orders (store_id, status, idempotency_key, request_hash, total_cents, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (o.store_id, "placed", key, fingerprint if key else None, sum(qty * price for _pid, qty, price in priced), stamp, stamp),
            )
        except sqlite3.IntegrityError as exc:
            raise ServiceError(409, "duplicate idempotency key (concurrent request)", IDEMPOTENCY_CONFLICT) from exc
        order_id = int(cur.lastrowid)
        for product_id, quantity, unit_price in priced:
            apply_movement(conn, o.store_id, product_id, -quantity, "sale", f"order:{order_id}")
            conn.execute(
                "INSERT INTO order_lines (order_id, product_id, quantity, unit_price_cents) VALUES (?,?,?,?)",
                (order_id, product_id, quantity, unit_price),
            )
    return get_order(conn, order_id), True


def cancel_order(conn: sqlite3.Connection, order_id: int) -> dict:
    """Cancel a placed order and return its stock (``return`` movements referencing ``cancel:{id}``).

    Idempotent: an already cancelled order is returned as is; a fulfilled one is 409 ``invalid_state``.
    """
    with transaction(conn):
        row = conn.execute("SELECT store_id, status FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            raise not_found("order", order_id)
        if row["status"] == "cancelled":
            return get_order(conn, order_id)
        if row["status"] == "fulfilled":
            raise ServiceError(409, "fulfilled orders cannot be cancelled", INVALID_STATE)
        lines = conn.execute("SELECT product_id, quantity FROM order_lines WHERE order_id = ? ORDER BY product_id", (order_id,)).fetchall()
        for line in lines:
            apply_movement(conn, row["store_id"], line["product_id"], line["quantity"], "return", f"cancel:{order_id}")
        conn.execute("UPDATE orders SET status = 'cancelled', updated_at = ? WHERE id = ?", (now_iso(), order_id))
    return get_order(conn, order_id)


def fulfil_order(conn: sqlite3.Connection, order_id: int) -> dict:
    """Mark a placed order fulfilled (the reserved stock ships; no ledger movement).

    Idempotent: an already fulfilled order is returned as is; a cancelled one is 409 ``invalid_state``.
    """
    with transaction(conn):
        row = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            raise not_found("order", order_id)
        if row["status"] == "fulfilled":
            return get_order(conn, order_id)
        if row["status"] == "cancelled":
            raise ServiceError(409, "cancelled orders cannot be fulfilled", INVALID_STATE)
        conn.execute("UPDATE orders SET status = 'fulfilled', updated_at = ? WHERE id = ?", (now_iso(), order_id))
    return get_order(conn, order_id)


def list_orders(conn: sqlite3.Connection, store_id: int | None, status: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    """Orders newest first (``created_at``, then id) with their lines; returns ``(items, total)`` in three queries."""
    where, params = _filters(store_id, status)
    total = conn.execute(f"SELECT COUNT(*) FROM orders WHERE {where}", params).fetchone()[0]
    rows = conn.execute(f"{_ORDER_SELECT} WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
    return _attach_lines(conn, [dict(row) for row in rows]), int(total)


def export_orders_csv(conn: sqlite3.Connection, store_id: int | None, status: str | None) -> tuple[str, int, bool]:
    """CSV with one row per order line (``ORDER_CSV_COLUMNS``), newest order first; returns ``(csv_text, row_count, truncated)``.

    At most ``common.CSV_ROW_CAP`` data rows are written; ``truncated`` tells whether more existed.
    """
    where, params = _filters(store_id, status, prefix="o.")
    cap = common.CSV_ROW_CAP
    rows = conn.execute(_EXPORT_SELECT.format(where=where), [*params, cap + 1]).fetchall()
    data = [dict(row) for row in rows[:cap]]
    return csv_text(data, ORDER_CSV_COLUMNS), len(data), len(rows) > cap


# --------------------------------------------------------------------------- bridge (browser) routes
def _bridge_place_order(call: BridgeCall) -> tuple[int, Any]:
    key = call.headers.get("idempotency-key")
    if key is not None and len(key) > KEY_MAX_LENGTH:
        raise ServiceError(422, f"Idempotency-Key must be at most {KEY_MAX_LENGTH} characters", VALIDATION)
    order, created = place_order(call.conn, schemas.OrderIn.model_validate(call.body), key)
    call.out_headers["Idempotent-Replayed"] = "false" if created else "true"
    return (201 if created else 200), order


def _bridge_list_orders(call: BridgeCall) -> tuple[int, Any]:
    limit = call.qint("limit", LIST_LIMIT_DEFAULT, lo=1, hi=LIST_LIMIT_MAX)
    offset = call.qint("offset", 0, lo=0)
    items, total = list_orders(call.conn, call.qint("store_id"), call.qstr("status", choices=ORDER_STATUSES), limit, offset)
    return 200, page(items, total, limit, offset)


def _bridge_export_orders_csv(call: BridgeCall) -> tuple[int, Any]:
    text, count, truncated = export_orders_csv(call.conn, call.qint("store_id"), call.qstr("status", choices=ORDER_STATUSES))
    call.out_headers["Content-Type"] = "text/csv; charset=utf-8"
    call.out_headers["Content-Disposition"] = f'attachment; filename="{csv_filename(CSV_STEM)}"'
    call.out_headers["X-Row-Count"] = str(count)
    if truncated:
        call.out_headers["X-Truncated"] = "true"
    return 200, text


def _bridge_get_order(call: BridgeCall) -> tuple[int, Any]:
    return 200, get_order(call.conn, call.params["order_id"])


def _bridge_cancel_order(call: BridgeCall) -> tuple[int, Any]:
    return 200, cancel_order(call.conn, call.params["order_id"])


def _bridge_fulfil_order(call: BridgeCall) -> tuple[int, Any]:
    return 200, fulfil_order(call.conn, call.params["order_id"])


# Same order as the router: the literal ``/orders/export.csv`` precedes the ``/orders/{order_id}`` pattern.
BRIDGE_ROUTES: list[BridgeRoute] = [
    ("POST", r"^/orders$", _bridge_place_order),
    ("GET", r"^/orders$", _bridge_list_orders),
    ("GET", r"^/orders/export\.csv$", _bridge_export_orders_csv),
    ("GET", r"^/orders/(?P<order_id>\d+)$", _bridge_get_order),
    ("POST", r"^/orders/(?P<order_id>\d+)/cancel$", _bridge_cancel_order),
    ("POST", r"^/orders/(?P<order_id>\d+)/fulfil$", _bridge_fulfil_order),
]
