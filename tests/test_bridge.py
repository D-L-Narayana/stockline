"""The browser demo router must behave like the HTTP API.

``app.bridge`` is the framework-free dispatcher the Pyodide demo drives through ``handle_json``. These tests pin
its contract: the route table aggregates the system routes and every domain module's ``BRIDGE_ROUTES``; error
bodies carry machine-readable codes; a known path with the wrong method is a 405 with ``Allow``; query bounds are
enforced like the FastAPI routers (422 ``validation_error``); pydantic and malformed-JSON errors are 422s; CSV
exports pass through as text with their headers; a 204 has a ``null`` body; transfers are entities (201 on create).
"""
import ast
import json
from pathlib import Path

import pytest

from app import __version__, bridge, catalog, common, db, inventory, orders, reports

BRIDGE_SOURCE = Path(__file__).resolve().parent.parent / "app" / "bridge.py"
FORBIDDEN_IMPORTS = {"fastapi", "starlette", "uvicorn", "queue", "threading"}
SERVER_ONLY_RELATIVE = {".deps", ".observability", ".security", ".main"}
CSV_ROUTES = [  # (url, filename stem, header columns)
    ("/inventory/export.csv?store_id=1", "inventory", inventory.INVENTORY_CSV_COLUMNS),
    ("/movements/export.csv?store_id=1&product_id=1", "movements", inventory.MOVEMENT_CSV_COLUMNS),
    ("/orders/export.csv?status=fulfilled", "orders", orders.ORDER_CSV_COLUMNS),
    ("/reports/reorder.csv?days=30", "reorder", reports.REORDER_COLUMNS),
]
BOUND_VIOLATIONS = (
    "/products?limit=0",
    "/products?limit=101",
    "/products?offset=-1",
    "/products?q=" + "x" * 61,
    "/orders?limit=abc",
    "/orders?status=bogus",
    "/inventory?limit=201",
    "/inventory/1/1/movements?limit=0",
    "/inventory/1/1/movements?before_id=0",
    "/movements?reason=bogus",
    "/movements?limit=501",
    "/transfers?offset=-1",
    "/reports/reorder?days=0",
    "/reports/reorder?days=366",
    "/reports/sales?group_by=bogus",
)


@pytest.fixture()
def br(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "b.db"))
    bridge._conn = None
    yield bridge
    conn = bridge._conn
    bridge._conn = None
    if conn is not None:
        conn.close()


def call(br, method, url, body=None, headers=None):
    """Dispatch one request; dict/list bodies are JSON-encoded, ``str`` bodies are sent verbatim (malformed-JSON probes)."""
    raw = body if body is None or isinstance(body, str) else json.dumps(body)
    return br.handle(method, url, raw, headers)


# --------------------------------------------------------------------------- route table and system routes
def test_health_and_seed(br):
    r = br.handle("GET", "/health")
    assert r["status"] == 200
    assert set(r["body"]) == {"status", "version", "schema_version", "runtime", "uptime_s"}
    assert r["body"]["status"] == "ok"
    assert r["body"]["version"] == __version__
    assert r["body"]["schema_version"] == 2
    assert r["body"]["runtime"] == "pyodide"
    assert r["body"]["uptime_s"] >= 0
    assert len(br.handle("GET", "/stores")["body"]) == 3
    assert br.handle("GET", "/products?limit=5")["body"]["total"] == 12


def test_route_table_aggregates_system_and_domain_routes(br):
    system = getattr(br, "SYSTEM_ROUTES", None)
    assert system is not None, "bridge.SYSTEM_ROUTES is missing"
    assert [(method, pattern) for method, pattern, _handler in system] == [
        ("GET", r"^/health$"),
        ("GET", r"^/integrity$"),
        ("POST", r"^/integrity/rebuild$"),
    ]
    expected = list(system) + catalog.BRIDGE_ROUTES + inventory.BRIDGE_ROUTES + orders.BRIDGE_ROUTES + reports.BRIDGE_ROUTES
    assert len(br.ROUTES) == len(expected) == 34
    for (method, pattern, handler), (exp_method, exp_pattern, exp_handler) in zip(br.ROUTES, expected, strict=True):
        assert method == exp_method
        assert pattern.pattern == exp_pattern  # compiled once, at import
        assert handler is exp_handler


