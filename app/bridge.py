"""Framework-free request router used by the browser demo (Pyodide).

It mirrors the FastAPI routes in ``main.py`` one-to-one but talks to the
service layer directly, so the GitHub Pages demo executes the *same* Python
business logic (ledger, idempotency, optimistic locking) as the real server.
Kept intentionally tiny: parse -> validate with pydantic -> call service.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from . import __version__, db, schemas, service
from .seed import seed

_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = db.connect()
        db.init_schema(_conn)
        seed(_conn)
    return _conn


def _q(qs: dict[str, list[str]], key: str, default: Any = None, cast=str):
    if key not in qs:
        return default
    v = qs[key][0]
    if cast is bool:
        return v.lower() in ("1", "true", "yes")
    return cast(v)


def _page(items, total, limit, offset):
    return {"items": items, "total": total, "limit": limit, "offset": offset}


ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    ("GET", re.compile(r"^/health$"), "health"),
    ("GET", re.compile(r"^/integrity$"), "integrity"),
    ("GET", re.compile(r"^/stores$"), "list_stores"),
    ("POST", re.compile(r"^/stores$"), "create_store"),
    ("GET", re.compile(r"^/products$"), "list_products"),
    ("POST", re.compile(r"^/products$"), "create_product"),
    ("GET", re.compile(r"^/products/(?P<product_id>\d+)$"), "get_product"),
    ("GET", re.compile(r"^/inventory$"), "list_inventory"),
    ("GET", re.compile(r"^/inventory/(?P<store_id>\d+)/(?P<product_id>\d+)$"), "get_inventory"),
    ("POST", re.compile(r"^/inventory/(?P<store_id>\d+)/(?P<product_id>\d+)/adjust$"), "adjust"),
    ("GET", re.compile(r"^/inventory/(?P<store_id>\d+)/(?P<product_id>\d+)/movements$"), "movements"),
    ("POST", re.compile(r"^/transfers$"), "transfer"),
    ("GET", re.compile(r"^/orders$"), "list_orders"),
    ("POST", re.compile(r"^/orders$"), "place_order"),
    ("GET", re.compile(r"^/orders/(?P<order_id>\d+)$"), "get_order"),
    ("POST", re.compile(r"^/orders/(?P<order_id>\d+)/cancel$"), "cancel_order"),
    ("POST", re.compile(r"^/orders/(?P<order_id>\d+)/fulfil$"), "fulfil_order"),
    ("GET", re.compile(r"^/reports/reorder$"), "reorder"),
]


def handle(method: str, url: str, body: str | None = None, headers: dict[str, str] | None = None) -> dict:
    """Return {"status": int, "headers": {...}, "body": <json-able>}."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    parts = urlsplit(url)
    path, qs = parts.path.rstrip("/") or "/", parse_qs(parts.query)
    conn = get_conn()
    out_headers: dict[str, str] = {}
    try:
        for m, pat, name in ROUTES:
            match = pat.match(path)
            if not match or m != method.upper():
                continue
            params = {k: int(v) for k, v in match.groupdict().items()}
            data = json.loads(body) if body else {}
            status, result = _dispatch(name, conn, params, qs, data, headers, out_headers)
            return {"status": status, "headers": out_headers, "body": result}
        return {"status": 404, "headers": out_headers, "body": {"detail": f"no route {method} {path}"}}
    except service.ServiceError as e:
        return {"status": e.status, "headers": out_headers, "body": {"detail": e.detail}}
    except ValidationError as e:
        return {"status": 422, "headers": out_headers, "body": {"detail": json.loads(e.json())}}
    except (ValueError, json.JSONDecodeError) as e:
        return {"status": 422, "headers": out_headers, "body": {"detail": str(e)}}


def _dispatch(name, conn, p, qs, data, headers, out_headers):
    if name == "health":
        return 200, {"status": "ok", "version": __version__, "runtime": "pyodide"}
    if name == "integrity":
        return 200, service.ledger_integrity(conn)
    if name == "list_stores":
        return 200, service.list_stores(conn)
    if name == "create_store":
        return 201, service.create_store(conn, schemas.StoreIn(**data))
    if name == "list_products":
        limit, offset = _q(qs, "limit", 20, int), _q(qs, "offset", 0, int)
        items, total = service.list_products(conn, _q(qs, "q"), _q(qs, "category"), limit, offset)
        return 200, _page(items, total, limit, offset)
    if name == "create_product":
        return 201, service.create_product(conn, schemas.ProductIn(**data))
    if name == "get_product":
        return 200, service.get_product(conn, p["product_id"])
    if name == "list_inventory":
        limit, offset = _q(qs, "limit", 50, int), _q(qs, "offset", 0, int)
        items, total = service.list_inventory(conn, _q(qs, "store_id", None, int), _q(qs, "low_stock", False, bool), limit, offset)
        return 200, _page(items, total, limit, offset)
    if name == "get_inventory":
        return 200, service.get_inventory_row(conn, p["store_id"], p["product_id"])
    if name == "adjust":
        return 200, service.adjust_stock(conn, p["store_id"], p["product_id"], schemas.StockAdjust(**data))
    if name == "movements":
        return 200, service.movements(conn, p["store_id"], p["product_id"], _q(qs, "limit", 50, int))
    if name == "transfer":
        return 200, service.transfer(conn, schemas.TransferIn(**data))
    if name == "list_orders":
        limit, offset = _q(qs, "limit", 20, int), _q(qs, "offset", 0, int)
        items, total = service.list_orders(conn, _q(qs, "store_id", None, int), _q(qs, "status"), limit, offset)
        return 200, _page(items, total, limit, offset)
    if name == "place_order":
        order, created = service.place_order(conn, schemas.OrderIn(**data), headers.get("idempotency-key"))
        out_headers["Idempotent-Replayed"] = "false" if created else "true"
        return (201 if created else 200), order
    if name == "get_order":
        return 200, service.get_order(conn, p["order_id"])
    if name == "cancel_order":
        return 200, service.cancel_order(conn, p["order_id"])
    if name == "fulfil_order":
        return 200, service.fulfil_order(conn, p["order_id"])
    if name == "reorder":
        return 200, service.reorder_report(conn)
    raise service.ServiceError(500, f"unhandled route {name}")


def handle_json(method: str, url: str, body: str | None = None, headers_json: str | None = None) -> str:
    """String-in / string-out wrapper for the JS side (avoids proxy juggling)."""
    headers = json.loads(headers_json) if headers_json else {}
    return json.dumps(handle(method, url, body or None, headers), default=str)
