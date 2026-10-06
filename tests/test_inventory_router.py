"""Inventory router on a private app: valuation fields, search, error codes, receipts, CSV export,
OpenAPI documentation and the bridge table that mirrors the router."""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import common, db, deps, inventory, schemas
from app.common import BridgeCall, ServiceError
from app.routers.inventory import router

ROW_KEYS = {"store_id", "store_code", "product_id", "sku", "name", "on_hand", "reorder_point", "version", "below_reorder", "price_cents", "value_cents"}
CSV_HEADER = ["store_id", "store_code", "product_id", "sku", "name", "on_hand", "reorder_point", "version", "below_reorder", "price_cents", "value_cents"]


@dataclass
class World:
    client: TestClient
    conn: sqlite3.Connection
    s1: int
    s2: int
    s3: int
    p1: int
    p2: int
    p3: int


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(ServiceError, deps.service_error_handler)
    return app


def _store(conn: sqlite3.Connection, code: str) -> int:
    return int(conn.execute("INSERT INTO stores (code, name, region) VALUES (?,?,?)", (code, f"Store {code}", "South")).lastrowid)


def _product(conn: sqlite3.Connection, sku: str, name: str, price_cents: int, reorder_point: int = 5) -> int:
    cur = conn.execute(
        "INSERT INTO products (sku, name, category, price_cents, reorder_point) VALUES (?,?,?,?,?)", (sku, name, "Home", price_cents, reorder_point)
    )
    return int(cur.lastrowid)


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _on_hand(conn: sqlite3.Connection, store_id: int, product_id: int) -> int:
    row = conn.execute("SELECT on_hand FROM inventory WHERE store_id=? AND product_id=?", (store_id, product_id)).fetchone()
    return int(row[0]) if row else 0


def _ok(resp, status: int = 200) -> dict:
    assert resp.status_code == status, f"{resp.request.method} {resp.url} -> {resp.status_code}: {resp.text}"
    return resp.json()


def _bridge(method: str, path: str):
    matches = [(pattern, handler) for m, pattern, handler in inventory.BRIDGE_ROUTES if m == method and re.fullmatch(pattern, path)]
    assert len(matches) == 1, f"expected exactly one bridge route for {method} {path}, found {len(matches)}"
    return matches[0]


def _params(pattern: str, path: str) -> dict[str, int]:
    match = re.fullmatch(pattern, path)
    assert match is not None
    return {k: int(v) for k, v in match.groupdict().items()}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    path = str(tmp_path / "inventory.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    deps.reset_state(seed=False)
    conn = db.connect(path)
    db.init_schema(conn)
    s1, s2, s3 = (_store(conn, code) for code in ("S1", "S2", "S3"))
    p1 = _product(conn, "SKU-1", "Widget", 1000, 5)
    p2 = _product(conn, "SKU-2", "Gadget", 2500, 2)
    p3 = _product(conn, "SKU-3", '=HYPERLINK("x")', 700, 1)
    for store_id, product_id, qty in ((s1, p1, 20), (s1, p2, 3), (s2, p1, 7)):
        inventory.adjust_stock(conn, store_id, product_id, schemas.StockAdjust(delta=qty, reason="receipt", reference="opening-stock"))
    with TestClient(_make_app()) as client:
        yield World(client, conn, s1, s2, s3, p1, p2, p3)
    conn.close()
    deps.reset_state(seed=False)


# --------------------------------------------------------------------------- rows
def test_inventory_row_includes_price_and_value(world):
    row = _ok(world.client.get(f"/inventory/{world.s1}/{world.p1}"))
    assert set(row) == ROW_KEYS
    assert (row["on_hand"], row["price_cents"], row["value_cents"], row["version"], row["below_reorder"]) == (20, 1000, 20_000, 1, False)
    assert (row["store_code"], row["sku"], row["name"], row["reorder_point"]) == ("S1", "SKU-1", "Widget", 5)
    missing = world.client.get(f"/inventory/{world.s3}/{world.p1}")
    assert missing.status_code == 404 and missing.json() == {"detail": "no inventory record", "code": "not_found"}
    with pytest.raises(ServiceError) as exc:
        inventory.get_inventory_row(world.conn, world.s3, world.p1)
    assert exc.value.code == "not_found"


def test_list_inventory_filters_paging_and_math(world):
    page = _ok(world.client.get("/inventory"))
    assert (page["total"], page["limit"], page["offset"]) == (3, 50, 0)
    assert [(r["store_id"], r["product_id"]) for r in page["items"]] == [(world.s1, world.p1), (world.s1, world.p2), (world.s2, world.p1)]
    assert [r["value_cents"] for r in page["items"]] == [20_000, 7_500, 7_000]
    assert all(set(r) == ROW_KEYS for r in page["items"])
    store = _ok(world.client.get("/inventory", params={"store_id": world.s2}))
    assert store["total"] == 1 and store["items"][0]["value_cents"] == 7_000
    assert _ok(world.client.get("/inventory", params={"low_stock": "true"}))["total"] == 0
    _ok(world.client.post(f"/inventory/{world.s1}/{world.p2}/adjust", json={"delta": -1, "reason": "adjustment"}))
    low = _ok(world.client.get("/inventory", params={"low_stock": "true"}))
    assert low["total"] == 1 and low["items"][0]["sku"] == "SKU-2" and low["items"][0]["below_reorder"] is True
    assert low["items"][0]["value_cents"] == 5_000 and low["items"][0]["version"] == 2
    paged = _ok(world.client.get("/inventory", params={"limit": 1, "offset": 2}))
    assert paged["total"] == 3 and [r["store_id"] for r in paged["items"]] == [world.s2] and paged["limit"] == 1
    for params in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"q": "x" * 61}, {"store_id": "abc"}):
        assert world.client.get("/inventory", params=params).status_code == 422, params
    assert _ok(world.client.get("/inventory", params={"limit": 200}))["limit"] == 200


