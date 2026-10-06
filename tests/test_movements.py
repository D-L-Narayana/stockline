"""Movement ledger surface: per-pair history with keyset paging, the cross-store feed and the CSV export."""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import common, db, deps, inventory, schemas
from app.common import BridgeCall, ServiceError
from app.routers.inventory import router

MOVEMENT_KEYS = {"id", "store_id", "product_id", "delta", "reason", "reference", "balance_after", "created_at"}
CSV_HEADER = ["id", "store_id", "product_id", "delta", "reason", "reference", "balance_after", "created_at"]


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


def _ok(resp, status: int = 200) -> dict | list:
    assert resp.status_code == status, f"{resp.request.method} {resp.url} -> {resp.status_code}: {resp.text}"
    return resp.json()


def _adjust(client: TestClient, store_id: int, product_id: int, delta: int, reason: str = "adjustment", reference: str | None = None) -> dict:
    body = {"delta": delta, "reason": reason, "reference": reference}
    return _ok(client.post(f"/inventory/{store_id}/{product_id}/adjust", json=body))


def _bridge(method: str, path: str):
    matches = [(pattern, handler) for m, pattern, handler in inventory.BRIDGE_ROUTES if m == method and re.fullmatch(pattern, path)]
    assert len(matches) == 1, f"expected exactly one bridge route for {method} {path}, found {len(matches)}"
    return matches[0]


