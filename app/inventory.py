"""Inventory operations: stock adjustments, deliveries (receipts), the movement ledger feeds,
store-to-store transfers as idempotent entities, and CSV exports.

Browser-safe: standard library + pydantic only, no framework imports. Every function takes an
open connection first and raises ``ServiceError`` with the canonical codes from ``common``.
``BRIDGE_ROUTES`` at the bottom mirrors ``app/routers/inventory.py`` with identical query bounds.
"""
from __future__ import annotations

import sqlite3

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import common, schemas
from .common import (
    IDEMPOTENCY_CONFLICT,
    IDEMPOTENCY_KEY_REUSE,
    NOT_FOUND,
    VALIDATION,
    VERSION_CONFLICT,
    BridgeCall,
    BridgeRoute,
    ServiceError,
    csv_filename,
    csv_text,
    like_pattern,
    not_found,
    page,
    request_hash,
)
from .db import transaction
from .ledger import apply_movement

MOVEMENT_REASONS: tuple[str, ...] = ("receipt", "sale", "return", "adjustment", "transfer_in", "transfer_out")
REASON_PATTERN = "^(" + "|".join(MOVEMENT_REASONS) + ")$"
MAX_IDEMPOTENCY_KEY = 64  # Idempotency-Key header length cap (router and bridge)
SEARCH_MAX_LENGTH = 60  # ``q`` query parameter
SINCE_MAX_LENGTH = 32  # ``since`` ISO-8601 lower bound (string comparison)

INVENTORY_CSV_COLUMNS = ["store_id", "store_code", "product_id", "sku", "name", "on_hand", "reorder_point", "version", "below_reorder", "price_cents", "value_cents"]
MOVEMENT_CSV_COLUMNS = ["id", "store_id", "product_id", "delta", "reason", "reference", "balance_after", "created_at"]

_ROW_COLUMNS = """i.store_id, s.code AS store_code, i.product_id, p.sku, p.name, i.on_hand, p.reorder_point, i.version,
       (i.on_hand <= p.reorder_point) AS below_reorder, p.price_cents, (i.on_hand * p.price_cents) AS value_cents"""
_ROW_FROM = "FROM inventory i JOIN stores s ON s.id = i.store_id JOIN products p ON p.id = i.product_id"
_MOVEMENT_COLUMNS = ", ".join(MOVEMENT_CSV_COLUMNS)
_TRANSFER_COLUMNS = "t.id, t.from_store_id, t.to_store_id, t.product_id, t.quantity, t.idempotency_key, t.created_at"


# --------------------------------------------------------------------------- models
class InventoryRowOut(schemas.InventoryRow):
    """Inventory row plus valuation: unit price and ``on_hand * price_cents``."""

    price_cents: int
    value_cents: int


class InventoryPage(BaseModel):
    """One page of inventory rows."""

    items: list[InventoryRowOut]
    total: int
    limit: int
    offset: int


class Movement(schemas.Movement):
    """Ledger row with the running balance written since schema v2 (``None`` on legacy rows)."""

    balance_after: int | None = None


class MovementFeed(BaseModel):
    """Keyset-paged slice of the movement ledger, newest first; ``next_before_id`` is the cursor for the next page."""

    items: list[Movement]
    limit: int
    next_before_id: int | None = None


class ReceiptLineIn(BaseModel):
    """One delivered line."""

    product_id: int
    quantity: int = Field(gt=0)


class ReceiptIn(BaseModel):
    """A delivery: a supplier/PO reference and 1–50 lines with distinct products."""

    reference: str = Field(min_length=1, max_length=64)
    lines: list[ReceiptLineIn] = Field(min_length=1, max_length=50)

    @field_validator("lines")
    @classmethod
    def unique_products(cls, v: list[ReceiptLineIn]) -> list[ReceiptLineIn]:
        """Reject the same product twice in one delivery."""
        ids = [line.product_id for line in v]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate product_id in lines")
        return v


class ReceiptOut(BaseModel):
    """Result of a delivery: the updated row for every line."""

    reference: str
    store_id: int
    lines: list[InventoryRowOut]


