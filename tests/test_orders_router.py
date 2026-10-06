"""Orders HTTP surface on a private FastAPI app: the v0.2 behaviour the v0.1 application does not have.

Covered here: ``updated_at`` on every order, ``request_hash`` never leaking, inactive products → 409
``product_inactive``, cancel/fulfil idempotency with timestamps, typed pages served without an N+1
query, the CSV export (headers, filters, formula guard, row cap), the OpenAPI documentation and the
bridge route table.  Idempotency-key semantics live in ``tests/test_idempotency.py``; the v0.1 flows
stay in ``tests/test_orders.py`` against the assembled application.
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import common, db, deps, ledger, orders
from app.common import BridgeCall, ServiceError
from app.routers import orders as orders_router

OLD = "2020-01-01T00:00:00.000Z"
ORDER_KEYS = {"id", "store_id", "status", "total_cents", "idempotency_key", "created_at", "updated_at", "lines"}
LINE_KEYS = {"product_id", "sku", "quantity", "unit_price_cents", "line_total_cents"}
CSV_COLUMNS = [
    "order_id", "store_id", "status", "created_at", "updated_at", "idempotency_key",
    "product_id", "sku", "quantity", "unit_price_cents", "line_total_cents", "order_total_cents",
]


@dataclass
class Env:
    """A private app over a temporary database plus a direct connection for raw SQL assertions."""

    client: TestClient
    conn: sqlite3.Connection
    path: str
    s1: int = 1
    s2: int = 2
    p1: int = 1
    p2: int = 2
    p3: int = 3


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(orders_router.router)
    app.add_exception_handler(ServiceError, deps.service_error_handler)
    return app


def _seed(conn: sqlite3.Connection) -> None:
    """Two stores, three products (the third has a sku starting with '-'), opening stock S1: 20/3/10, S2: 10 of p1."""
    conn.execute("INSERT INTO stores (code, name, region) VALUES ('S1', 'Store One', 'South'), ('S2', 'Store Two', 'West')")
    conn.execute(
        "INSERT INTO products (sku, name, category, price_cents, reorder_point) VALUES "
        "('SKU-1', 'Widget', 'Home', 1000, 5), ('SKU-2', 'Gadget', 'Electronics', 2500, 2), ('-DASH-3', 'Dash Thing', 'Misc', 700, 1)"
    )
    with db.transaction(conn):
        ledger.apply_movement(conn, 1, 1, 20, "receipt", "opening")
        ledger.apply_movement(conn, 1, 2, 3, "receipt", "opening")
        ledger.apply_movement(conn, 1, 3, 10, "receipt", "opening")
        ledger.apply_movement(conn, 2, 1, 10, "receipt", "opening")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = str(tmp_path / "orders.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    conn = db.connect(path)
    db.migrate(conn)
    _seed(conn)
    deps.reset_state(seed=False)
    yield Env(client=TestClient(_make_app()), conn=conn, path=path)
    conn.close()
    deps.reset_state(seed=False)


def _order(env: Env, q1: int = 2, q2: int = 1, store_id: int | None = None) -> dict:
    return {
        "store_id": env.s1 if store_id is None else store_id,
        "lines": [{"product_id": env.p1, "quantity": q1}, {"product_id": env.p2, "quantity": q2}],
    }


def _single(env: Env, *, store_id: int | None = None, product_id: int | None = None, qty: int = 1) -> dict:
    return {
        "store_id": env.s1 if store_id is None else store_id,
        "lines": [{"product_id": env.p1 if product_id is None else product_id, "quantity": qty}],
    }


def _place(env: Env, body: dict, key: str | None = None) -> dict:
    headers = {"Idempotency-Key": key} if key is not None else {}
    r = env.client.post("/orders", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _on_hand(conn: sqlite3.Connection, store_id: int, product_id: int) -> int:
    row = conn.execute("SELECT on_hand FROM inventory WHERE store_id=? AND product_id=?", (store_id, product_id)).fetchone()
    return row[0] if row else 0


def _count(conn: sqlite3.Connection, table: str, where: str = "1=1", params: tuple = ()) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]


def _rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


# --------------------------------------------------------------------------- place / get
def test_place_order_returns_201_with_updated_at_and_server_side_prices(env):
    r = env.client.post("/orders", json=_order(env))
    assert r.status_code == 201
    assert r.headers["Idempotent-Replayed"] == "false"
    o = r.json()
    assert set(o) == ORDER_KEYS
    assert o["status"] == "placed" and o["total_cents"] == 2 * 1000 + 1 * 2500
    assert o["idempotency_key"] is None
    assert o["updated_at"] == o["created_at"]
    assert [set(line) for line in o["lines"]] == [LINE_KEYS, LINE_KEYS]
    assert [(ln["product_id"], ln["sku"], ln["quantity"], ln["unit_price_cents"], ln["line_total_cents"]) for ln in o["lines"]] == [
        (1, "SKU-1", 2, 1000, 2000),
        (2, "SKU-2", 1, 2500, 2500),
    ]
    assert _on_hand(env.conn, 1, 1) == 18 and _on_hand(env.conn, 1, 2) == 2
    sales = env.conn.execute(
        "SELECT reason, delta, balance_after FROM stock_movements WHERE reference = ? ORDER BY product_id", (f"order:{o['id']}",)
    ).fetchall()
    assert [tuple(m) for m in sales] == [("sale", -2, 18), ("sale", -1, 2)]


def test_get_order_includes_updated_at_and_never_request_hash(env):
    created = _place(env, _order(env), key="k-get")
    stored = env.conn.execute("SELECT request_hash, updated_at FROM orders WHERE id=?", (created["id"],)).fetchone()
    assert stored["request_hash"]  # the fingerprint is persisted ...
    r = env.client.get(f"/orders/{created['id']}")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == ORDER_KEYS  # ... but never exposed
    assert body["updated_at"] == stored["updated_at"]
    assert body["idempotency_key"] == "k-get"
    assert body == created
    missing = env.client.get("/orders/999")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "order 999 not found", "code": "not_found"}


def test_inactive_product_is_409_product_inactive_and_nothing_is_written(env):
    env.conn.execute("UPDATE products SET active = 0 WHERE id = ?", (env.p2,))
    before = _count(env.conn, "stock_movements")
    r = env.client.post("/orders", json=_order(env))
    assert r.status_code == 409
    assert r.json() == {"detail": f"product {env.p2} is inactive", "code": "product_inactive"}
    assert _on_hand(env.conn, 1, 1) == 20  # the active line was not reserved either (all-or-nothing)
    assert _count(env.conn, "orders") == 0 and _count(env.conn, "stock_movements") == before


def test_unknown_store_or_product_is_404_not_found(env):
    r = env.client.post("/orders", json={"store_id": 999, "lines": [{"product_id": env.p1, "quantity": 1}]})
    assert (r.status_code, r.json()) == (404, {"detail": "store 999 not found", "code": "not_found"})
    r = env.client.post("/orders", json={"store_id": env.s1, "lines": [{"product_id": 999, "quantity": 1}]})
    assert (r.status_code, r.json()) == (404, {"detail": "product 999 not found", "code": "not_found"})
    assert _count(env.conn, "orders") == 0 and _on_hand(env.conn, 1, 1) == 20


def test_insufficient_stock_is_409_with_code_and_rolls_back(env):
    before = _count(env.conn, "stock_movements")
    r = env.client.post("/orders", json=_order(env, q1=1, q2=99))
    assert r.status_code == 409
    assert r.json() == {"detail": "insufficient stock for product 2 at store 1: have 3, need 99", "code": "insufficient_stock"}
    assert _on_hand(env.conn, 1, 1) == 20 and _on_hand(env.conn, 1, 2) == 3
    assert _count(env.conn, "orders") == 0 and _count(env.conn, "stock_movements") == before


# --------------------------------------------------------------------------- cancel / fulfil
def test_cancel_restocks_sets_updated_at_and_is_idempotent(env):
    oid = _place(env, _order(env))["id"]
    env.conn.execute("UPDATE orders SET updated_at = ? WHERE id = ?", (OLD, oid))
    before = _count(env.conn, "stock_movements")
    c1 = env.client.post(f"/orders/{oid}/cancel")
    assert c1.status_code == 200
    body = c1.json()
    assert set(body) == ORDER_KEYS and body["status"] == "cancelled"
    assert body["updated_at"] > OLD and body["updated_at"] >= body["created_at"]
    assert _on_hand(env.conn, 1, 1) == 20 and _on_hand(env.conn, 1, 2) == 3
    returns = env.conn.execute(
        "SELECT product_id, delta, reason, balance_after FROM stock_movements WHERE reference = ? ORDER BY product_id", (f"cancel:{oid}",)
    ).fetchall()
    assert [tuple(m) for m in returns] == [(1, 2, "return", 20), (2, 1, "return", 3)]
    assert _count(env.conn, "stock_movements") == before + 2
    c2 = env.client.post(f"/orders/{oid}/cancel")
    assert c2.status_code == 200 and c2.json() == body  # replayed as is: same updated_at, nothing written
    assert _count(env.conn, "stock_movements") == before + 2
    f = env.client.post(f"/orders/{oid}/fulfil")
    assert (f.status_code, f.json()) == (409, {"detail": "cancelled orders cannot be fulfilled", "code": "invalid_state"})
    assert env.client.get(f"/orders/{oid}").json() == body


def test_fulfil_sets_updated_at_and_is_idempotent(env):
    oid = _place(env, _order(env))["id"]
    env.conn.execute("UPDATE orders SET updated_at = ? WHERE id = ?", (OLD, oid))
    before = _count(env.conn, "stock_movements")
    f1 = env.client.post(f"/orders/{oid}/fulfil")
    assert f1.status_code == 200
    body = f1.json()
    assert body["status"] == "fulfilled" and body["updated_at"] > OLD
    assert _on_hand(env.conn, 1, 1) == 18 and _count(env.conn, "stock_movements") == before  # reserved stock ships: no new movement
    f2 = env.client.post(f"/orders/{oid}/fulfil")
    assert f2.status_code == 200 and f2.json() == body
    c = env.client.post(f"/orders/{oid}/cancel")
    assert (c.status_code, c.json()) == (409, {"detail": "fulfilled orders cannot be cancelled", "code": "invalid_state"})
    assert _on_hand(env.conn, 1, 1) == 18 and _count(env.conn, "stock_movements") == before


def test_cancel_and_fulfil_unknown_order_are_404(env):
    for action in ("cancel", "fulfil"):
        r = env.client.post(f"/orders/4242/{action}")
        assert (r.status_code, r.json()) == (404, {"detail": "order 4242 not found", "code": "not_found"}), action


# --------------------------------------------------------------------------- list
def test_list_orders_filters_paginates_and_sorts_newest_first(env):
    a = _place(env, _single(env))["id"]
    b = _place(env, _single(env))["id"]
    c = _place(env, _single(env, store_id=env.s2))["id"]
    d = _place(env, _order(env))["id"]
    env.client.post(f"/orders/{a}/cancel")
    env.client.post(f"/orders/{b}/fulfil")

    page = env.client.get("/orders").json()
    assert set(page) == {"items", "total", "limit", "offset"}
    assert (page["total"], page["limit"], page["offset"]) == (4, 20, 0)
    assert [o["id"] for o in page["items"]] == [d, c, b, a]
    assert all(set(o) == ORDER_KEYS for o in page["items"])
    assert [len(o["lines"]) for o in page["items"]] == [2, 1, 1, 1]
    assert [o["status"] for o in page["items"]] == ["placed", "placed", "fulfilled", "cancelled"]
    assert all(o["updated_at"] for o in page["items"])

    assert [o["id"] for o in env.client.get("/orders", params={"store_id": env.s2}).json()["items"]] == [c]
    assert [o["id"] for o in env.client.get("/orders", params={"status": "cancelled"}).json()["items"]] == [a]
    both = env.client.get("/orders", params={"store_id": env.s1, "status": "placed"}).json()
    assert both["total"] == 1 and both["items"][0]["id"] == d
    first = env.client.get("/orders", params={"limit": 2, "offset": 0}).json()
    second = env.client.get("/orders", params={"limit": 2, "offset": 2}).json()
    assert [o["id"] for o in first["items"]] == [d, c] and [o["id"] for o in second["items"]] == [b, a]
    assert (first["total"], first["limit"], second["offset"]) == (4, 2, 2)
    assert env.client.get("/orders", params={"limit": 3, "offset": 10}).json()["items"] == []
    for bad in ({"status": "bogus"}, {"limit": 0}, {"limit": 101}, {"offset": -1}, {"store_id": "x"}):
        assert env.client.get("/orders", params=bad).status_code == 422, bad
    env.conn.execute("UPDATE orders SET created_at = ? WHERE id = ?", (OLD, d))  # a back-dated order is no longer the newest
    assert [o["id"] for o in env.client.get("/orders").json()["items"]] == [c, b, a, d]


def test_list_orders_has_no_n_plus_one_query(env):
    ids = [_place(env, _single(env))["id"] for _ in range(10)]
    probe = db.connect(env.path)
    statements: list[str] = []
    probe.set_trace_callback(statements.append)
    items, total = orders.list_orders(probe, None, None, 10, 0)
    probe.set_trace_callback(None)
    probe.close()
    assert total == 10 and [o["id"] for o in items] == ids[::-1]
    assert all(len(o["lines"]) == 1 and o["lines"][0]["sku"] == "SKU-1" for o in items)
    assert len(statements) <= 3, statements
    assert env.client.get("/orders", params={"limit": 10}).json()["items"] == items


# --------------------------------------------------------------------------- CSV export
def test_export_csv_rows_headers_and_formula_guard(env):
    first = _place(env, _order(env), key="=SUM(A1:A9)")
    second = _place(env, _single(env, product_id=env.p3, qty=2))
    third = _place(env, _single(env, store_id=env.s2))
    env.conn.execute("UPDATE orders SET created_at = ?, updated_at = ? WHERE id = ?", (OLD, OLD, second["id"]))  # back-dated → sorts last
    assert env.client.post(f"/orders/{second['id']}/cancel").status_code == 200

    r = env.client.get("/orders/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert r.headers["content-disposition"] == f'attachment; filename="{common.csv_filename("orders")}"'
    assert r.headers["x-row-count"] == "4"
    assert "x-truncated" not in r.headers
    rows = _rows(r.text)
    assert rows[0] == CSV_COLUMNS == orders.ORDER_CSV_COLUMNS
    assert len(rows) == 5
    # newest order first (created_at, then id), lines by product id
    assert [row[0] for row in rows[1:]] == [str(third["id"]), str(first["id"]), str(first["id"]), str(second["id"])]
    cells = [dict(zip(rows[0], row, strict=True)) for row in rows[1:]]
    assert cells[0]["idempotency_key"] == ""  # NULL → empty cell
    assert cells[0]["store_id"] == str(env.s2)
    assert cells[1]["idempotency_key"] == cells[2]["idempotency_key"] == "'=SUM(A1:A9)"  # formula guard on client-controlled text
    assert (cells[1]["product_id"], cells[2]["product_id"]) == ("1", "2")
    assert all(c["order_total_cents"] == str(first["total_cents"]) for c in cells[1:3])
    assert cells[3]["status"] == "cancelled" and cells[3]["sku"] == "'-DASH-3"  # formula guard on a sku starting with '-'
    assert cells[3]["created_at"] == OLD and cells[3]["updated_at"] > OLD
    assert all(int(c["line_total_cents"]) == int(c["quantity"]) * int(c["unit_price_cents"]) for c in cells)
    assert all(c["created_at"] and c["updated_at"] >= c["created_at"] for c in cells)

    cancelled = env.client.get("/orders/export.csv", params={"status": "cancelled"})
    assert cancelled.headers["x-row-count"] == "1" and _rows(cancelled.text)[1][0] == str(second["id"])
    s2 = env.client.get("/orders/export.csv", params={"store_id": env.s2})
    assert s2.headers["x-row-count"] == "1" and _rows(s2.text)[1][1] == str(env.s2)
    empty = env.client.get("/orders/export.csv", params={"status": "fulfilled"})
    assert empty.status_code == 200 and empty.headers["x-row-count"] == "0" and _rows(empty.text) == [CSV_COLUMNS]
    assert env.client.get("/orders/export.csv", params={"status": "bogus"}).status_code == 422
    assert env.client.get("/orders/export.csv", params={"store_id": "x"}).status_code == 422


def test_export_csv_cap_marks_truncation(env, monkeypatch):
    for _ in range(3):
        _place(env, _single(env))
    monkeypatch.setattr(common, "CSV_ROW_CAP", 2)
    r = env.client.get("/orders/export.csv")
    assert r.status_code == 200
    assert (r.headers["x-row-count"], r.headers.get("x-truncated")) == ("2", "true")
    assert len(_rows(r.text)) == 3
    text, count, truncated = orders.export_orders_csv(env.conn, None, None)
    assert (count, truncated, len(_rows(text))) == (2, True, 3)
    monkeypatch.setattr(common, "CSV_ROW_CAP", 3)
    assert orders.export_orders_csv(env.conn, None, None)[1:] == (3, False)


# --------------------------------------------------------------------------- documentation and route tables
def test_openapi_documents_every_operation(env):
    spec = env.client.get("/openapi.json").json()
    paths = spec["paths"]
    assert set(paths) == {"/orders", "/orders/export.csv", "/orders/{order_id}", "/orders/{order_id}/cancel", "/orders/{order_id}/fulfil"}
    op_ids = []
    for path, ops in paths.items():
        for method, op in ops.items():
            assert op.get("summary"), (path, method)
            assert op["tags"] == ["orders"], (path, method)
            op_ids.append(op["operationId"])
    assert len(op_ids) == len(set(op_ids)) == 6

    post = paths["/orders"]["post"]["responses"]
    assert {"200", "201", "404", "409", "422"} <= set(post)
    for code in ("200", "201"):
        assert "Idempotent-Replayed" in post[code]["headers"], code
        assert post[code]["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/OrderOut"
    for code in ("404", "409", "422"):
        assert post[code]["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/ErrorBody"
    assert paths["/orders"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/OrderPage"
    export = paths["/orders/export.csv"]["get"]["responses"]["200"]
    assert export["content"] == {"text/csv": {"schema": {"type": "string"}}}
    for path in ("/orders/{order_id}", "/orders/{order_id}/cancel", "/orders/{order_id}/fulfil"):
        op = paths[path].get("get") or paths[path]["post"]
        assert op["responses"]["404"]["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/ErrorBody", path
        assert op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/OrderOut", path
    assert "409" in paths["/orders/{order_id}/cancel"]["post"]["responses"]
    assert "409" in paths["/orders/{order_id}/fulfil"]["post"]["responses"]

    models = spec["components"]["schemas"]
    assert models["OrderPage"]["properties"]["items"]["items"]["$ref"] == "#/components/schemas/OrderOut"
    assert set(models["OrderPage"]["properties"]) == {"items", "total", "limit", "offset"}
    assert "updated_at" in models["OrderOut"]["properties"]
    assert "request_hash" not in models["OrderOut"]["properties"]


def test_export_route_is_declared_before_the_order_id_route():
    paths = [getattr(route, "path", None) for route in orders_router.router.routes]
    assert paths.index("/orders/export.csv") < paths.index("/orders/{order_id}")
    assert orders_router.router.tags == ["orders"]


def _bridge_table() -> list[tuple[str, re.Pattern[str], object]]:
    return [(method, re.compile(pattern), handler) for method, pattern, handler in orders.BRIDGE_ROUTES]


def _find(method: str, path: str) -> tuple[int, object, dict[str, int]]:
    for index, (m, pattern, handler) in enumerate(_bridge_table()):
        match = pattern.match(path)
        if m == method and match:
            return index, handler, {k: int(v) for k, v in match.groupdict().items()}
    raise AssertionError(f"no bridge route for {method} {path}")


def test_bridge_routes_mirror_the_router_and_its_bounds(env):
    assert len(_bridge_table()) == 6
    export_index, export_handler, _ = _find("GET", "/orders/export.csv")
    get_index, get_handler, params = _find("GET", "/orders/7")
    assert export_index < get_index and export_handler is not get_handler and params == {"order_id": 7}
    assert _find("POST", "/orders")[1] is not _find("GET", "/orders")[1]
    assert _find("POST", "/orders/7/cancel")[1] is not _find("POST", "/orders/7/fulfil")[1]
    with pytest.raises(AssertionError):
        _find("GET", "/orders/abc")
    list_handler = _find("GET", "/orders")[1]
    for bad in ({"limit": "0"}, {"limit": "101"}, {"offset": "-1"}, {"status": "bogus"}, {"store_id": "x"}):
        with pytest.raises(ServiceError) as ei:
            list_handler(BridgeCall(conn=env.conn, query=bad))
        assert (ei.value.status, ei.value.code) == (422, "validation_error"), bad
    with pytest.raises(ServiceError) as ei:
        _find("GET", "/orders/export.csv")[1](BridgeCall(conn=env.conn, query={"status": "bogus"}))
    assert (ei.value.status, ei.value.code) == (422, "validation_error")


def test_bridge_handlers_execute_the_order_flow(env):
    def call(method: str, path: str, *, query: dict | None = None, body: dict | None = None, headers: dict | None = None):
        _, handler, params = _find(method, path)
        c = BridgeCall(conn=env.conn, params=params, query=query or {}, body={} if body is None else body, headers=headers or {})
        status, out = handler(c)
        return status, out, c.out_headers

    status, order, hdrs = call("POST", "/orders", body=_order(env), headers={"idempotency-key": "bridge-1"})
    assert status == 201 and hdrs == {"Idempotent-Replayed": "false"} and set(order) == ORDER_KEYS
    status, again, hdrs = call("POST", "/orders", body=_order(env), headers={"idempotency-key": "bridge-1"})
    assert (status, again["id"], hdrs["Idempotent-Replayed"]) == (200, order["id"], "true")
    assert _on_hand(env.conn, 1, 1) == 18
    assert call("GET", f"/orders/{order['id']}")[:2] == (200, order)
    status, page, _ = call("GET", "/orders", query={"limit": "5"})
    assert status == 200 and set(page) == {"items", "total", "limit", "offset"}
    assert (page["total"], page["limit"], page["offset"], page["items"][0]["id"]) == (1, 5, 0, order["id"])
    status, text, hdrs = call("GET", "/orders/export.csv", query={"store_id": "1"})
    assert status == 200 and isinstance(text, str)
    assert hdrs["Content-Type"] == "text/csv; charset=utf-8" and hdrs["X-Row-Count"] == "2" and "X-Truncated" not in hdrs
    assert hdrs["Content-Disposition"] == f'attachment; filename="{common.csv_filename("orders")}"'
    assert _rows(text)[0] == CSV_COLUMNS and len(_rows(text)) == 3
    status, cancelled, _ = call("POST", f"/orders/{order['id']}/cancel")
    assert (status, cancelled["status"]) == (200, "cancelled") and _on_hand(env.conn, 1, 1) == 20
    with pytest.raises(ServiceError) as ei:
        call("POST", f"/orders/{order['id']}/fulfil")
    assert (ei.value.status, ei.value.code) == (409, "invalid_state")
    with pytest.raises(ServiceError) as ei:
        call("POST", "/orders", body=_order(env), headers={"idempotency-key": "k" * 65})
    assert (ei.value.status, ei.value.code) == (422, "validation_error")
    with pytest.raises(ServiceError) as ei:
        call("GET", "/orders/999")
    assert (ei.value.status, ei.value.code) == (404, "not_found")
