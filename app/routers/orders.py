"""HTTP surface for orders: ``POST/GET /orders``, the CSV export, ``GET /orders/{id}``, cancel and fulfil.

Thin wrappers over ``app.orders``. Every operation documents its error bodies with ``ErrorBody``.
``/orders/export.csv`` is declared **before** ``/orders/{order_id}`` so the literal path wins the match.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Header, Query, Response

from .. import orders
from ..common import ErrorBody, csv_filename, page
from ..deps import get_conn
from ..schemas import OrderIn

router = APIRouter(tags=["orders"])

_STATUS_PATTERN = "^(" + "|".join(orders.ORDER_STATUSES) + ")$"
_REPLAY_HEADER = {
    "Idempotent-Replayed": {
        "description": "`true` when an earlier request with the same Idempotency-Key was replayed, else `false`.",
        "schema": {"type": "string", "enum": ["true", "false"]},
    }
}
_NOT_FOUND = {"model": ErrorBody, "description": "Unknown store, product or order (`not_found`)"}
_VALIDATION = {"model": ErrorBody, "description": "Invalid body, header or query parameter (`validation_error`)"}


@router.post(
    "/orders",
    response_model=orders.OrderOut,
    status_code=201,
    summary="Place an order (reserves stock atomically; idempotent with the Idempotency-Key header)",
    response_description="Order created; stock reserved for every line",
    responses={
        200: {"model": orders.OrderOut, "description": "Replay of an earlier request with the same Idempotency-Key", "headers": _REPLAY_HEADER},
        201: {"headers": _REPLAY_HEADER},
        404: _NOT_FOUND,
        409: {"model": ErrorBody, "description": "`insufficient_stock`, `product_inactive` or `idempotency_conflict`"},
        422: {"model": ErrorBody, "description": "`validation_error`, or `idempotency_key_reuse` (same key, different body)"},
    },
)
def place_order(
    body: OrderIn,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=orders.KEY_MAX_LENGTH),
    conn: sqlite3.Connection = Depends(get_conn),
):
    order, created = orders.place_order(conn, body, idempotency_key)
    response.status_code = 201 if created else 200
    response.headers["Idempotent-Replayed"] = "false" if created else "true"
    return order


@router.get(
    "/orders",
    response_model=orders.OrderPage,
    summary="List orders (newest first) with their lines",
    responses={422: _VALIDATION},
)
def list_orders(
    store_id: int | None = None,
    status: str | None = Query(None, pattern=_STATUS_PATTERN),
    limit: int = Query(orders.LIST_LIMIT_DEFAULT, ge=1, le=orders.LIST_LIMIT_MAX),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
):
    items, total = orders.list_orders(conn, store_id, status, limit, offset)
    return page(items, total, limit, offset)


@router.get(
    "/orders/export.csv",
    response_class=Response,
    summary="Export orders as CSV (one row per order line, newest order first)",
    responses={
        200: {"content": {"text/csv": {"schema": {"type": "string"}}}, "description": "CSV export (`X-Row-Count`, `X-Truncated` headers)"},
        422: _VALIDATION,
    },
)
def export_orders_csv(
    store_id: int | None = None,
    status: str | None = Query(None, pattern=_STATUS_PATTERN),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    text, count, truncated = orders.export_orders_csv(conn, store_id, status)
    headers = {"Content-Disposition": f'attachment; filename="{csv_filename(orders.CSV_STEM)}"', "X-Row-Count": str(count)}
    if truncated:
        headers["X-Truncated"] = "true"
    return Response(content=text, media_type="text/csv; charset=utf-8", headers=headers)


@router.get(
    "/orders/{order_id}",
    response_model=orders.OrderOut,
    summary="Get one order with its lines",
    responses={404: _NOT_FOUND, 422: _VALIDATION},
)
def get_order(order_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    return orders.get_order(conn, order_id)


@router.post(
    "/orders/{order_id}/cancel",
    response_model=orders.OrderOut,
    summary="Cancel an order and restock it (idempotent)",
    responses={404: _NOT_FOUND, 409: {"model": ErrorBody, "description": "Fulfilled orders cannot be cancelled (`invalid_state`)"}, 422: _VALIDATION},
)
def cancel_order(order_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    return orders.cancel_order(conn, order_id)


@router.post(
    "/orders/{order_id}/fulfil",
    response_model=orders.OrderOut,
    summary="Mark an order fulfilled (idempotent)",
    responses={404: _NOT_FOUND, 409: {"model": ErrorBody, "description": "Cancelled orders cannot be fulfilled (`invalid_state`)"}, 422: _VALIDATION},
)
def fulfil_order(order_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    return orders.fulfil_order(conn, order_id)