class TransferOut(BaseModel):
    """A store-to-store transfer with the current inventory rows on both sides (``from`` / ``to``)."""

    model_config = ConfigDict(populate_by_name=True)

    id: int
    from_store_id: int
    to_store_id: int
    product_id: int
    quantity: int
    idempotency_key: str | None = None
    created_at: str
    from_: InventoryRowOut = Field(alias="from")
    to: InventoryRowOut


class TransferPage(BaseModel):
    """One page of transfers, newest first."""

    items: list[TransferOut]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- helpers
def _ensure_exists(conn: sqlite3.Connection, table: str, id_: int) -> None:
    if not conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (id_,)).fetchone():
        raise not_found(table[:-1], id_)


def _row_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["below_reorder"] = bool(d["below_reorder"])
    return d


def _inventory_where(store_id: int | None, low_stock_only: bool, q: str | None) -> tuple[str, list]:
    where: list[str] = ["1=1"]
    params: list = []
    if store_id is not None:
        where.append("i.store_id = ?")
        params.append(store_id)
    if low_stock_only:
        where.append("i.on_hand <= p.reorder_point")
    if q:
        pattern = like_pattern(q)
        where.append("(p.sku LIKE ? ESCAPE '\\' OR p.name LIKE ? ESCAPE '\\')")
        params += [pattern, pattern]
    return " AND ".join(where), params


def _movement_where(
    store_id: int | None, product_id: int | None, reason: str | None, since: str | None, before_id: int | None
) -> tuple[str, list]:
    if reason is not None and reason not in MOVEMENT_REASONS:
        raise ServiceError(422, f"reason must be one of {', '.join(MOVEMENT_REASONS)}", VALIDATION)
    where: list[str] = ["1=1"]
    params: list = []
    if store_id is not None:
        where.append("store_id = ?")
        params.append(store_id)
    if product_id is not None:
        where.append("product_id = ?")
        params.append(product_id)
    if reason is not None:
        where.append("reason = ?")
        params.append(reason)
    if since:
        where.append("created_at >= ?")
        params.append(since)
    if before_id is not None:
        where.append("id < ?")
        params.append(before_id)
    return " AND ".join(where), params


def _rows_for_pairs(conn: sqlite3.Connection, pairs: list[tuple[int, int]]) -> dict[tuple[int, int], dict]:
    """Current inventory rows for several (store, product) pairs in one query.

    A pair without an inventory record (possible only after manual surgery on the table) yields a
    zero-balance row instead of failing, so transfer listings never break on a missing cache row.
    """
    unique = list(dict.fromkeys(pairs))
    if not unique:
        return {}
    values = ",".join("(?,?)" for _ in unique)
    params = [value for pair in unique for value in pair]
    rows = conn.execute(
        f"""WITH pr(store_id, product_id) AS (VALUES {values})
            SELECT pr.store_id, s.code AS store_code, pr.product_id, p.sku, p.name,
                   COALESCE(i.on_hand, 0) AS on_hand, p.reorder_point, COALESCE(i.version, 0) AS version,
                   (COALESCE(i.on_hand, 0) <= p.reorder_point) AS below_reorder, p.price_cents,
                   (COALESCE(i.on_hand, 0) * p.price_cents) AS value_cents
            FROM pr JOIN stores s ON s.id = pr.store_id JOIN products p ON p.id = pr.product_id
            LEFT JOIN inventory i ON i.store_id = pr.store_id AND i.product_id = pr.product_id""",
        params,
    ).fetchall()
    return {(r["store_id"], r["product_id"]): _row_dict(r) for r in rows}


def _transfer_dict(row: sqlite3.Row, lookup: dict[tuple[int, int], dict]) -> dict:
    d = dict(row)  # explicit column list: request_hash is never selected, so it is never returned
    d["from"] = lookup[(d["from_store_id"], d["product_id"])]
    d["to"] = lookup[(d["to_store_id"], d["product_id"])]
    return d


