"""Shared, framework-free primitives used by every layer (server, bridge and browser demo).

Only the standard library and pydantic may be imported here: this module is shipped to the
browser (Pyodide) together with the service layer, see ``BROWSER_MODULES``.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

# Modules shipped to the browser demo, in load order. The literal below is parsed from the
# source text by the site builder and the parity tests, so keep its exact shape.
BROWSER_MODULES: tuple[str, ...] = (
    "__init__.py", "common.py", "db.py", "schemas.py", "ledger.py", "catalog.py",
    "inventory.py", "orders.py", "reports.py", "service.py", "seed.py", "bridge.py",
)
SERVER_ONLY_MODULES: frozenset[str] = frozenset({"main.py", "deps.py", "observability.py", "security.py"})

# Canonical machine-readable error codes (``ServiceError.code`` / ``ErrorBody.code``).
NOT_FOUND = "not_found"
CONFLICT = "conflict"
INSUFFICIENT_STOCK = "insufficient_stock"
VERSION_CONFLICT = "version_conflict"
DUPLICATE = "duplicate"
IDEMPOTENCY_KEY_REUSE = "idempotency_key_reuse"
IDEMPOTENCY_CONFLICT = "idempotency_conflict"
PRODUCT_INACTIVE = "product_inactive"
INVALID_STATE = "invalid_state"
VALIDATION = "validation_error"
METHOD_NOT_ALLOWED = "method_not_allowed"
UNAUTHORIZED = "unauthorized"
POOL_EXHAUSTED = "pool_exhausted"
INTERNAL = "internal"

CSV_ROW_CAP = 10_000

_INT_RE = re.compile(r"\s*[+-]?\d+\s*")
_TRUE_WORDS = frozenset({"1", "true", "yes"})
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


@dataclass
class ServiceError(Exception):
    """Domain error carrying an HTTP-ish status, a human message and a machine-readable code.

    Positional construction ``ServiceError(409, "msg")`` keeps working (``code`` defaults to ``"error"``).
    """

    status: int
    detail: str
    code: str = "error"

    def __str__(self) -> str:
        return self.detail

    def to_body(self) -> dict:
        """JSON body for API and bridge responses."""
        return {"detail": self.detail, "code": self.code}


def not_found(what: str, id_: int | str) -> ServiceError:
    """Build the canonical 404 error for a missing entity."""
    return ServiceError(404, f"{what} {id_} not found", NOT_FOUND)


def now_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SS.mmmZ`` — the same shape as SQLite's ``strftime('%Y-%m-%dT%H:%M:%fZ','now')``."""
    now = datetime.now(UTC)
    return f"{now:%Y-%m-%dT%H:%M:%S}.{now.microsecond // 1000:03d}Z"


def request_hash(payload: Any) -> str:
    """Stable fingerprint of a JSON-able payload (key order independent); used for idempotency checks."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def like_pattern(q: str) -> str:
    """Escape ``%``, ``_`` and ``\\`` in a user search term and wrap it in ``%…%`` for ``LIKE ? ESCAPE '\\'``."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def page(items: list, total: int, limit: int, offset: int) -> dict:
    """Standard paginated response body."""
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value  # spreadsheet formula-injection guard; numbers are never strings here
    return value


def csv_text(rows: Iterable[dict], columns: list[str]) -> str:
    """Render ``rows`` as CSV: header row first, ``\\n`` line ends, booleans as ``true``/``false``, ``None`` empty."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row.get(col)) for col in columns])
    return buf.getvalue()


def csv_filename(stem: str) -> str:
    """``<stem>-YYYYMMDD.csv`` using the current UTC date."""
    return f"{stem}-{datetime.now(UTC):%Y%m%d}.csv"


class ErrorBody(BaseModel):
    """Error response body shared by the API documentation and the bridge."""

    detail: Any
    code: str = "error"
    request_id: str | None = None


@dataclass
class BridgeCall:
    """Everything a bridge (browser) route handler receives for one request."""

    conn: sqlite3.Connection
    params: dict[str, int] = field(default_factory=dict)  # path params (regex groups, already int)
    query: dict[str, str] = field(default_factory=dict)  # first value per key, raw strings
    body: Any = field(default_factory=dict)  # parsed JSON, {} when there was no body
    headers: dict[str, str] = field(default_factory=dict)  # lower-cased keys
    out_headers: dict[str, str] = field(default_factory=dict)  # response headers set by the handler

    def qint(self, key: str, default: int | None = None, *, lo: int | None = None, hi: int | None = None) -> int | None:
        """Integer query parameter with optional bounds; 422 ``validation_error`` when malformed or out of range."""
        raw = self.query.get(key)
        if raw is None:
            return default
        if not _INT_RE.fullmatch(raw):
            raise ServiceError(422, f"query parameter {key!r} must be an integer", VALIDATION)
        value = int(raw)
        if lo is not None and value < lo:
            raise ServiceError(422, f"query parameter {key!r} must be >= {lo}", VALIDATION)
        if hi is not None and value > hi:
            raise ServiceError(422, f"query parameter {key!r} must be <= {hi}", VALIDATION)
        return value

    def qbool(self, key: str, default: bool = False) -> bool:
        """Boolean query parameter: ``1``/``true``/``yes`` (any case) are true, anything else false."""
        raw = self.query.get(key)
        if raw is None:
            return default
        return raw.strip().lower() in _TRUE_WORDS

    def qstr(
        self, key: str, default: str | None = None, *, max_length: int | None = None, choices: Iterable[str] | None = None
    ) -> str | None:
        """String query parameter with optional length cap and allowed values; 422 ``validation_error`` on violation."""
        raw = self.query.get(key)
        if raw is None:
            return default
        if max_length is not None and len(raw) > max_length:
            raise ServiceError(422, f"query parameter {key!r} must be at most {max_length} characters", VALIDATION)
        if choices is not None:
            allowed = tuple(choices)
            if raw not in allowed:
                raise ServiceError(422, f"query parameter {key!r} must be one of {', '.join(allowed)}", VALIDATION)
        return raw


BridgeHandler = Callable[[BridgeCall], tuple[int, Any]]  # returns (status, json-able body or None)
BridgeRoute = tuple[str, str, BridgeHandler]  # ("GET", r"^/orders/(?P<order_id>\d+)$", handler)