def test_list_inventory_search_matches_sku_or_name_and_escapes_wildcards(world):
    pct = _product(world.conn, "SKU-PCT", "Bulb 100%", 500)
    plain = _product(world.conn, "SKU-1000", "Bulb 1000", 500)
    for pid in (pct, plain):
        _ok(world.client.post(f"/inventory/{world.s1}/{pid}/adjust", json={"delta": 1, "reason": "receipt"}))
    by_name = _ok(world.client.get("/inventory", params={"q": "100%"}))
    assert by_name["total"] == 1 and [r["sku"] for r in by_name["items"]] == ["SKU-PCT"]  # % is literal, not a wildcard
    by_sku = _ok(world.client.get("/inventory", params={"q": "sku-1000"}))  # case-insensitive, matches the sku
    assert [r["sku"] for r in by_sku["items"]] == ["SKU-1000"]
    assert _ok(world.client.get("/inventory", params={"q": "bulb"}))["total"] == 2
    assert _ok(world.client.get("/inventory", params={"q": "_"}))["total"] == 0  # _ is literal too
    assert _ok(world.client.get("/inventory", params={"q": "widget", "store_id": world.s2}))["total"] == 1
    items, total = inventory.list_inventory(world.conn, None, False, 50, 0, q="widget")
    assert total == 2 and {r["sku"] for r in items} == {"SKU-1"}
    low, low_total = inventory.list_inventory(world.conn, world.s1, True, 10, 0)  # the two bulbs: 1 on hand <= reorder point 5
    assert low_total == 2 and {r["sku"] for r in low} == {"SKU-PCT", "SKU-1000"}
    assert inventory.list_inventory(world.conn, world.s3, False, 10, 0) == ([], 0)