def _transfer_by_key(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT id, request_hash FROM transfers WHERE idempotency_key = ?", (key,)).fetchone()


def _replay_id(existing: sqlite3.Row, fingerprint: str) -> int:
    """Id of the transfer to replay, or 422 when the stored fingerprint belongs to a different body (NULL = legacy row, accepted)."""
    stored = existing["request_hash"]
    if stored is not None and stored != fingerprint:
        raise ServiceError(422, "Idempotency-Key reused with a different request body", IDEMPOTENCY_KEY_REUSE)
    return int(existing["id"])


# --------------------------------------------------------------------------- rows & adjustments
def adjust_stock(conn: sqlite3.Connection, store_id: int, product_id: int, adj: schemas.StockAdjust) -> dict:
    """Receive / return / adjust stock in one transaction, with optimistic locking via ``expected_version``.

    Raises 404 for an unknown store or product, 409 ``version_conflict`` when the row moved on,
    409 ``insufficient_stock`` when the balance would go negative (nothing is written).
    """
    with transaction(conn):
        _ensure_exists(conn, "stores", store_id)
        _ensure_exists(conn, "products", product_id)
        if adj.expected_version is not None:
            row = conn.execute("SELECT version FROM inventory WHERE store_id=? AND product_id=?", (store_id, product_id)).fetchone()
            current_version = row["version"] if row else 0
            if current_version != adj.expected_version:
                raise ServiceError(409, f"version conflict: expected {adj.expected_version}, current {current_version}", VERSION_CONFLICT)
        apply_movement(conn, store_id, product_id, adj.delta, adj.reason, adj.reference)
        return get_inventory_row(conn, store_id, product_id)


def get_inventory_row(conn: sqlite3.Connection, store_id: int, product_id: int) -> dict:
    """One inventory row with valuation; 404 ``not_found`` when the pair has no record yet."""
    row = conn.execute(f"SELECT {_ROW_COLUMNS} {_ROW_FROM} WHERE i.store_id=? AND i.product_id=?", (store_id, product_id)).fetchone()
    if not row:
        raise ServiceError(404, "no inventory record", NOT_FOUND)
    return _row_dict(row)


def list_inventory(
    conn: sqlite3.Connection, store_id: int | None, low_stock_only: bool, limit: int, offset: int, *, q: str | None = None
) -> tuple[list[dict], int]:
    """Inventory rows ordered by (store, product) with optional store, low-stock and SKU/name search filters."""
    where, params = _inventory_where(store_id, low_stock_only, q)
    total = conn.execute(f"SELECT COUNT(*) {_ROW_FROM} WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT {_ROW_COLUMNS} {_ROW_FROM} WHERE {where} ORDER BY i.store_id, i.product_id LIMIT ? OFFSET ?", params + [limit, offset]
    ).fetchall()
    return [_row_dict(r) for r in rows], int(total)


# --------------------------------------------------------------------------- receipts
def receive_stock(conn: sqlite3.Connection, store_id: int, receipt: ReceiptIn) -> dict:
    """Book a delivery: every line becomes a ``receipt`` movement referencing ``receipt:<reference>``.

    All-or-nothing — an unknown product (404) rolls back the lines already applied.
    """
    reference = f"receipt:{receipt.reference}"
    with transaction(conn):
        _ensure_exists(conn, "stores", store_id)
        for line in receipt.lines:
            _ensure_exists(conn, "products", line.product_id)
            apply_movement(conn, store_id, line.product_id, line.quantity, "receipt", reference)
        lines = [get_inventory_row(conn, store_id, line.product_id) for line in receipt.lines]
    return {"reference": receipt.reference, "store_id": store_id, "lines": lines}


# --------------------------------------------------------------------------- movements
def movements(conn: sqlite3.Connection, store_id: int, product_id: int, limit: int, *, before_id: int | None = None) -> list[dict]:
    """Ledger history of one (store, product) pair, newest first; ``before_id`` pages backwards (``id < before_id``)."""
    where, params = _movement_where(store_id, product_id, None, None, before_id)
    rows = conn.execute(f"SELECT {_MOVEMENT_COLUMNS} FROM stock_movements WHERE {where} ORDER BY id DESC LIMIT ?", params + [limit])
    return [dict(r) for r in rows]


def movement_feed(
    conn: sqlite3.Connection,
    *,
    store_id: int | None = None,
    product_id: int | None = None,
    reason: str | None = None,
    since: str | None = None,
    before_id: int | None = None,
    limit: int = 100,
) -> dict:
    """Cross-store movement feed, newest first, with keyset paging.

    ``since`` is compared as an ISO-8601 string against ``created_at``. ``next_before_id`` is the last id
    of a full page (pass it back as ``before_id``) and ``None`` once a page comes back short.
    """
    where, params = _movement_where(store_id, product_id, reason, since, before_id)
    rows = conn.execute(f"SELECT {_MOVEMENT_COLUMNS} FROM stock_movements WHERE {where} ORDER BY id DESC LIMIT ?", params + [limit])
    items = [dict(r) for r in rows]
    next_before_id = items[-1]["id"] if len(items) == limit else None
    return {"items": items, "limit": limit, "next_before_id": next_before_id}


# --------------------------------------------------------------------------- transfers
def transfer(conn: sqlite3.Connection, t: schemas.TransferIn, idempotency_key: str | None = None) -> tuple[dict, bool]:
    """Move stock between two stores atomically and record the transfer as an entity.

    Returns ``(transfer, created)``. With an ``Idempotency-Key`` a repeat of the same request replays the
    stored transfer (``created=False``) without writing; the same key with a different body is a 422
    ``idempotency_key_reuse``. The transfer row is inserted first so both movements reference
    ``transfer:<id>``; any failure (unknown store/product, insufficient stock) rolls everything back,
    leaving no transfer row and both balances untouched.
    """
    if t.from_store_id == t.to_store_id:
        raise ServiceError(422, "from_store_id and to_store_id must differ", VALIDATION)
    key = idempotency_key or None
    fingerprint = request_hash({"from": t.from_store_id, "to": t.to_store_id, "product_id": t.product_id, "quantity": t.quantity})
    if key:
        existing = _transfer_by_key(conn, key)  # fast path: no write lock needed for a replay
        if existing is not None and existing["request_hash"] in (None, fingerprint):
            return get_transfer(conn, int(existing["id"])), False
    with transaction(conn):
        existing = _transfer_by_key(conn, key) if key else None
        if existing is not None:
            transfer_id, created = _replay_id(existing, fingerprint), False
        else:
            _ensure_exists(conn, "stores", t.from_store_id)
            _ensure_exists(conn, "stores", t.to_store_id)
            _ensure_exists(conn, "products", t.product_id)
            try:
                cur = conn.execute(
                    "INSERT INTO transfers (from_store_id, to_store_id, product_id, quantity, idempotency_key, request_hash) VALUES (?,?,?,?,?,?)",
                    (t.from_store_id, t.to_store_id, t.product_id, t.quantity, key, fingerprint if key else None),
                )
            except sqlite3.IntegrityError as exc:
                # Lost a race with an identical request between the check and the insert; surface the conflict.
                raise ServiceError(409, "duplicate idempotency key (concurrent request)", IDEMPOTENCY_CONFLICT) from exc
            transfer_id, created = int(cur.lastrowid), True
            reference = f"transfer:{transfer_id}"
            apply_movement(conn, t.from_store_id, t.product_id, -t.quantity, "transfer_out", reference)
            apply_movement(conn, t.to_store_id, t.product_id, t.quantity, "transfer_in", reference)
    return get_transfer(conn, transfer_id), created


def get_transfer(conn: sqlite3.Connection, transfer_id: int) -> dict:
    """One transfer with the current inventory rows of both sides; 404 ``not_found`` when unknown."""
    row = conn.execute(f"SELECT {_TRANSFER_COLUMNS} FROM transfers t WHERE t.id = ?", (transfer_id,)).fetchone()
    if not row:
        raise not_found("transfer", transfer_id)
    lookup = _rows_for_pairs(conn, [(row["from_store_id"], row["product_id"]), (row["to_store_id"], row["product_id"])])
    return _transfer_dict(row, lookup)


def list_transfers(conn: sqlite3.Connection, store_id: int | None, product_id: int | None, limit: int, offset: int) -> tuple[list[dict], int]:
    """Transfers newest first; ``store_id`` matches either side. Inventory rows are fetched in one extra query."""
    where: list[str] = ["1=1"]
    params: list = []
    if store_id is not None:
        where.append("(t.from_store_id = ? OR t.to_store_id = ?)")
        params += [store_id, store_id]
    if product_id is not None:
        where.append("t.product_id = ?")
        params.append(product_id)
    w = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM transfers t WHERE {w}", params).fetchone()[0]
    rows = conn.execute(f"SELECT {_TRANSFER_COLUMNS} FROM transfers t WHERE {w} ORDER BY t.id DESC LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
    pairs = [(r[side], r["product_id"]) for r in rows for side in ("from_store_id", "to_store_id")]
    lookup = _rows_for_pairs(conn, pairs)
    return [_transfer_dict(r, lookup) for r in rows], int(total)


# --------------------------------------------------------------------------- CSV exports
def export_inventory_csv(conn: sqlite3.Connection, store_id: int | None, low_stock_only: bool, *, q: str | None = None) -> tuple[str, int, bool]:
    """Inventory rows as CSV (same filters as ``list_inventory``), ordered by store then product.

    Returns ``(csv_text, row_count, truncated)``; at most ``common.CSV_ROW_CAP`` data rows are written.
    """
    where, params = _inventory_where(store_id, low_stock_only, q)
    cap = common.CSV_ROW_CAP
    rows = conn.execute(
        f"SELECT {_ROW_COLUMNS} {_ROW_FROM} WHERE {where} ORDER BY i.store_id, i.product_id LIMIT ?", params + [cap + 1]
    ).fetchall()
    items = [_row_dict(r) for r in rows[:cap]]
    return csv_text(items, INVENTORY_CSV_COLUMNS), len(items), len(rows) > cap


def export_movements_csv(
    conn: sqlite3.Connection, *, store_id: int | None = None, product_id: int | None = None, reason: str | None = None, since: str | None = None
) -> tuple[str, int, bool]:
    """Movement feed as CSV, newest first. Returns ``(csv_text, row_count, truncated)`` capped at ``common.CSV_ROW_CAP`` rows."""
    where, params = _movement_where(store_id, product_id, reason, since, None)
    cap = common.CSV_ROW_CAP
    rows = conn.execute(f"SELECT {_MOVEMENT_COLUMNS} FROM stock_movements WHERE {where} ORDER BY id DESC LIMIT ?", params + [cap + 1]).fetchall()
    items = [dict(r) for r in rows[:cap]]
    return csv_text(items, MOVEMENT_CSV_COLUMNS), len(items), len(rows) > cap


# --------------------------------------------------------------------------- bridge (browser demo) handlers
def _csv_reply(call: BridgeCall, stem: str, result: tuple[str, int, bool]) -> tuple[int, str]:
    text, count, truncated = result
    call.out_headers["Content-Type"] = "text/csv; charset=utf-8"
    call.out_headers["Content-Disposition"] = f'attachment; filename="{csv_filename(stem)}"'
    call.out_headers["X-Row-Count"] = str(count)
    if truncated:
        call.out_headers["X-Truncated"] = "true"
    return 200, text


def _bridge_list_inventory(call: BridgeCall) -> tuple[int, dict]:
    limit = call.qint("limit", 50, lo=1, hi=200)
    offset = call.qint("offset", 0, lo=0)
    items, total = list_inventory(
        call.conn, call.qint("store_id"), call.qbool("low_stock"), limit, offset, q=call.qstr("q", max_length=SEARCH_MAX_LENGTH)
    )
    return 200, page(items, total, limit, offset)


def _bridge_export_inventory(call: BridgeCall) -> tuple[int, str]:
    result = export_inventory_csv(call.conn, call.qint("store_id"), call.qbool("low_stock"), q=call.qstr("q", max_length=SEARCH_MAX_LENGTH))
    return _csv_reply(call, "inventory", result)


def _bridge_receive_stock(call: BridgeCall) -> tuple[int, dict]:
    receipt = ReceiptIn.model_validate(call.body)
    return 201, receive_stock(call.conn, call.params["store_id"], receipt)


def _bridge_get_row(call: BridgeCall) -> tuple[int, dict]:
    return 200, get_inventory_row(call.conn, call.params["store_id"], call.params["product_id"])


def _bridge_adjust(call: BridgeCall) -> tuple[int, dict]:
    adj = schemas.StockAdjust.model_validate(call.body)
    return 200, adjust_stock(call.conn, call.params["store_id"], call.params["product_id"], adj)


def _bridge_pair_movements(call: BridgeCall) -> tuple[int, list]:
    limit = call.qint("limit", 50, lo=1, hi=500)
    return 200, movements(call.conn, call.params["store_id"], call.params["product_id"], limit, before_id=call.qint("before_id", lo=1))


def _bridge_movement_feed(call: BridgeCall) -> tuple[int, dict]:
    feed = movement_feed(
        call.conn,
        store_id=call.qint("store_id"),
        product_id=call.qint("product_id"),
        reason=call.qstr("reason", choices=MOVEMENT_REASONS),
        since=call.qstr("since", max_length=SINCE_MAX_LENGTH),
        before_id=call.qint("before_id", lo=1),
        limit=call.qint("limit", 100, lo=1, hi=500),
    )
    return 200, feed


def _bridge_export_movements(call: BridgeCall) -> tuple[int, str]:
    result = export_movements_csv(
        call.conn,
        store_id=call.qint("store_id"),
        product_id=call.qint("product_id"),
        reason=call.qstr("reason", choices=MOVEMENT_REASONS),
        since=call.qstr("since", max_length=SINCE_MAX_LENGTH),
    )
    return _csv_reply(call, "movements", result)


def _bridge_create_transfer(call: BridgeCall) -> tuple[int, dict]:
    body = schemas.TransferIn.model_validate(call.body)
    key = call.headers.get("idempotency-key") or None
    if key is not None and len(key) > MAX_IDEMPOTENCY_KEY:
        raise ServiceError(422, f"Idempotency-Key must be at most {MAX_IDEMPOTENCY_KEY} characters", VALIDATION)
    out, created = transfer(call.conn, body, key)
    call.out_headers["Idempotent-Replayed"] = "false" if created else "true"
    return (201 if created else 200), out


def _bridge_list_transfers(call: BridgeCall) -> tuple[int, dict]:
    limit = call.qint("limit", 20, lo=1, hi=100)
    offset = call.qint("offset", 0, lo=0)
    items, total = list_transfers(call.conn, call.qint("store_id"), call.qint("product_id"), limit, offset)
    return 200, page(items, total, limit, offset)


def _bridge_get_transfer(call: BridgeCall) -> tuple[int, dict]:
    return 200, get_transfer(call.conn, call.params["transfer_id"])


# Same paths, methods and bounds as app/routers/inventory.py (literal paths before parameterised ones).
BRIDGE_ROUTES: list[BridgeRoute] = [
    ("GET", r"^/inventory$", _bridge_list_inventory),
    ("GET", r"^/inventory/export\.csv$", _bridge_export_inventory),
    ("POST", r"^/inventory/(?P<store_id>\d+)/receipts$", _bridge_receive_stock),
    ("GET", r"^/inventory/(?P<store_id>\d+)/(?P<product_id>\d+)$", _bridge_get_row),
    ("POST", r"^/inventory/(?P<store_id>\d+)/(?P<product_id>\d+)/adjust$", _bridge_adjust),
    ("GET", r"^/inventory/(?P<store_id>\d+)/(?P<product_id>\d+)/movements$", _bridge_pair_movements),
    ("GET", r"^/movements$", _bridge_movement_feed),
    ("GET", r"^/movements/export\.csv$", _bridge_export_movements),
    ("POST", r"^/transfers$", _bridge_create_transfer),
    ("GET", r"^/transfers$", _bridge_list_transfers),
    ("GET", r"^/transfers/(?P<transfer_id>\d+)$", _bridge_get_transfer),
]