@pytest.fixture()
def world(tmp_path, monkeypatch):
    path = str(tmp_path / "movements.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    deps.reset_state(seed=False)
    conn = db.connect(path)
    db.init_schema(conn)
    s1, s2, s3 = (_store(conn, code) for code in ("S1", "S2", "S3"))
    p1 = _product(conn, "SKU-1", "Widget", 1000, 5)
    p2 = _product(conn, "SKU-2", "Gadget", 2500, 2)
    p3 = _product(conn, "SKU-3", "Gizmo", 700, 1)
    # three opening receipts -> movement ids 1..3 in this order
    for store_id, product_id, qty in ((s1, p1, 20), (s1, p2, 3), (s2, p1, 7)):
        inventory.adjust_stock(conn, store_id, product_id, schemas.StockAdjust(delta=qty, reason="receipt", reference="opening-stock"))
    with TestClient(_make_app()) as client:
        yield World(client, conn, s1, s2, s3, p1, p2, p3)
    conn.close()
    deps.reset_state(seed=False)


# --------------------------------------------------------------------------- feed
def test_feed_is_newest_first_with_balance_after(world):
    _adjust(world.client, world.s1, world.p1, -4, "adjustment", "count")
    _adjust(world.client, world.s2, world.p1, 1, "return", "cust-9")
    feed = _ok(world.client.get("/movements"))
    assert set(feed) == {"items", "limit", "next_before_id"}
    assert feed["limit"] == 100 and feed["next_before_id"] is None
    ids = [m["id"] for m in feed["items"]]
    assert len(ids) == 5 and ids == sorted(ids, reverse=True)
    newest, second = feed["items"][0], feed["items"][1]
    assert set(newest) == MOVEMENT_KEYS
    assert (newest["store_id"], newest["product_id"], newest["delta"], newest["reason"], newest["reference"], newest["balance_after"]) == (
        world.s2,
        world.p1,
        1,
        "return",
        "cust-9",
        8,
    )
    assert (second["delta"], second["reason"], second["reference"], second["balance_after"]) == (-4, "adjustment", "count", 16)
    assert feed["items"][-1]["reference"] == "opening-stock" and feed["items"][-1]["balance_after"] == 20
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", m["created_at"]) for m in feed["items"])


def test_feed_filters_by_store_product_and_reason(world):
    _adjust(world.client, world.s1, world.p1, -2)
    xfer = _ok(
        world.client.post("/transfers", json={"from_store_id": world.s1, "to_store_id": world.s2, "product_id": world.p1, "quantity": 3}), 201
    )
    by_store = _ok(world.client.get("/movements", params={"store_id": world.s2}))["items"]
    assert [m["reason"] for m in by_store] == ["transfer_in", "receipt"] and all(m["store_id"] == world.s2 for m in by_store)
    by_product = _ok(world.client.get("/movements", params={"product_id": world.p2}))["items"]
    assert [(m["product_id"], m["delta"]) for m in by_product] == [(world.p2, 3)]
    by_reason = _ok(world.client.get("/movements", params={"reason": "adjustment"}))["items"]
    assert [m["delta"] for m in by_reason] == [-2]
    assert len(_ok(world.client.get("/movements", params={"reason": "receipt"}))["items"]) == 3
    combo = _ok(world.client.get("/movements", params={"store_id": world.s1, "product_id": world.p1, "reason": "transfer_out"}))["items"]
    assert len(combo) == 1 and combo[0]["reference"] == f"transfer:{xfer['id']}" and combo[0]["balance_after"] == 15
    assert _ok(world.client.get("/movements", params={"store_id": world.s3}))["items"] == []
    assert world.client.get("/movements", params={"reason": "bogus"}).status_code == 422
    assert world.client.get("/movements", params={"store_id": "abc"}).status_code == 422


def test_feed_keyset_paging_and_cursor(world):
    for delta in (1, 2, 3, 4):
        _adjust(world.client, world.s1, world.p1, delta)
    # 3 opening receipts + 4 adjustments = 7 movements
    page1 = _ok(world.client.get("/movements", params={"limit": 3}))
    assert len(page1["items"]) == 3 and page1["limit"] == 3
    assert page1["next_before_id"] == page1["items"][-1]["id"]
    page2 = _ok(world.client.get("/movements", params={"limit": 3, "before_id": page1["next_before_id"]}))
    assert len(page2["items"]) == 3 and page2["next_before_id"] == page2["items"][-1]["id"]
    assert all(m["id"] < page1["next_before_id"] for m in page2["items"])
    page3 = _ok(world.client.get("/movements", params={"limit": 3, "before_id": page2["next_before_id"]}))
    assert len(page3["items"]) == 1 and page3["next_before_id"] is None
    seen = [m["id"] for m in page1["items"] + page2["items"] + page3["items"]]
    assert seen == sorted(seen, reverse=True) and len(set(seen)) == 7
    # an exactly full page still hands out a cursor; the page after it is empty and ends the walk
    full = _ok(world.client.get("/movements", params={"limit": 7}))
    assert len(full["items"]) == 7 and full["next_before_id"] == full["items"][-1]["id"]
    assert _ok(world.client.get("/movements", params={"limit": 7, "before_id": full["next_before_id"]})) == {
        "items": [],
        "limit": 7,
        "next_before_id": None,
    }
    direct = inventory.movement_feed(world.conn, store_id=world.s1, product_id=world.p1, limit=2)
    assert [m["delta"] for m in direct["items"]] == [4, 3] and direct["next_before_id"] == direct["items"][-1]["id"]


def test_feed_since_compares_iso_strings(world):
    ids = [m["id"] for m in _ok(world.client.get("/movements"))["items"]]
    assert len(ids) == 3
    oldest = min(ids)
    world.conn.execute("UPDATE stock_movements SET created_at='2020-01-01T00:00:00.000Z' WHERE id=?", (oldest,))
    recent = _ok(world.client.get("/movements", params={"since": "2021-01-01T00:00:00.000Z"}))["items"]
    assert {m["id"] for m in recent} == set(ids) - {oldest}
    everything = _ok(world.client.get("/movements", params={"since": "2019-12-31"}))["items"]
    assert {m["id"] for m in everything} == set(ids)
    assert _ok(world.client.get("/movements", params={"since": "2999-01-01"}))["items"] == []
    assert world.client.get("/movements", params={"since": "x" * 33}).status_code == 422


def test_feed_bounds(world):
    for params in ({"limit": 0}, {"limit": 501}, {"before_id": 0}, {"limit": "abc"}, {"before_id": "abc"}):
        assert world.client.get("/movements", params=params).status_code == 422, params
    assert _ok(world.client.get("/movements", params={"limit": 500}))["limit"] == 500
    assert _ok(world.client.get("/movements", params={"limit": 1}))["limit"] == 1
    # the module functions guard `reason` themselves (callers other than the router/bridge get the same 422)
    with pytest.raises(ServiceError) as exc:
        inventory.movement_feed(world.conn, reason="bogus")
    assert (exc.value.status, exc.value.code) == (422, "validation_error")
    with pytest.raises(ServiceError):
        inventory.export_movements_csv(world.conn, reason="bogus")


# --------------------------------------------------------------------------- per-pair history
def test_pair_movements_support_before_id_and_carry_balance_after(world):
    for delta in (-1, -2, -3):
        _adjust(world.client, world.s1, world.p1, delta)
    url = f"/inventory/{world.s1}/{world.p1}/movements"
    rows = _ok(world.client.get(url))
    assert isinstance(rows, list) and [m["delta"] for m in rows] == [-3, -2, -1, 20]
    assert [m["balance_after"] for m in rows] == [14, 17, 19, 20]
    assert all(set(m) == MOVEMENT_KEYS and m["store_id"] == world.s1 and m["product_id"] == world.p1 for m in rows)
    older = _ok(world.client.get(url, params={"before_id": rows[1]["id"], "limit": 1}))
    assert [m["id"] for m in older] == [rows[2]["id"]]
    assert _ok(world.client.get(url, params={"limit": 2})) == rows[:2]
    for params in ({"limit": 0}, {"limit": 501}, {"before_id": 0}):
        assert world.client.get(url, params=params).status_code == 422, params
    assert _ok(world.client.get(f"/inventory/{world.s3}/{world.p1}/movements")) == []
    direct = inventory.movements(world.conn, world.s1, world.p1, 50)
    assert [m["id"] for m in direct] == [m["id"] for m in rows]
    assert inventory.movements(world.conn, world.s1, world.p1, 50, before_id=rows[-1]["id"]) == []


# --------------------------------------------------------------------------- CSV export
def test_export_movements_csv(world, monkeypatch):
    _adjust(world.client, world.s1, world.p1, -3, "adjustment", "=SUM(A1)")
    r = world.client.get("/movements/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert re.fullmatch(r'attachment; filename="movements-\d{8}\.csv"', r.headers["content-disposition"])
    assert r.headers["x-row-count"] == "4" and "x-truncated" not in r.headers
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == CSV_HEADER and len(rows) == 5
    assert rows[1][3] == "-3" and rows[1][5] == "'=SUM(A1)"  # negative numbers stay numeric; formulas are neutralised
    assert rows[1][6] == "17" and rows[-1][5] == "opening-stock"
    ids = [int(row[0]) for row in rows[1:]]
    assert ids == sorted(ids, reverse=True)
    filtered = world.client.get("/movements/export.csv", params={"store_id": world.s2, "reason": "receipt"})
    assert filtered.status_code == 200 and filtered.headers["x-row-count"] == "1"
    body = list(csv.reader(io.StringIO(filtered.text)))
    assert body[1][1] == str(world.s2) and body[1][4] == "receipt" and body[1][3] == "7"
    assert world.client.get("/movements/export.csv", params={"reason": "nope"}).status_code == 422
    assert world.client.get("/movements/export.csv", params={"since": "2999-01-01"}).headers["x-row-count"] == "0"
    monkeypatch.setattr(common, "CSV_ROW_CAP", 2)
    capped = world.client.get("/movements/export.csv")
    assert capped.headers["x-truncated"] == "true" and capped.headers["x-row-count"] == "2"
    assert len(capped.text.splitlines()) == 3
    text, count, truncated = inventory.export_movements_csv(world.conn, since="2000-01-01")
    assert (count, truncated) == (2, True) and text.startswith("id,store_id,product_id,")
    text, count, truncated = inventory.export_movements_csv(world.conn, product_id=world.p2)
    assert (count, truncated) == (1, False) and text.count("\n") == 2


# --------------------------------------------------------------------------- bridge
def test_bridge_feed_and_export_handlers_match_router_bounds(world, monkeypatch):
    _, feed = _bridge("GET", "/movements")
    status, body = feed(BridgeCall(conn=world.conn, query={"limit": "2", "store_id": str(world.s1)}))
    assert status == 200 and len(body["items"]) == 2 and body["limit"] == 2 and body["next_before_id"] == body["items"][-1]["id"]
    status, body = feed(BridgeCall(conn=world.conn, query={"reason": "receipt", "since": "2000-01-01", "before_id": "2"}))
    assert status == 200 and [m["id"] for m in body["items"]] == [1] and body["limit"] == 100 and body["next_before_id"] is None
    for query in ({"limit": "0"}, {"limit": "501"}, {"reason": "bogus"}, {"before_id": "0"}, {"since": "x" * 33}, {"store_id": "abc"}):
        with pytest.raises(ServiceError) as exc:
            feed(BridgeCall(conn=world.conn, query=query))
        assert (exc.value.status, exc.value.code) == (422, "validation_error"), query

    _, export = _bridge("GET", "/movements/export.csv")
    call = BridgeCall(conn=world.conn, query={"reason": "receipt"})
    status, text = export(call)
    assert status == 200 and isinstance(text, str) and text.startswith(",".join(CSV_HEADER) + "\n")
    assert call.out_headers["Content-Type"] == "text/csv; charset=utf-8"
    assert call.out_headers["X-Row-Count"] == "3" and "X-Truncated" not in call.out_headers
    assert re.fullmatch(r'attachment; filename="movements-\d{8}\.csv"', call.out_headers["Content-Disposition"])
    with pytest.raises(ServiceError):
        export(BridgeCall(conn=world.conn, query={"reason": "nope"}))
    monkeypatch.setattr(common, "CSV_ROW_CAP", 1)
    capped = BridgeCall(conn=world.conn)
    status, text = export(capped)
    assert status == 200 and len(text.splitlines()) == 2
    assert capped.out_headers["X-Truncated"] == "true" and capped.out_headers["X-Row-Count"] == "1"

    pattern, per_pair = _bridge("GET", f"/inventory/{world.s1}/{world.p1}/movements")
    params = {k: int(v) for k, v in re.fullmatch(pattern, f"/inventory/{world.s1}/{world.p1}/movements").groupdict().items()}
    status, rows = per_pair(BridgeCall(conn=world.conn, params=params, query={"limit": "500"}))
    assert status == 200 and [m["delta"] for m in rows] == [20] and rows[0]["balance_after"] == 20
    for query in ({"limit": "0"}, {"limit": "501"}, {"before_id": "0"}):
        with pytest.raises(ServiceError):
            per_pair(BridgeCall(conn=world.conn, params=params, query=query))
