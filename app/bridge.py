"""Framework-free request router used by the browser demo (Pyodide).

The GitHub Pages demo ships the service layer unchanged and drives it through ``handle_json`` instead
of HTTP, so the browser runs the very same ledger, idempotency and locking code as the server. The
route table is not written by hand: ``ROUTES`` aggregates the system routes defined here with the
``BRIDGE_ROUTES`` every domain module publishes (``catalog``, ``inventory``, ``orders``, ``reports``),
compiled once at import. The parity tests keep this table and the FastAPI routers identical.

Contract
--------
* ``handle(method, url, body=None, headers=None)`` → ``{"status", "headers", "body"}``. ``body`` is the
  handler's JSON-able result, ``None`` for a 204, or the raw ``str`` of a CSV export together with the
  ``Content-Type`` / ``Content-Disposition`` / ``X-Row-Count`` headers the handler set. Only
  ``handle_json`` (the JavaScript entry point) JSON-encodes the envelope.
* Path and method match → handler. Path match with another method → 405 ``method_not_allowed`` with an
  ``Allow`` header listing every method the path accepts. No match → 404 ``not_found``.
* ``ServiceError`` → its status and ``{"detail", "code"}``; pydantic ``ValidationError`` and malformed
  JSON → 422 ``validation_error`` with the same error-list shape as the server; any other exception →
  500 ``internal`` (logged, transaction rolled back, connection kept usable).
* One shared connection (``get_conn()`` / ``_conn``), migrated and seeded on first use.

This module stays importable without FastAPI, Starlette or uvicorn and never imports the server-only
modules (``deps``, ``observability``, ``security``, ``main``) — it is listed in ``common.BROWSER_MODULES``.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from . import __version__, catalog, db, inventory, ledger, orders, reports
from .common import INTERNAL, METHOD_NOT_ALLOWED, NOT_FOUND, VALIDATION, BridgeCall, BridgeHandler, BridgeRoute, ServiceError
from .seed import seed

RUNTIME = "pyodide"
INTERNAL_ERROR_DETAIL = "internal error"
METHOD_NOT_ALLOWED_DETAIL = "method not allowed"

logger = logging.getLogger("stockline.bridge")
_STARTED = time.monotonic()
_conn: sqlite3.Connection | None = None


# --------------------------------------------------------------------------- connection
def get_conn() -> sqlite3.Connection:
    """The demo's single shared connection: opened lazily from ``db.DB_PATH``, migrated and seeded on first use.

    Tests reset ``bridge._conn = None`` (after re-pointing ``db.DB_PATH``) to start from a fresh database.
    """
    global _conn
    if _conn is None:
        conn = db.connect()
        try:
            db.init_schema(conn)
            seed(conn)
        except BaseException:
            conn.close()
            raise
        _conn = conn
    return _conn


# --------------------------------------------------------------------------- system routes
def uptime_s() -> float:
    """Seconds since this module was imported, one decimal place (mirrors the server's ``/health``)."""
    return round(time.monotonic() - _STARTED, 1)


def _health(call: BridgeCall) -> tuple[int, dict]:
    call.conn.execute("SELECT 1")
    return 200, {
        "status": "ok",
        "version": __version__,
        "schema_version": db.schema_version(call.conn),
        "runtime": RUNTIME,
        "uptime_s": uptime_s(),
    }


def _integrity(call: BridgeCall) -> tuple[int, dict]:
    return 200, ledger.ledger_integrity(call.conn)


def _rebuild(call: BridgeCall) -> tuple[int, dict]:
    return 200, ledger.rebuild_balances(call.conn)


SYSTEM_ROUTES: list[BridgeRoute] = [
    ("GET", r"^/health$", _health),
    ("GET", r"^/integrity$", _integrity),
    ("POST", r"^/integrity/rebuild$", _rebuild),
]

# The complete table, in declaration order (literal paths precede parameterised ones within each module).
ROUTE_TABLE: list[BridgeRoute] = SYSTEM_ROUTES + catalog.BRIDGE_ROUTES + inventory.BRIDGE_ROUTES + orders.BRIDGE_ROUTES + reports.BRIDGE_ROUTES
ROUTES: list[tuple[str, re.Pattern[str], BridgeHandler]] = [(method, re.compile(pattern), handler) for method, pattern, handler in ROUTE_TABLE]


# --------------------------------------------------------------------------- request plumbing
def _match(method: str, path: str) -> tuple[BridgeHandler | None, dict[str, int], set[str]]:
    """Find the handler for ``method`` + ``path``; also returns the methods of every route whose path matched (405 ``Allow``)."""
    allowed: set[str] = set()
    for route_method, pattern, handler in ROUTES:
        found = pattern.match(path)
        if found is None:
            continue
        allowed.add(route_method)
        if route_method == method:
            return handler, {name: int(value) for name, value in found.groupdict().items()}, allowed
    return None, {}, allowed


def _parse_body(body: Any) -> Any:
    """Parsed JSON body; ``{}`` when there is none. Raises ``json.JSONDecodeError`` for malformed text."""
    if body is None or body == "" or body == b"":
        return {}
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if not isinstance(body, str):
        return body  # already-parsed data from a Python caller
    return json.loads(body)


def _validation_detail(exc: ValidationError) -> list[dict]:
    """pydantic errors in the server's shape: ``loc`` prefixed with ``body``, no documentation ``url``, JSON-safe ``ctx``."""
    return [{**err, "loc": ["body", *err.get("loc", [])]} for err in json.loads(exc.json(include_url=False))]


def _json_invalid_detail(exc: json.JSONDecodeError) -> list[dict]:
    """Malformed request JSON, shaped like FastAPI's ``json_invalid`` error."""
    return [{"type": "json_invalid", "loc": ["body", exc.pos], "msg": "JSON decode error", "input": {}, "ctx": {"error": exc.msg}}]


def _reply(status: int, body: Any, headers: dict[str, str]) -> dict:
    return {"status": status, "headers": headers, "body": body}


def _rollback(conn: sqlite3.Connection) -> None:
    """Leave the shared connection clean after a failed handler."""
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        logger.warning("rollback after a failed bridge request did not succeed", exc_info=True)


# --------------------------------------------------------------------------- entry points
def handle(method: str, url: str, body: str | None = None, headers: dict[str, str] | None = None) -> dict:
    """Dispatch one request and return ``{"status": int, "headers": {...}, "body": <json-able | str | None>}``.

    ``url`` is a path with an optional query string (a missing leading slash or a trailing slash is
    tolerated); only the first value of a repeated query key is used. ``headers`` keys are matched
    case-insensitively. CSV export bodies are returned as ``str`` exactly as the handler produced them.
    """
    verb = method.upper()
    parts = urlsplit(url)
    path = "/" + parts.path.strip("/")
    query = {key: values[0] for key, values in parse_qs(parts.query, keep_blank_values=True).items()}
    handler, params, allowed = _match(verb, path)
    if handler is None:
        if allowed:
            return _reply(405, {"detail": METHOD_NOT_ALLOWED_DETAIL, "code": METHOD_NOT_ALLOWED}, {"Allow": ", ".join(sorted(allowed))})
        return _reply(404, {"detail": f"no route {verb} {path}", "code": NOT_FOUND}, {})
    out_headers: dict[str, str] = {}
    try:
        data = _parse_body(body)
    except json.JSONDecodeError as exc:
        return _reply(422, {"detail": _json_invalid_detail(exc), "code": VALIDATION}, out_headers)
    conn = get_conn()
    call = BridgeCall(
        conn=conn,
        params=params,
        query=query,
        body=data,
        headers={str(key).lower(): value for key, value in (headers or {}).items()},
        out_headers=out_headers,
    )
    try:
        status, result = handler(call)
    except ServiceError as exc:
        return _reply(exc.status, exc.to_body(), out_headers)
    except ValidationError as exc:
        return _reply(422, {"detail": _validation_detail(exc), "code": VALIDATION}, out_headers)
    except Exception:
        logger.exception("unhandled error in bridge handler for %s %s", verb, path)
        return _reply(500, {"detail": INTERNAL_ERROR_DETAIL, "code": INTERNAL}, out_headers)
    finally:
        _rollback(conn)  # a handler that failed mid-transaction must not poison the shared connection
    return _reply(status, None if status == 204 else result, out_headers)


def handle_json(method: str, url: str, body: str | None = None, headers_json: str | None = None) -> str:
    """String-in / string-out wrapper for the JavaScript side (avoids proxy juggling across the Pyodide boundary)."""
    headers = json.loads(headers_json) if headers_json else {}
    return json.dumps(handle(method, url, body or None, headers if isinstance(headers, dict) else {}), default=str)