def test_bridge_source_imports_no_server_code():
    tree = ast.parse(BRIDGE_SOURCE.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.module:
                names.add(f".{node.module.split('.')[0]}")
            elif node.level:
                names.update(f".{alias.name}" for alias in node.names)
            else:
                names.add((node.module or "").split(".")[0])
    assert not names & FORBIDDEN_IMPORTS, sorted(names & FORBIDDEN_IMPORTS)
    assert not names & SERVER_ONLY_RELATIVE, sorted(names & SERVER_ONLY_RELATIVE)


# --------------------------------------------------------------------------- orders
def test_order_idempotency_and_409(br):
    body = {"store_id": 1, "lines": [{"product_id": 1, "quantity": 1}]}
    r1 = call(br, "POST", "/orders", body, {"Idempotency-Key": "abc"})
    r2 = call(br, "POST", "/orders", body, {"Idempotency-Key": "abc"})
    assert (r1["status"], r2["status"]) == (201, 200)
    assert r1["body"]["id"] == r2["body"]["id"]
    assert r1["headers"]["Idempotent-Replayed"] == "false"
    assert r2["headers"]["Idempotent-Replayed"] == "true"
    assert "request_hash" not in r1["body"]
    assert r1["body"]["updated_at"] == r1["body"]["created_at"]
    big = call(br, "POST", "/orders", {"store_id": 1, "lines": [{"product_id": 1, "quantity": 999}]})
    assert (big["status"], big["body"].get("code")) == (409, "insufficient_stock")
    mismatch = call(br, "POST", "/orders", {"store_id": 1, "lines": [{"product_id": 1, "quantity": 2}]}, {"Idempotency-Key": "abc"})
    assert (mismatch["status"], mismatch["body"].get("code")) == (422, "idempotency_key_reuse")


def test_idempotency_key_length_bound(br):
    before = br.handle("GET", "/orders?limit=1")["body"]["total"]
    body = {"store_id": 1, "lines": [{"product_id": 2, "quantity": 1}]}
    too_long = call(br, "POST", "/orders", body, {"Idempotency-Key": "k" * 65})
    assert (too_long["status"], too_long["body"].get("code")) == (422, "validation_error")
    assert br.handle("GET", "/orders?limit=1")["body"]["total"] == before, "a rejected key must not create an order"
    assert call(br, "POST", "/orders", body, {"Idempotency-Key": "k" * 64})["status"] == 201
    transfer = {"from_store_id": 1, "to_store_id": 2, "product_id": 2, "quantity": 1}
    rejected = call(br, "POST", "/transfers", transfer, {"Idempotency-Key": "t" * 65})
    assert (rejected["status"], rejected["body"].get("code")) == (422, "validation_error")


# --------------------------------------------------------------------------- error shapes
def test_validation_errors_carry_the_code(br):
    r = call(br, "POST", "/orders", {"store_id": 1, "lines": []})
    assert (r["status"], r["body"].get("code")) == (422, "validation_error")
    assert isinstance(r["body"]["detail"], list) and r["body"]["detail"]
    assert {"type", "loc", "msg"} <= set(r["body"]["detail"][0])
    assert "url" not in r["body"]["detail"][0], "error items must have the same shape as the server's validation errors"
    assert r["body"]["detail"][0]["loc"][0] == "body", "validation locations are prefixed like the server's"
    bad = call(br, "POST", "/orders", "{not json")
    assert (bad["status"], bad["body"].get("code")) == (422, "validation_error")
    assert isinstance(bad["body"]["detail"], list) and bad["body"]["detail"][0]["type"] == "json_invalid"
    assert bad["body"]["detail"][0]["loc"][0] == "body"
    for url in BOUND_VIOLATIONS:
        r = br.handle("GET", url)
        assert (r["status"], r["body"].get("code")) == (422, "validation_error"), url
    assert br.handle("GET", "/products?limit=100&offset=0")["status"] == 200
    assert br.handle("GET", "/reports/reorder?days=365")["status"] == 200


def test_not_found_and_method_not_allowed(br):
    r = br.handle("GET", "/nope")
    assert r["status"] == 404
    assert set(r["body"]) == {"detail", "code"} and r["body"]["code"] == "not_found"
    r = br.handle("GET", "/orders/9999")
    assert (r["status"], r["body"].get("code"), r["body"].get("detail")) == (404, "not_found", "order 9999 not found")
    r = br.handle("PUT", "/orders")
    assert r["status"] == 405
    assert r["body"] == {"detail": "method not allowed", "code": "method_not_allowed"}
    assert r["headers"]["Allow"] == "GET, POST"
    assert br.handle("DELETE", "/health")["headers"].get("Allow") == "GET"
    head = br.handle("HEAD", "/health")  # the table declares GET only, so HEAD is a wrong method, not an unknown path
    assert (head["status"], head["headers"].get("Allow"), head["body"]["code"]) == (405, "GET", "method_not_allowed")
    rebuild = br.handle("GET", "/integrity/rebuild")
    assert (rebuild["status"], rebuild["headers"].get("Allow")) == (405, "POST")
    assert br.handle("POST", "/products/1/inventory")["headers"].get("Allow") == "GET"
    assert br.handle("PUT", "/stores/1")["headers"].get("Allow") == "GET, PATCH"
    assert br.handle("PUT", "/products/1")["headers"].get("Allow") == "DELETE, GET, PATCH"
    assert br.handle("DELETE", "/orders/1")["status"] == 405
    assert br.handle("GET", "/orders/export.csv/extra")["status"] == 404


def test_unexpected_handler_error_is_a_500_internal(br, monkeypatch):
    def boom(call):
        raise RuntimeError("kaboom")

    routes = list(br.ROUTES)
    index = next(i for i, (method, pattern, _handler) in enumerate(routes) if method == "GET" and pattern.pattern == r"^/stores$")
    method, pattern, _handler = routes[index]
    routes[index] = (method, pattern, boom)
    monkeypatch.setattr(br, "ROUTES", routes)
    r = br.handle("GET", "/stores")
    assert r["status"] == 500
    assert r["body"] == {"detail": "internal error", "code": "internal"}
    assert br.handle("GET", "/health")["status"] == 200, "the shared connection must stay usable"


# --------------------------------------------------------------------------- inventory, transfers, reports
def test_adjust_transfer_reports(br):
    r = call(br, "POST", "/inventory/1/1/adjust", {"delta": 5, "reason": "receipt"})
    assert r["status"] == 200 and r["body"]["on_hand"] > 0
    assert {"price_cents", "value_cents"} <= set(r["body"])
    stale = call(br, "POST", "/inventory/1/1/adjust", {"delta": 1, "reason": "receipt", "expected_version": 0})
    assert (stale["status"], stale["body"].get("code")) == (409, "version_conflict")
    t = call(br, "POST", "/transfers", {"from_store_id": 1, "to_store_id": 2, "product_id": 1, "quantity": 1})
    assert t["status"] == 201
    assert {"from", "to", "id"} <= set(t["body"])
    assert {"from_store_id", "to_store_id", "product_id", "quantity", "idempotency_key", "created_at"} <= set(t["body"])
    assert t["headers"]["Idempotent-Replayed"] == "false"
    assert br.handle("GET", "/reports/reorder")["status"] == 200
    assert br.handle("GET", "/integrity")["body"]["ok"] is True
    assert br.handle("GET", "/inventory?low_stock=true&store_id=1")["status"] == 200
    assert br.handle("GET", "/inventory/1/1/movements")["status"] == 200
    assert br.handle("POST", "/orders/1/cancel")["status"] in (200, 409)


def test_transfer_entity_routes_and_idempotency(br):
    body = {"from_store_id": 1, "to_store_id": 2, "product_id": 3, "quantity": 1}
    first = call(br, "POST", "/transfers", body, {"Idempotency-Key": "xfer-1"})
    replay = call(br, "POST", "/transfers", body, {"Idempotency-Key": "xfer-1"})
    assert (first["status"], replay["status"]) == (201, 200)
    assert first["body"]["id"] == replay["body"]["id"]
    assert replay["headers"]["Idempotent-Replayed"] == "true"
    assert first["body"]["from"]["store_id"] == 1 and first["body"]["to"]["store_id"] == 2
    one = br.handle("GET", f"/transfers/{first['body']['id']}")
    assert one["status"] == 200 and one["body"]["idempotency_key"] == "xfer-1"
    listing = br.handle("GET", "/transfers?store_id=2&limit=20")
    assert listing["status"] == 200
    assert set(listing["body"]) == {"items", "total", "limit", "offset"}
    assert first["body"]["id"] in {item["id"] for item in listing["body"]["items"]}
    mismatch = call(br, "POST", "/transfers", {**body, "quantity": 2}, {"Idempotency-Key": "xfer-1"})
    assert (mismatch["status"], mismatch["body"].get("code")) == (422, "idempotency_key_reuse")
    same_store = call(br, "POST", "/transfers", {**body, "to_store_id": 1})
    assert (same_store["status"], same_store["body"].get("code")) == (422, "validation_error")
    short = call(br, "POST", "/transfers", {**body, "quantity": 999})
    assert (short["status"], short["body"].get("code")) == (409, "insufficient_stock")
    assert br.handle("GET", "/transfers/9999")["status"] == 404


def test_receipts_movement_feed_and_system_routes(br):
    receipt = call(br, "POST", "/inventory/1/receipts", {"reference": "PO-77", "lines": [{"product_id": 1, "quantity": 2}, {"product_id": 2, "quantity": 3}]})
    assert receipt["status"] == 201
    assert set(receipt["body"]) == {"reference", "store_id", "lines"} and len(receipt["body"]["lines"]) == 2
    unknown = call(br, "POST", "/inventory/1/receipts", {"reference": "PO-78", "lines": [{"product_id": 999, "quantity": 1}]})
    assert (unknown["status"], unknown["body"].get("code")) == (404, "not_found")
    feed = br.handle("GET", "/movements?limit=5")
    assert feed["status"] == 200 and set(feed["body"]) == {"items", "limit", "next_before_id"}
    assert len(feed["body"]["items"]) == 5 and feed["body"]["next_before_id"] == feed["body"]["items"][-1]["id"]
    older = br.handle("GET", f"/movements?limit=5&before_id={feed['body']['next_before_id']}")
    assert older["status"] == 200 and all(m["id"] < feed["body"]["next_before_id"] for m in older["body"]["items"])
    receipts = br.handle("GET", "/movements?store_id=1&reason=receipt&limit=100")
    assert receipts["body"]["items"] and all(m["reason"] == "receipt" for m in receipts["body"]["items"])
    assert all("balance_after" in m for m in receipts["body"]["items"])
    pair = br.handle("GET", "/inventory/1/1/movements?limit=2")
    assert pair["status"] == 200 and len(pair["body"]) == 2
    paged = br.handle("GET", f"/inventory/1/1/movements?limit=2&before_id={pair['body'][-1]['id']}")
    assert paged["status"] == 200 and all(m["id"] < pair["body"][-1]["id"] for m in paged["body"])
    rebuild = br.handle("POST", "/integrity/rebuild")
    assert rebuild["status"] == 200 and set(rebuild["body"]) == {"checked", "fixed", "backfilled", "ok"}
    integrity = br.handle("GET", "/integrity")
    assert set(integrity["body"]) == {"ok", "checked", "schema_version", "mismatches", "negative", "chain_breaks", "order_total_mismatches", "missing_balance_after"}
    summary = br.handle("GET", "/reports/summary")
    assert summary["status"] == 200
    assert set(summary["body"]) == {"stores", "totals", "orders", "revenue_cents", "products", "generated_at"}
    assert len(summary["body"]["stores"]) == 3
    assert br.handle("GET", "/reports/reorder?days=14&store_id=1")["status"] == 200
    for group_by in ("day", "product"):
        sales = br.handle("GET", f"/reports/sales?days=30&group_by={group_by}")
        assert sales["status"] == 200 and isinstance(sales["body"], list) and sales["body"]


# --------------------------------------------------------------------------- catalog lifecycle
def test_catalog_lifecycle_routes(br):
    store = br.handle("GET", "/stores/1")
    assert store["status"] == 200 and store["body"]["code"] == "BLR-01"
    renamed = call(br, "PATCH", "/stores/1", {"name": "Renamed"})
    assert renamed["status"] == 200 and renamed["body"]["name"] == "Renamed"
    empty = call(br, "PATCH", "/stores/1", {})
    assert (empty["status"], empty["body"].get("code")) == (422, "validation_error")
    dup = call(br, "POST", "/stores", {"code": "BLR-01", "name": "Dup", "region": "South"})
    assert (dup["status"], dup["body"].get("code")) == (409, "duplicate")
    created = call(br, "POST", "/products", {"sku": "NEW-1", "name": "New", "category": "Misc", "price_cents": 100})
    assert created["status"] == 201 and created["body"]["active"] is True
    patched = call(br, "PATCH", f"/products/{created['body']['id']}", {"price_cents": 250, "reorder_point": 1})
    assert patched["status"] == 200 and patched["body"]["price_cents"] == 250 and patched["body"]["updated_at"]
    immutable = call(br, "PATCH", "/products/1", {"sku": "X-1"})
    assert (immutable["status"], immutable["body"].get("code")) == (422, "validation_error")
    rows = br.handle("GET", "/products/1/inventory")
    assert rows["status"] == 200 and [row["store_id"] for row in rows["body"]] == [1, 2, 3]
    assert br.handle("GET", "/products/9999/inventory")["status"] == 404
    assert br.handle("GET", "/stores/9999")["status"] == 404


def test_delete_product_is_a_204_with_null_body(br):
    r = br.handle("DELETE", "/products/12")
    assert r["status"] == 204 and r["body"] is None
    again = br.handle("DELETE", "/products/12")
    assert again["status"] == 204 and again["body"] is None
    product = br.handle("GET", "/products/12")
    assert product["status"] == 200 and product["body"]["active"] is False
    assert br.handle("GET", "/products?limit=100")["body"]["total"] == 11
    assert br.handle("GET", "/products?limit=100&include_inactive=true")["body"]["total"] == 12
    order = call(br, "POST", "/orders", {"store_id": 1, "lines": [{"product_id": 12, "quantity": 1}]})
    assert (order["status"], order["body"].get("code")) == (409, "product_inactive")
    out = json.loads(br.handle_json("DELETE", "/products/12"))
    assert out["status"] == 204 and out["body"] is None
    assert br.handle("DELETE", "/products/9999")["status"] == 404


# --------------------------------------------------------------------------- CSV exports (PLAN §11.3)
@pytest.mark.parametrize(("url", "stem", "columns"), CSV_ROUTES, ids=[stem for _url, stem, _columns in CSV_ROUTES])
def test_csv_exports_pass_through_as_text_with_headers(br, url, stem, columns):
    r = br.handle("GET", url)
    assert r["status"] == 200, r
    assert isinstance(r["body"], str), "CSV bodies must pass through handle() untouched"
    lines = r["body"].split("\n")
    assert lines[0] == ",".join(columns)
    data_rows = [line for line in lines[1:] if line]
    assert data_rows, "the seeded database must yield at least one data row"
    assert r["headers"]["Content-Type"] == "text/csv; charset=utf-8"
    assert r["headers"]["X-Row-Count"] == str(len(data_rows))
    disposition = r["headers"]["Content-Disposition"]
    assert disposition.startswith(f'attachment; filename="{stem}-') and disposition.endswith('.csv"')
    assert "X-Truncated" not in r["headers"]
    out = json.loads(br.handle_json("GET", url, None, "{}"))
    assert out["status"] == 200 and out["body"] == r["body"]
    assert out["headers"]["X-Row-Count"] == r["headers"]["X-Row-Count"]


def test_csv_export_honours_the_row_cap(br, monkeypatch):
    monkeypatch.setattr(common, "CSV_ROW_CAP", 5)
    r = br.handle("GET", "/inventory/export.csv")
    assert r["status"] == 200
    assert (r["headers"]["X-Row-Count"], r["headers"]["X-Truncated"]) == ("5", "true")
    assert len([line for line in r["body"].split("\n") if line]) == 6  # header + 5 rows


# --------------------------------------------------------------------------- transport details
def test_handle_json_roundtrip(br):
    out = json.loads(br.handle_json("GET", "/health", None, json.dumps({"X-Test": "1"})))
    assert out["status"] == 200 and out["body"]["runtime"] == "pyodide"
    body = json.dumps({"store_id": 2, "lines": [{"product_id": 4, "quantity": 1}]})
    first = json.loads(br.handle_json("POST", "/orders", body, json.dumps({"idempotency-key": "json-1"})))
    second = json.loads(br.handle_json("POST", "/orders", body, json.dumps({"Idempotency-Key": "json-1"})))
    assert (first["status"], second["status"]) == (201, 200)
    assert second["headers"]["Idempotent-Replayed"] == "true" and second["body"]["id"] == first["body"]["id"]
    missing = json.loads(br.handle_json("GET", "/orders/9999"))
    assert missing == {"status": 404, "headers": {}, "body": {"detail": "order 9999 not found", "code": "not_found"}}


def test_url_parsing_is_tolerant(br):
    assert br.handle("GET", "/stores/")["status"] == 200  # trailing slash
    assert br.handle("get", "/stores")["status"] == 200  # method case
    assert br.handle("GET", "stores/1")["status"] == 200  # missing leading slash
    assert br.handle("GET", "/stores/1?")["status"] == 200  # empty query string
    assert br.handle("GET", "/products?q=&limit=100")["body"]["total"] == 12  # blank search = no filter, as on the server
    blank = br.handle("GET", "/inventory?store_id=")  # a blank integer is a 422 on the server too
    assert (blank["status"], blank["body"].get("code")) == (422, "validation_error")
