"""System endpoints: health, ledger integrity audit and rebuild, Prometheus metrics."""
from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .. import __version__, db, deps, ledger
from ..common import ErrorBody
from ..deps import get_conn
from ..observability import METRICS, PROMETHEUS_CONTENT_TYPE

router = APIRouter(tags=["system"])
RUNTIME = "server"
_STARTED = time.monotonic()


class Health(BaseModel):
    """Liveness and readiness summary."""

    status: str
    version: str
    schema_version: int
    runtime: str
    uptime_s: float


class CacheMismatch(BaseModel):
    """Cached ``on_hand`` differs from the ledger sum."""

    store_id: int
    product_id: int
    on_hand: int
    ledger: int


class NegativeBalance(BaseModel):
    """Cached or ledger balance below zero."""

    store_id: int
    product_id: int
    on_hand: int


class ChainBreak(BaseModel):
    """A movement whose ``balance_after`` does not equal the running sum."""

    store_id: int
    product_id: int
    movement_id: int
    expected: int
    actual: int


class OrderTotalMismatch(BaseModel):
    """An order whose ``total_cents`` differs from the sum of its lines."""

    order_id: int
    total_cents: int
    lines_total_cents: int


class IntegrityReport(BaseModel):
    """Result of ``ledger.ledger_integrity``; ``missing_balance_after`` is informational and never affects ``ok``."""

    ok: bool
    checked: int
    schema_version: int
    mismatches: list[CacheMismatch]
    negative: list[NegativeBalance]
    chain_breaks: list[ChainBreak]
    order_total_mismatches: list[OrderTotalMismatch]
    missing_balance_after: int


class RebuildFix(BaseModel):
    """One cached balance corrected by the rebuild."""

    store_id: int
    product_id: int
    before: int
    after: int


class RebuildReport(BaseModel):
    """Result of ``ledger.rebuild_balances``."""

    checked: int
    fixed: list[RebuildFix]
    backfilled: int
    ok: bool


def uptime_s() -> float:
    """Seconds since this module was imported, one decimal place."""
    return round(time.monotonic() - _STARTED, 1)


@router.get(
    "/health",
    response_model=Health,
    summary="Liveness and readiness: database reachable, schema version, uptime",
    responses={503: {"model": ErrorBody, "description": "Connection pool exhausted (Retry-After: 1)"}},
)
def health(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    conn.execute("SELECT 1")
    return {"status": "ok", "version": __version__, "schema_version": db.schema_version(conn), "runtime": RUNTIME, "uptime_s": uptime_s()}


@router.get(
    "/integrity",
    response_model=IntegrityReport,
    summary="Audit the stock ledger against cached balances, running balances and order totals",
)
def integrity(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return ledger.ledger_integrity(conn)


@router.post(
    "/integrity/rebuild",
    response_model=RebuildReport,
    summary="Rebuild cached balances from the ledger (write operation; requires the API key when one is configured)",
    responses={401: {"model": ErrorBody}, 409: {"model": ErrorBody, "description": "A ledger balance is negative; repair the movements first"}},
)
def rebuild(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return ledger.rebuild_balances(conn)


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    summary="Prometheus text exposition: request counters, latency histogram, pool gauges",
    responses={200: {"content": {"text/plain": {"schema": {"type": "string"}}}, "description": "Prometheus exposition format 0.0.4"}},
)
def metrics() -> PlainTextResponse:
    # deps.pool is looked up at call time: reset_state() replaces the module global.
    return PlainTextResponse(METRICS.render_prometheus(deps.pool.stats), media_type=PROMETHEUS_CONTENT_TYPE)
