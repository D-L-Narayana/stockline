"""Inventory HTTP surface: rows with valuation, adjustments, deliveries, movement feeds, transfers and CSV exports.

Thin layer over ``app.inventory``: query bounds declared here are mirrored by ``inventory.BRIDGE_ROUTES``.
Literal paths (``/inventory/export.csv``, ``/inventory/{store_id}/receipts``) are declared before the
parameterised ``/inventory/{store_id}/{product_id}`` family.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Header, Query, Response

from .. import inventory, schemas
from ..common import ErrorBody, csv_filename, page
from ..deps import get_conn
from ..inventory import InventoryPage, InventoryRowOut, Movement, MovementFeed, ReceiptIn, ReceiptOut, TransferOut, TransferPage

router = APIRouter(tags=["inventory"])

_NOT_FOUND = {404: {"model": ErrorBody, "description": "Unknown store, product or record"}}
_VALIDATION = {422: {"model": ErrorBody, "description": "Validation error"}}
_CSV = {200: {"content": {"text/csv": {"schema": {"type": "string"}}}, "description": "CSV export"}, **_VALIDATION}
_REPLAYED = {
    "Idempotent-Replayed": {
        "schema": {"type": "string", "enum": ["true", "false"]},
        "description": "`true` when an earlier request with the same Idempotency-Key was replayed",
    }
}


def _csv_response(stem: str, text: str, count: int, truncated: bool) -> Response:
    headers = {"Content-Disposition": f'attachment; filename="{csv_filename(stem)}"', "X-Row-Count": str(count)}
    if truncated:
        headers["X-Truncated"] = "true"
    return Response(content=text, media_type="text/csv; charset=utf-8", headers=headers)


# --------------------------------------------------------------------------- inventory rows
@router.get("/inventory", response_model=InventoryPage, summary="List inventory rows with valuation (store, low-stock and search filters)", responses=_VALIDATION)
def list_inventory(
    store_id: int | None = None,
    low_stock: bool = False,
    q: str | None = Query(None, max_length=inventory.SEARCH_MAX_LENGTH, description="Case-insensitive match on SKU or product name"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Rows are ordered by store then product; `value_cents = on_hand * price_cents`."""
    items, total = inventory.list_inventory(conn, store_id, low_stock, limit, offset, q=q)
    return page(items, total, limit, offset)


@router.get("/inventory/export.csv", response_class=Response, summary="Export inventory rows as CSV (same filters as the list)", responses=_CSV)
def export_inventory(
    store_id: int | None = None,
    low_stock: bool = False,
    q: str | None = Query(None, max_length=inventory.SEARCH_MAX_LENGTH),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """`X-Row-Count` carries the number of data rows; `X-Truncated: true` when the export cap was hit."""
    return _csv_response("inventory", *inventory.export_inventory_csv(conn, store_id, low_stock, q=q))


@router.post(
    "/inventory/{store_id}/receipts",
    response_model=ReceiptOut,
    status_code=201,
    summary="Receive a delivery (several lines, all-or-nothing)",
    responses={**_NOT_FOUND, **_VALIDATION},
)
def receive_stock(store_id: int, body: ReceiptIn, conn: sqlite3.Connection = Depends(get_conn)):
    """Every line becomes a `receipt` movement referencing `receipt:<reference>`; an unknown product rolls the whole delivery back."""
    return inventory.receive_stock(conn, store_id, body)


@router.get("/inventory/{store_id}/{product_id}", response_model=InventoryRowOut, summary="One inventory row with valuation", responses=_NOT_FOUND)
def get_inventory(store_id: int, product_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    return inventory.get_inventory_row(conn, store_id, product_id)


@router.post(
    "/inventory/{store_id}/{product_id}/adjust",
    response_model=InventoryRowOut,
    summary="Receive / return / adjust stock (optimistic locking via expected_version)",
    responses={**_NOT_FOUND, 409: {"model": ErrorBody, "description": "`version_conflict` or `insufficient_stock`"}, **_VALIDATION},
)
def adjust(store_id: int, product_id: int, body: schemas.StockAdjust, conn: sqlite3.Connection = Depends(get_conn)):
    return inventory.adjust_stock(conn, store_id, product_id, body)


@router.get(
    "/inventory/{store_id}/{product_id}/movements",
    response_model=list[Movement],
    summary="Ledger history of one store/product pair (newest first, keyset paging with before_id)",
    responses=_VALIDATION,
)
def list_movements(
    store_id: int,
    product_id: int,
    limit: int = Query(50, ge=1, le=500),
    before_id: int | None = Query(None, ge=1, description="Return movements with id below this value"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return inventory.movements(conn, store_id, product_id, limit, before_id=before_id)


# --------------------------------------------------------------------------- movement feed
@router.get("/movements", response_model=MovementFeed, summary="Cross-store movement feed (filters, keyset paging)", responses=_VALIDATION)
def movement_feed(
    store_id: int | None = None,
    product_id: int | None = None,
    reason: str | None = Query(None, pattern=inventory.REASON_PATTERN),
    since: str | None = Query(None, max_length=inventory.SINCE_MAX_LENGTH, description="ISO-8601 lower bound on created_at (string comparison)"),
    before_id: int | None = Query(None, ge=1, description="Keyset cursor: movements with id below this value"),
    limit: int = Query(100, ge=1, le=500),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """`next_before_id` is the cursor for the following page, `null` once a page comes back short."""
    return inventory.movement_feed(conn, store_id=store_id, product_id=product_id, reason=reason, since=since, before_id=before_id, limit=limit)


@router.get("/movements/export.csv", response_class=Response, summary="Export the movement feed as CSV (newest first)", responses=_CSV)
def export_movements(
    store_id: int | None = None,
    product_id: int | None = None,
    reason: str | None = Query(None, pattern=inventory.REASON_PATTERN),
    since: str | None = Query(None, max_length=inventory.SINCE_MAX_LENGTH),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return _csv_response("movements", *inventory.export_movements_csv(conn, store_id=store_id, product_id=product_id, reason=reason, since=since))


# --------------------------------------------------------------------------- transfers
@router.post(
    "/transfers",
    response_model=TransferOut,
    status_code=201,
    summary="Atomic store-to-store transfer (idempotent with the Idempotency-Key header)",
    responses={
        200: {"model": TransferOut, "description": "Replayed: a transfer with this Idempotency-Key already exists", "headers": _REPLAYED},
        201: {"description": "Transfer created", "headers": _REPLAYED},
        **_NOT_FOUND,
        409: {"model": ErrorBody, "description": "`insufficient_stock` (nothing moved) or `idempotency_conflict`"},
        **_VALIDATION,
    },
)
def create_transfer(
    body: schemas.TransferIn,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=inventory.MAX_IDEMPOTENCY_KEY),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Both movements reference `transfer:<id>`; the same key with a different body is a 422 `idempotency_key_reuse`."""
    out, created = inventory.transfer(conn, body, idempotency_key)
    response.status_code = 201 if created else 200
    response.headers["Idempotent-Replayed"] = "false" if created else "true"
    return out


@router.get("/transfers", response_model=TransferPage, summary="List transfers (newest first; store_id matches either side)", responses=_VALIDATION)
def list_transfers(
    store_id: int | None = None,
    product_id: int | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
):
    items, total = inventory.list_transfers(conn, store_id, product_id, limit, offset)
    return page(items, total, limit, offset)


@router.get("/transfers/{transfer_id}", response_model=TransferOut, summary="One transfer with the current rows on both sides", responses=_NOT_FOUND)
def get_transfer(transfer_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    return inventory.get_transfer(conn, transfer_id)
