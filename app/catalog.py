"""Catalogue domain: stores and products.

Lifecycle of the two reference entities — create, read, partial update and (for products) an
idempotent soft delete — plus the per-product stock view across stores. Browser-safe (standard
library and pydantic only): shipped unchanged to the Pyodide demo, see ``common.BROWSER_MODULES``.

Conventions: every function takes an open connection first and raises ``ServiceError`` with a
machine-readable code; the new pydantic models live here because ``schemas.py`` is frozen;
``BRIDGE_ROUTES`` at the bottom mirrors ``app/routers/catalog.py`` with identical query bounds.
"""
from __future__ import annotations

import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from . import schemas
from .common import DUPLICATE, VALIDATION, BridgeCall, BridgeRoute, ServiceError, like_pattern, not_found, now_iso, page

# --------------------------------------------------------------------------- models


class StorePatch(BaseModel):
    """Partial update of a store. ``code`` is immutable, so it is not a field; unknown fields are rejected (422)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    region: str | None = Field(default=None, min_length=1, max_length=40)


class ProductPatch(BaseModel):
    """Partial update of a product. ``sku`` is immutable (sending it is a 422); ``active`` deactivates or reactivates."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    category: str | None = Field(default=None, min_length=1, max_length=40)
    price_cents: int | None = Field(default=None, ge=0)
    reorder_point: int | None = Field(default=None, ge=0)
    active: bool | None = None


class ProductOut(schemas.Product):
    """``Product`` plus ``updated_at`` (``None`` until the first PATCH or soft delete)."""

    updated_at: str | None = None


class ProductPage(BaseModel):
    """Typed page of products."""

    items: list[ProductOut]
    total: int
    limit: int
    offset: int


class ProductInventoryRow(schemas.InventoryRow):
    """One store's stock of a product, valued at the product's *current* price."""

    price_cents: int
    value_cents: int


# --------------------------------------------------------------------------- helpers
_PRODUCT_INVENTORY_SQL = """
SELECT i.store_id, s.code AS store_code, i.product_id, p.sku, p.name, i.on_hand, p.reorder_point, i.version,
       (i.on_hand <= p.reorder_point) AS below_reorder, p.price_cents, i.on_hand * p.price_cents AS value_cents
FROM inventory i JOIN stores s ON s.id = i.store_id JOIN products p ON p.id = i.product_id
WHERE i.product_id = ?
ORDER BY i.store_id
"""