def test_adjust_error_codes_and_ledger_balance(world):
    url = f"/inventory/{world.s1}/{world.p1}/adjust"
    stale = world.client.post(url, json={"delta": 1, "reason": "receipt", "expected_version": 7})
    assert stale.status_code == 409 and stale.json()["code"] == "version_conflict"
    assert stale.json()["detail"] == "version conflict: expected 7, current 1"
    short = world.client.post(url, json={"delta": -21, "reason": "adjustment"})
    assert short.status_code == 409 and short.json()["code"] == "insufficient_stock"
    assert _on_hand(world.conn, world.s1, world.p1) == 20 and _count(world.conn, "stock_movements") == 3
    missing_store = world.client.post(f"/inventory/999/{world.p1}/adjust", json={"delta": 1, "reason": "receipt"})
    assert missing_store.status_code == 404 and missing_store.json() == {"detail": "store 999 not found", "code": "not_found"}
    missing_product = world.client.post(f"/inventory/{world.s1}/999/adjust", json={"delta": 1, "reason": "receipt"})
    assert missing_product.status_code == 404 and missing_product.json()["code"] == "not_found"
    assert world.client.post(url, json={"delta": 0, "reason": "adjustment"}).status_code == 422
    ok = _ok(world.client.post(url, json={"delta": 2, "reason": "receipt", "expected_version": 1}))
    assert (ok["version"], ok["on_hand"], ok["value_cents"]) == (2, 22, 22_000)
    row = world.conn.execute("SELECT balance_after, reason FROM stock_movements ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(row) == (22, "receipt")
    fresh = _ok(world.client.post(f"/inventory/{world.s3}/{world.p3}/adjust", json={"delta": 4, "reason": "receipt", "expected_version": 0}))
    assert (fresh["on_hand"], fresh["version"], fresh["below_reorder"], fresh["value_cents"]) == (4, 1, False, 2_800)


# --------------------------------------------------------------------------- receipts
def test_receipt_is_all_or_nothing(world):
    body = {"reference": "PO-77", "lines": [{"product_id": world.p1, "quantity": 5}, {"product_id": world.p3, "quantity": 2}]}
    r = world.client.post(f"/inventory/{world.s2}/receipts", json=body)
    out = _ok(r, 201)
    assert set(out) == {"reference", "store_id", "lines"} and out["reference"] == "PO-77" and out["store_id"] == world.s2
    assert [(ln["product_id"], ln["on_hand"], ln["value_cents"], ln["version"]) for ln in out["lines"]] == [(world.p1, 12, 12_000, 2), (world.p3, 2, 1_400, 1)]
    assert all(set(ln) == ROW_KEYS for ln in out["lines"])
    moves = world.conn.execute(
        "SELECT store_id, product_id, delta, reason, balance_after FROM stock_movements WHERE reference='receipt:PO-77' ORDER BY id"
    ).fetchall()
    assert [tuple(m) for m in moves] == [(world.s2, world.p1, 5, "receipt", 12), (world.s2, world.p3, 2, "receipt", 2)]
    # second line unknown -> nothing from the first line is kept
    before = _count(world.conn, "stock_movements")
    bad = world.client.post(
        f"/inventory/{world.s2}/receipts",
        json={"reference": "PO-78", "lines": [{"product_id": world.p1, "quantity": 5}, {"product_id": 999, "quantity": 1}]},
    )
    assert bad.status_code == 404 and bad.json() == {"detail": "product 999 not found", "code": "not_found"}
    assert _count(world.conn, "stock_movements") == before
    assert _on_hand(world.conn, world.s2, world.p1) == 12
    assert world.conn.execute("SELECT COUNT(*) FROM stock_movements WHERE reference='receipt:PO-78'").fetchone()[0] == 0
    no_store = world.client.post("/inventory/999/receipts", json=body)
    assert no_store.status_code == 404 and no_store.json() == {"detail": "store 999 not found", "code": "not_found"}
    direct = inventory.receive_stock(world.conn, world.s1, inventory.ReceiptIn(reference="PO-79", lines=[{"product_id": world.p2, "quantity": 1}]))
    assert direct["reference"] == "PO-79" and direct["lines"][0]["on_hand"] == 4


def test_receipt_validation(world):
    url = f"/inventory/{world.s1}/receipts"
    one = [{"product_id": world.p1, "quantity": 1}]
    assert world.client.post(url, json={"reference": "R", "lines": []}).status_code == 422
    assert world.client.post(url, json={"reference": "", "lines": one}).status_code == 422
    assert world.client.post(url, json={"reference": "x" * 65, "lines": one}).status_code == 422
    assert world.client.post(url, json={"reference": "R", "lines": [{"product_id": world.p1, "quantity": 0}]}).status_code == 422
    assert world.client.post(url, json={"reference": "R", "lines": [{"product_id": world.p1, "quantity": -2}]}).status_code == 422
    dup = [{"product_id": world.p1, "quantity": 1}, {"product_id": world.p1, "quantity": 2}]
    assert world.client.post(url, json={"reference": "R", "lines": dup}).status_code == 422
    too_many = [{"product_id": world.p1 + i, "quantity": 1} for i in range(51)]
    assert world.client.post(url, json={"reference": "R", "lines": too_many}).status_code == 422
    assert world.client.post(url, json={"lines": one}).status_code == 422
    assert _count(world.conn, "stock_movements") == 3
    with pytest.raises(ValidationError):
        inventory.ReceiptIn(reference="R", lines=dup)
    assert _ok(world.client.post(url, json={"reference": "x" * 64, "lines": one}), 201)["reference"] == "x" * 64


# --------------------------------------------------------------------------- CSV export
def test_export_inventory_csv(world, monkeypatch):
    _ok(world.client.post(f"/inventory/{world.s1}/{world.p3}/adjust", json={"delta": 1, "reason": "receipt"}))
    r = world.client.get("/inventory/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert re.fullmatch(r'attachment; filename="inventory-\d{8}\.csv"', r.headers["content-disposition"])
    assert r.headers["x-row-count"] == "4" and "x-truncated" not in r.headers
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == CSV_HEADER and len(rows) == 5
    assert [(int(x[0]), int(x[2])) for x in rows[1:]] == [(world.s1, world.p1), (world.s1, world.p2), (world.s1, world.p3), (world.s2, world.p1)]
    by_key = {(x[1], x[3]): x for x in rows[1:]}  # SKU-1 is stocked in two stores, so key by (store_code, sku)
    assert by_key[("S1", "SKU-3")][4] == "'=HYPERLINK(\"x\")"  # formula-injection guard
    assert by_key[("S1", "SKU-3")][8] == "true" and by_key[("S1", "SKU-1")][8] == "false"
    assert by_key[("S1", "SKU-1")][9:] == ["1000", "20000"] and by_key[("S2", "SKU-1")][9:] == ["1000", "7000"]
    low = world.client.get("/inventory/export.csv", params={"low_stock": "true"})
    assert low.status_code == 200 and low.headers["x-row-count"] == "1" and "SKU-3" in low.text
    searched = world.client.get("/inventory/export.csv", params={"store_id": world.s2, "q": "widget"})
    assert searched.headers["x-row-count"] == "1" and list(csv.reader(io.StringIO(searched.text)))[1][3] == "SKU-1"
    assert world.client.get("/inventory/export.csv", params={"q": "x" * 61}).status_code == 422
    assert world.client.get("/inventory/export.csv", params={"q": "nothing-like-this"}).headers["x-row-count"] == "0"
    monkeypatch.setattr(common, "CSV_ROW_CAP", 3)
    capped = world.client.get("/inventory/export.csv")
    assert capped.headers["x-truncated"] == "true" and capped.headers["x-row-count"] == "3"
    assert len(capped.text.splitlines()) == 4
    text, count, truncated = inventory.export_inventory_csv(world.conn, world.s1, False)
    assert (count, truncated) == (3, False) and text.count("\n") == 4 and text.startswith(",".join(CSV_HEADER) + "\n")
    text, count, truncated = inventory.export_inventory_csv(world.conn, None, False, q="widget")
    assert (count, truncated) == (2, False)


# --------------------------------------------------------------------------- documentation & bridge table
def test_router_documents_every_operation_and_orders_literal_paths_first(world):
    routes = [r for r in router.routes if isinstance(r, APIRoute)]
    paths = [r.path for r in routes]
    assert paths.index("/inventory/{store_id}/receipts") < paths.index("/inventory/{store_id}/{product_id}")
    assert paths.index("/inventory/export.csv") < paths.index("/inventory/{store_id}/{product_id}")
    assert paths.index("/movements/export.csv") < paths.index("/transfers/{transfer_id}")
    for r in routes:
        assert r.summary, r.path
        assert r.tags == ["inventory"], r.path
        if not r.path.endswith(".csv"):
            assert r.response_model is not None, r.path
    spec = _ok(world.client.get("/openapi.json"))
    csv_op = spec["paths"]["/inventory/export.csv"]["get"]
    assert "text/csv" in csv_op["responses"]["200"]["content"]
    assert "text/csv" in spec["paths"]["/movements/export.csv"]["get"]["responses"]["200"]["content"]
    post_transfer = spec["paths"]["/transfers"]["post"]
    assert "Idempotent-Replayed" in post_transfer["responses"]["201"]["headers"]
    assert "Idempotent-Replayed" in post_transfer["responses"]["200"]["headers"]
    assert {"200", "201", "404", "409", "422"} <= set(post_transfer["responses"])
    assert any(p["name"] == "Idempotency-Key" and p["in"] == "header" for p in post_transfer["parameters"])
    receipts = spec["paths"]["/inventory/{store_id}/receipts"]["post"]
    assert {"201", "404", "422"} <= set(receipts["responses"])
    adjust = spec["paths"]["/inventory/{store_id}/{product_id}/adjust"]["post"]
    assert {"200", "404", "409", "422"} <= set(adjust["responses"])
    schemas_ = spec["components"]["schemas"]
    assert {"id", "from", "to", "from_store_id", "to_store_id", "idempotency_key", "created_at"} <= set(schemas_["TransferOut"]["properties"])
    assert "request_hash" not in schemas_["TransferOut"]["properties"]
    assert "balance_after" in schemas_["Movement"]["properties"]
    assert {"price_cents", "value_cents"} <= set(schemas_["InventoryRowOut"]["properties"])
    assert set(schemas_["MovementFeed"]["properties"]) == {"items", "limit", "next_before_id"}
    operation_ids = [op["operationId"] for item in spec["paths"].values() for op in item.values()]
    assert len(operation_ids) == len(set(operation_ids)) == 11


def test_bridge_routes_mirror_the_router(world):
    routes = [r for r in router.routes if isinstance(r, APIRoute)]
    expected = {(m, re.sub(r"\{\w+\}", "1", r.path)) for r in routes for m in r.methods}
    assert len(inventory.BRIDGE_ROUTES) == len(expected) == 11
    for method, sample in expected:
        matching = [pattern for m, pattern, _ in inventory.BRIDGE_ROUTES if m == method and re.fullmatch(pattern, sample)]
        assert len(matching) == 1, (method, sample, matching)
    for method, pattern, handler in inventory.BRIDGE_ROUTES:
        assert pattern.startswith("^") and pattern.endswith("$") and callable(handler), pattern
        sample = re.sub(r"\(\?P<\w+>\\d\+\)", "1", pattern)[1:-1].replace("\\.", ".")
        assert (method, sample) in expected, (method, pattern)


def test_bridge_inventory_handlers_enforce_router_bounds(world):
    _, list_handler = _bridge("GET", "/inventory")
    status, page = list_handler(BridgeCall(conn=world.conn, query={"store_id": str(world.s1), "low_stock": "false", "limit": "200"}))
    assert status == 200 and page["total"] == 2 and page["limit"] == 200 and page["offset"] == 0 and page["items"][0]["value_cents"] == 20_000
    status, found = list_handler(BridgeCall(conn=world.conn, query={"q": "gadget"}))
    assert [r["sku"] for r in found["items"]] == ["SKU-2"]
    status, low = list_handler(BridgeCall(conn=world.conn, query={"low_stock": "1"}))
    assert low["items"] == []
    for query in ({"limit": "0"}, {"limit": "201"}, {"offset": "-1"}, {"q": "x" * 61}, {"store_id": "abc"}):
        with pytest.raises(ServiceError) as exc:
            list_handler(BridgeCall(conn=world.conn, query=query))
        assert (exc.value.status, exc.value.code) == (422, "validation_error"), query

    _, export = _bridge("GET", "/inventory/export.csv")
    call = BridgeCall(conn=world.conn, query={"low_stock": "true"})
    status, text = export(call)
    assert status == 200 and text == ",".join(CSV_HEADER) + "\n" and call.out_headers["X-Row-Count"] == "0"
    assert call.out_headers["Content-Type"] == "text/csv; charset=utf-8" and "X-Truncated" not in call.out_headers
    assert re.fullmatch(r'attachment; filename="inventory-\d{8}\.csv"', call.out_headers["Content-Disposition"])
    with pytest.raises(ServiceError):
        export(BridgeCall(conn=world.conn, query={"q": "x" * 61}))

    pattern, receipts = _bridge("POST", f"/inventory/{world.s1}/receipts")
    params = _params(pattern, f"/inventory/{world.s1}/receipts")
    status, out = receipts(BridgeCall(conn=world.conn, params=params, body={"reference": "B-1", "lines": [{"product_id": world.p2, "quantity": 4}]}))
    assert status == 201 and out["lines"][0]["on_hand"] == 7 and out["reference"] == "B-1"
    with pytest.raises(ValidationError):
        receipts(BridgeCall(conn=world.conn, params=params, body={"reference": "B-2", "lines": []}))
    with pytest.raises(ServiceError) as exc:
        receipts(BridgeCall(conn=world.conn, params={"store_id": 999}, body={"reference": "B-3", "lines": [{"product_id": world.p2, "quantity": 1}]}))
    assert exc.value.status == 404

    pattern, get_row = _bridge("GET", f"/inventory/{world.s1}/{world.p2}")
    status, row = get_row(BridgeCall(conn=world.conn, params=_params(pattern, f"/inventory/{world.s1}/{world.p2}")))
    assert status == 200 and row["on_hand"] == 7 and row["value_cents"] == 17_500 and row["version"] == 2

    pattern, adjust = _bridge("POST", f"/inventory/{world.s1}/{world.p2}/adjust")
    params = _params(pattern, f"/inventory/{world.s1}/{world.p2}/adjust")
    status, row = adjust(BridgeCall(conn=world.conn, params=params, body={"delta": -1, "reason": "adjustment", "expected_version": 2}))
    assert status == 200 and row["on_hand"] == 6 and row["version"] == 3
    with pytest.raises(ServiceError) as exc:
        adjust(BridgeCall(conn=world.conn, params=params, body={"delta": -1, "reason": "adjustment", "expected_version": 2}))
    assert (exc.value.status, exc.value.code) == (409, "version_conflict")
    with pytest.raises(ValidationError):
        adjust(BridgeCall(conn=world.conn, params=params, body={"delta": 0, "reason": "adjustment"}))
