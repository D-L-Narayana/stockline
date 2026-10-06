"""Reports: reorder planning, stock summary / valuation and sales — plus the CSV export and bridge routes.

Browser-safe: standard library, pydantic and the shared primitives only. Every function takes an
open connection first and raises ``ServiceError`` with the canonical codes. Windows are measured
backwards from "now" (UTC) over ``created_at`` timestamps, which share the ``now_iso`` text format.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from . import common
from .common import VALIDATION, BridgeCall, BridgeRoute, ServiceError, csv_filename, csv_text, now_iso

MIN_DAYS = 1
MAX_DAYS = 365
DEFAULT_DAYS = 30
SALES_GROUPINGS = ("day", "product")
CSV_STEM = "reorder"
CSV_MEDIA_TYPE = "text/csv; charset=utf-8"


# --------------------------------------------------------------------------- models
class ReorderRow(BaseModel):
    """One active SKU at or below its reorder point, with the netted sales velocity of the requested window."""

    store_id: int
    store_code: str
    product_id: int
    sku: str
    name: str
    on_hand: int
    reorder_point: int
    days: int
    sold_window: int
    returned_window: int
    net_sold: int
    daily_velocity: float
    days_of_cover: float | None
    suggested_qty: int


class StoreSummary(BaseModel):
    """Stock position of one store over its inventory rows of active products."""

    store_id: int
    store_code: str
    store_name: str
    skus: int
    units: int
    value_cents: int
    low_stock: int


class SummaryTotals(BaseModel):
    skus: int
    units: int
    value_cents: int
    low_stock: int


class OrderCounts(BaseModel):
    placed: int
    fulfilled: int
    cancelled: int


class RevenueSummary(BaseModel):
    placed: int
    fulfilled: int


class ProductCounts(BaseModel):
    active: int
    inactive: int


class SummaryReport(BaseModel):
    """Dashboard summary: per-store valuation, totals, order / revenue / product counts."""

    stores: list[StoreSummary]
    totals: SummaryTotals
    orders: OrderCounts
    revenue_cents: RevenueSummary
    products: ProductCounts
    generated_at: str


class SalesRow(BaseModel):
    """One sales bucket: ``day`` is set when grouping by day, ``product_id``/``sku``/``name`` when grouping by product."""

    day: str | None = None
    product_id: int | None = None
    sku: str | None = None
    name: str | None = None
    orders: int
    units: int
    revenue_cents: int


REORDER_COLUMNS: list[str] = list(ReorderRow.model_fields)

# --------------------------------------------------------------------------- SQL
_REORDER_SQL = """
WITH sold AS (
    SELECT store_id, product_id, -SUM(delta) AS units
    FROM stock_movements
    WHERE reason = 'sale' AND created_at >= :since
    GROUP BY store_id, product_id
), returned AS (
    SELECT store_id, product_id, SUM(delta) AS units
    FROM stock_movements
    WHERE reason = 'return' AND reference LIKE 'cancel:%' AND created_at >= :since
    GROUP BY store_id, product_id
)
SELECT i.store_id, s.code AS store_code, i.product_id, p.sku, p.name, i.on_hand, p.reorder_point,
       COALESCE(so.units, 0) AS sold_window, COALESCE(r.units, 0) AS returned_window
FROM inventory i
JOIN products p ON p.id = i.product_id
JOIN stores s ON s.id = i.store_id
LEFT JOIN sold so ON so.store_id = i.store_id AND so.product_id = i.product_id
LEFT JOIN returned r ON r.store_id = i.store_id AND r.product_id = i.product_id
WHERE p.active = 1 AND i.on_hand <= p.reorder_point AND (:store_id IS NULL OR i.store_id = :store_id)
ORDER BY (p.reorder_point - i.on_hand) DESC, s.code, p.sku
"""

_STORE_SUMMARY_SQL = """
SELECT s.id AS store_id, s.code AS store_code, s.name AS store_name,
       COUNT(x.product_id) AS skus,
       COALESCE(SUM(x.on_hand), 0) AS units,
       COALESCE(SUM(x.on_hand * x.price_cents), 0) AS value_cents,
       COALESCE(SUM(x.on_hand <= x.reorder_point), 0) AS low_stock
FROM stores s
LEFT JOIN (SELECT i.store_id, i.product_id, i.on_hand, p.price_cents, p.reorder_point
           FROM inventory i JOIN products p ON p.id = i.product_id
           WHERE p.active = 1) x ON x.store_id = s.id