def _product(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["active"] = bool(d["active"])
    return d


def _changes(patch: BaseModel) -> dict:
    """Fields explicitly sent with a non-null value; nothing to apply is a 422 ``validation_error``."""
    data = {key: value for key, value in patch.model_dump(exclude_unset=True).items() if value is not None}
    if not data:
        raise ServiceError(422, "no fields to update", VALIDATION)
    return data


def _update(conn: sqlite3.Connection, table: str, id_: int, data: dict) -> int:
    """``UPDATE table SET … WHERE id = ?`` for the given column → value map; returns the number of rows touched.

    The keys are declared fields of the patch models (which forbid extras), so they are safe to interpolate.
    """
    columns = sorted(data)
    assignments = ", ".join(f"{column} = ?" for column in columns)
    cur = conn.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", [*(data[column] for column in columns), id_])
    return cur.rowcount


def _require_product(conn: sqlite3.Connection, product_id: int) -> None:
    if not conn.execute("SELECT 1 FROM products WHERE id = ?", (product_id,)).fetchone():
        raise not_found("product", product_id)


# --------------------------------------------------------------------------- stores
def create_store(conn: sqlite3.Connection, s: schemas.StoreIn) -> dict:
    """Insert a store; a reused ``code`` is a 409 ``duplicate``."""
    try:
        cur = conn.execute("INSERT INTO stores (code, name, region) VALUES (?,?,?)", (s.code, s.name, s.region))
    except sqlite3.IntegrityError as exc:
        raise ServiceError(409, f"store code {s.code!r} already exists", DUPLICATE) from exc
    return get_store(conn, cur.lastrowid)


def list_stores(conn: sqlite3.Connection) -> list[dict]:
    """All stores in id order."""
    return [dict(r) for r in conn.execute("SELECT * FROM stores ORDER BY id")]


def get_store(conn: sqlite3.Connection, store_id: int) -> dict:
    """One store; 404 ``not_found`` when the id is unknown."""
    row = conn.execute("SELECT * FROM stores WHERE id = ?", (store_id,)).fetchone()
    if not row:
        raise not_found("store", store_id)
    return dict(row)


def update_store(conn: sqlite3.Connection, store_id: int, patch: StorePatch) -> dict:
    """Change a store's ``name`` and/or ``region`` (``code`` is immutable). Empty patch → 422, unknown id → 404."""
    data = _changes(patch)
    if _update(conn, "stores", store_id, data) == 0:
        raise not_found("store", store_id)
    return get_store(conn, store_id)


# --------------------------------------------------------------------------- products
def create_product(conn: sqlite3.Connection, p: schemas.ProductIn) -> dict:
    """Insert an active product; a reused ``sku`` is a 409 ``duplicate``."""
    try:
        cur = conn.execute(
            "INSERT INTO products (sku, name, category, price_cents, reorder_point) VALUES (?,?,?,?,?)",
            (p.sku, p.name, p.category, p.price_cents, p.reorder_point),
        )
    except sqlite3.IntegrityError as exc:
        raise ServiceError(409, f"sku {p.sku!r} already exists", DUPLICATE) from exc
    return get_product(conn, cur.lastrowid)


def get_product(conn: sqlite3.Connection, product_id: int) -> dict:
    """One product including ``updated_at``; inactive (soft-deleted) products are returned too. 404 when unknown."""
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not row:
        raise not_found("product", product_id)
    return _product(row)


def list_products(
    conn: sqlite3.Connection, q: str | None, category: str | None, limit: int, offset: int, *, include_inactive: bool = False
) -> tuple[list[dict], int]:
    """Page of products in id order → ``(items, total)``.

    ``q`` is a case-insensitive substring match on name or SKU with ``%``, ``_`` and ``\\`` taken literally
    (``like_pattern`` + ``ESCAPE``); ``category`` is an exact match; inactive products are hidden unless
    ``include_inactive`` is set.
    """
    where: list[str] = []
    params: list[object] = []
    if not include_inactive:
        where.append("active = 1")
    if q:
        where.append("(name LIKE ? ESCAPE '\\' OR sku LIKE ? ESCAPE '\\')")
        params += [like_pattern(q), like_pattern(q)]
    if category:
        where.append("category = ?")
        params.append(category)
    w = " AND ".join(where) or "1=1"
    total = conn.execute(f"SELECT COUNT(*) FROM products WHERE {w}", params).fetchone()[0]
    rows = conn.execute(f"SELECT * FROM products WHERE {w} ORDER BY id LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
    return [_product(r) for r in rows], total


def update_product(conn: sqlite3.Connection, product_id: int, patch: ProductPatch) -> dict:
    """Apply a partial update (``sku`` is immutable) and stamp ``updated_at``. Empty patch → 422, unknown id → 404.

    Order lines keep their ``unit_price_cents`` snapshot: a price change only affects orders placed afterwards.
    """
    data = _changes(patch)
    if "active" in data:
        data["active"] = int(data["active"])
    data["updated_at"] = now_iso()
    if _update(conn, "products", product_id, data) == 0:
        raise not_found("product", product_id)
    return get_product(conn, product_id)


def deactivate_product(conn: sqlite3.Connection, product_id: int) -> None:
    """Soft delete: ``active = 0`` plus ``updated_at``. Idempotent — an already inactive product is left untouched. 404 when unknown."""
    cur = conn.execute("UPDATE products SET active = 0, updated_at = ? WHERE id = ? AND active = 1", (now_iso(), product_id))
    if cur.rowcount == 0:
        _require_product(conn, product_id)


def product_inventory(conn: sqlite3.Connection, product_id: int) -> list[dict]:
    """Stock rows of one product across stores (store id order), each valued at the current price. 404 when unknown."""
    _require_product(conn, product_id)
    out = []
    for row in conn.execute(_PRODUCT_INVENTORY_SQL, (product_id,)):
        d = dict(row)
        d["below_reorder"] = bool(d["below_reorder"])
        out.append(d)
    return out


# --------------------------------------------------------------------------- bridge (browser demo) handlers
def _b_create_store(call: BridgeCall) -> tuple[int, dict]:
    return 201, create_store(call.conn, schemas.StoreIn.model_validate(call.body))


def _b_list_stores(call: BridgeCall) -> tuple[int, list[dict]]:
    return 200, list_stores(call.conn)


def _b_get_store(call: BridgeCall) -> tuple[int, dict]:
    return 200, get_store(call.conn, call.params["store_id"])


def _b_update_store(call: BridgeCall) -> tuple[int, dict]:
    return 200, update_store(call.conn, call.params["store_id"], StorePatch.model_validate(call.body))


def _b_create_product(call: BridgeCall) -> tuple[int, dict]:
    return 201, create_product(call.conn, schemas.ProductIn.model_validate(call.body))


def _b_list_products(call: BridgeCall) -> tuple[int, dict]:
    limit = call.qint("limit", 20, lo=1, hi=100)
    offset = call.qint("offset", 0, lo=0)
    items, total = list_products(
        call.conn,
        call.qstr("q", max_length=60),
        call.qstr("category"),
        limit,
        offset,
        include_inactive=call.qbool("include_inactive"),
    )
    return 200, page(items, total, limit, offset)


def _b_get_product(call: BridgeCall) -> tuple[int, dict]:
    return 200, get_product(call.conn, call.params["product_id"])


def _b_update_product(call: BridgeCall) -> tuple[int, dict]:
    return 200, update_product(call.conn, call.params["product_id"], ProductPatch.model_validate(call.body))


def _b_deactivate_product(call: BridgeCall) -> tuple[int, None]:
    deactivate_product(call.conn, call.params["product_id"])
    return 204, None


def _b_product_inventory(call: BridgeCall) -> tuple[int, list[dict]]:
    return 200, product_inventory(call.conn, call.params["product_id"])


BRIDGE_ROUTES: list[BridgeRoute] = [
    ("POST", r"^/stores$", _b_create_store),
    ("GET", r"^/stores$", _b_list_stores),
    ("GET", r"^/stores/(?P<store_id>\d+)$", _b_get_store),
    ("PATCH", r"^/stores/(?P<store_id>\d+)$", _b_update_store),
    ("POST", r"^/products$", _b_create_product),
    ("GET", r"^/products$", _b_list_products),
    ("GET", r"^/products/(?P<product_id>\d+)$", _b_get_product),
    ("PATCH", r"^/products/(?P<product_id>\d+)$", _b_update_product),
    ("DELETE", r"^/products/(?P<product_id>\d+)$", _b_deactivate_product),
    ("GET", r"^/products/(?P<product_id>\d+)/inventory$", _b_product_inventory),
]
