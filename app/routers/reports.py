"""HTTP surface of the reports domain: ``/reports/reorder``, ``/reports/reorder.csv``, ``/reports/summary``, ``/reports/sales``."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query, Response

from .. import reports
from ..common import ErrorBody
from ..deps import get_conn

router = APIRouter(tags=["reports"])

_ERR_422 = {422: {"model": ErrorBody}}
_CSV_RESPONSES = {200: {"content": {"text/csv": {"schema": {"type": "string"}}}, "description": "CSV export"}, **_ERR_422}


@router.get(
    "/reports/reorder",
    response_model=list[reports.ReorderRow],
    summary="SKUs at/below reorder point with netted sales velocity",
    responses=_ERR_422,
)
def reorder_report(
    days: int = Query(reports.DEFAULT_DAYS, ge=reports.MIN_DAYS, le=reports.MAX_DAYS, description="Sales window in days"),
    store_id: int | None = Query(None, description="Restrict to one store"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return reports.reorder_report(conn, days=days, store_id=store_id)


@router.get("/reports/reorder.csv", response_class=Response, summary="Reorder report as CSV", responses=_CSV_RESPONSES)
def reorder_report_csv(
    days: int = Query(reports.DEFAULT_DAYS, ge=reports.MIN_DAYS, le=reports.MAX_DAYS, description="Sales window in days"),
    store_id: int | None = Query(None, description="Restrict to one store"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    text, row_count, truncated = reports.export_reorder_csv(conn, days=days, store_id=store_id)
    return Response(content=text, media_type=reports.CSV_MEDIA_TYPE, headers=reports.csv_headers(row_count, truncated))


@router.get("/reports/summary", response_model=reports.SummaryReport, summary="Stock valuation per store, totals, order and product counts")
def summary_report(conn: sqlite3.Connection = Depends(get_conn)):
    return reports.summary_report(conn)


@router.get(
    "/reports/sales",
    response_model=list[reports.SalesRow],
    summary="Sales of non-cancelled orders grouped by day or by product",
    responses=_ERR_422,
)
def sales_report(
    days: int = Query(reports.DEFAULT_DAYS, ge=reports.MIN_DAYS, le=reports.MAX_DAYS, description="Sales window in days"),
    store_id: int | None = Query(None, description="Restrict to one store"),
    group_by: str = Query("day", pattern="^(day|product)$", description="day: calendar days ascending; product: by revenue"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return reports.sales_report(conn, days=days, store_id=store_id, group_by=group_by)