GROUP BY s.id
ORDER BY s.id
"""

_SALES_WHERE = """
WHERE o.status <> 'cancelled' AND o.created_at >= :since AND (:store_id IS NULL OR o.store_id = :store_id)
"""

_SALES_BY_DAY_SQL = (
    """
SELECT substr(o.created_at, 1, 10) AS day, COUNT(DISTINCT o.id) AS orders,
       SUM(ol.quantity) AS units, SUM(ol.quantity * ol.unit_price_cents) AS revenue_cents
FROM orders o
JOIN order_lines ol ON ol.order_id = o.id
"""
    + _SALES_WHERE
    + "GROUP BY day ORDER BY day"
)

_SALES_BY_PRODUCT_SQL = (
    """
SELECT ol.product_id, p.sku, p.name, COUNT(DISTINCT o.id) AS orders,
       SUM(ol.quantity) AS units, SUM(ol.quantity * ol.unit_price_cents) AS revenue_cents
FROM orders o
JOIN order_lines ol ON ol.order_id = o.id
JOIN products p ON p.id = ol.product_id
"""
    + _SALES_WHERE
    + "GROUP BY ol.product_id ORDER BY revenue_cents DESC, units DESC, ol.product_id"
)


# --------------------------------------------------------------------------- helpers
def window_start(days: int, *, now: datetime | None = None) -> str:
    """``created_at`` lower bound (inclusive) for a window of ``days`` days ending now, in the ``now_iso`` text format."""
    moment = (now or datetime.now(UTC)) - timedelta(days=days)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


def _check_days(days: int) -> int:
    if days < MIN_DAYS or days > MAX_DAYS:
        raise ServiceError(422, f"days must be between {MIN_DAYS} and {MAX_DAYS}", VALIDATION)
    return days


# --------------------------------------------------------------------------- reports
def reorder_report(conn: sqlite3.Connection, *, days: int = DEFAULT_DAYS, store_id: int | None = None) -> list[dict]:
    """Active SKUs at/below their reorder point, with sales of the last ``days`` days netted against cancellations.

    ``sold_window`` sums the units sold (``sale`` movements), ``returned_window`` the units restocked by order
    cancellations (``return`` movements referencing ``cancel:<order>``); walk-in returns are not netted.
    ``days_of_cover`` is ``None`` when nothing was sold in the window. Rows are ordered by the size of the
    deficit (``reorder_point - on_hand``), then store code and SKU.
    """
    _check_days(days)
    params = {"since": window_start(days), "store_id": store_id}
    rows: list[dict] = []
    for r in conn.execute(_REORDER_SQL, params):
        sold, returned = int(r["sold_window"]), int(r["returned_window"])
        net_sold = max(sold - returned, 0)
        velocity = round(net_sold / days, 2)
        rows.append(
            {
                "store_id": r["store_id"],
                "store_code": r["store_code"],
                "product_id": r["product_id"],
                "sku": r["sku"],
                "name": r["name"],
                "on_hand": r["on_hand"],
                "reorder_point": r["reorder_point"],
                "days": days,
                "sold_window": sold,
                "returned_window": returned,
                "net_sold": net_sold,
                "daily_velocity": velocity,
                "days_of_cover": round(r["on_hand"] / velocity, 1) if velocity > 0 else None,
                "suggested_qty": max(r["reorder_point"] * 2 - r["on_hand"], net_sold, 0),
            }
        )
    return rows


def summary_report(conn: sqlite3.Connection) -> dict:
    """Per-store stock valuation (active products only), totals, order and revenue counts by status, product counts."""
    stores = [dict(r) for r in conn.execute(_STORE_SUMMARY_SQL)]
    totals = {key: sum(store[key] for store in stores) for key in ("skus", "units", "value_cents", "low_stock")}
    by_status = {
        r["status"]: (r["n"], r["revenue"])
        for r in conn.execute("SELECT status, COUNT(*) AS n, COALESCE(SUM(total_cents), 0) AS revenue FROM orders GROUP BY status")
    }
    active, inactive = conn.execute("SELECT COALESCE(SUM(active = 1), 0), COALESCE(SUM(active = 0), 0) FROM products").fetchone()
    return {
        "stores": stores,
        "totals": totals,
        "orders": {status: by_status.get(status, (0, 0))[0] for status in ("placed", "fulfilled", "cancelled")},
        "revenue_cents": {status: by_status.get(status, (0, 0))[1] for status in ("placed", "fulfilled")},
        "products": {"active": active, "inactive": inactive},
        "generated_at": now_iso(),
    }


def sales_report(
    conn: sqlite3.Connection, *, days: int = DEFAULT_DAYS, store_id: int | None = None, group_by: str = "day"
) -> list[dict]:
    """Sales of non-cancelled orders in the last ``days`` days, grouped by calendar day (ascending) or by product (revenue first).

    Every row carries the full ``SalesRow`` key set (unused grouping keys are ``None``) so the HTTP and bridge bodies match.
    """
    if group_by not in SALES_GROUPINGS:
        raise ServiceError(422, f"group_by must be one of {', '.join(SALES_GROUPINGS)}", VALIDATION)
    _check_days(days)
    params = {"since": window_start(days), "store_id": store_id}
    if group_by == "day":
        return [
            {"day": r["day"], "product_id": None, "sku": None, "name": None, "orders": r["orders"], "units": r["units"], "revenue_cents": r["revenue_cents"]}
            for r in conn.execute(_SALES_BY_DAY_SQL, params)
        ]
    return [
        {
            "day": None,
            "product_id": r["product_id"],
            "sku": r["sku"],
            "name": r["name"],
            "orders": r["orders"],
            "units": r["units"],
            "revenue_cents": r["revenue_cents"],
        }
        for r in conn.execute(_SALES_BY_PRODUCT_SQL, params)
    ]


# --------------------------------------------------------------------------- CSV export
def export_reorder_csv(conn: sqlite3.Connection, *, days: int = DEFAULT_DAYS, store_id: int | None = None) -> tuple[str, int, bool]:
    """Reorder report as CSV (``REORDER_COLUMNS`` order): ``(csv_text, row_count, truncated)``, capped at ``common.CSV_ROW_CAP`` rows."""
    rows = reorder_report(conn, days=days, store_id=store_id)
    cap = common.CSV_ROW_CAP
    truncated = len(rows) > cap
    if truncated:
        rows = rows[:cap]
    return csv_text(rows, REORDER_COLUMNS), len(rows), truncated


def csv_headers(row_count: int, truncated: bool) -> dict[str, str]:
    """``Content-Disposition`` / ``X-Row-Count`` (/ ``X-Truncated``) headers of the reorder export; the media type is ``CSV_MEDIA_TYPE``."""
    headers = {"Content-Disposition": f'attachment; filename="{csv_filename(CSV_STEM)}"', "X-Row-Count": str(row_count)}
    if truncated:
        headers["X-Truncated"] = "true"
    return headers


# --------------------------------------------------------------------------- bridge routes
def _window_query(call: BridgeCall) -> tuple[int, int | None]:
    return call.qint("days", DEFAULT_DAYS, lo=MIN_DAYS, hi=MAX_DAYS), call.qint("store_id")


def _bridge_reorder(call: BridgeCall) -> tuple[int, Any]:
    days, store_id = _window_query(call)
    return 200, reorder_report(call.conn, days=days, store_id=store_id)


def _bridge_reorder_csv(call: BridgeCall) -> tuple[int, Any]:
    days, store_id = _window_query(call)
    text, row_count, truncated = export_reorder_csv(call.conn, days=days, store_id=store_id)
    call.out_headers["Content-Type"] = CSV_MEDIA_TYPE
    call.out_headers.update(csv_headers(row_count, truncated))
    return 200, text


def _bridge_summary(call: BridgeCall) -> tuple[int, Any]:
    return 200, summary_report(call.conn)


def _bridge_sales(call: BridgeCall) -> tuple[int, Any]:
    days, store_id = _window_query(call)
    group_by = call.qstr("group_by", "day", choices=SALES_GROUPINGS) or "day"
    return 200, sales_report(call.conn, days=days, store_id=store_id, group_by=group_by)


BRIDGE_ROUTES: list[BridgeRoute] = [
    ("GET", r"^/reports/reorder$", _bridge_reorder),
    ("GET", r"^/reports/reorder\.csv$", _bridge_reorder_csv),
    ("GET", r"^/reports/summary$", _bridge_summary),
    ("GET", r"^/reports/sales$", _bridge_sales),
]
