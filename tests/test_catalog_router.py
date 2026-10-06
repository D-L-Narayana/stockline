"""Catalogue lifecycle (v0.2) on a private FastAPI app: ``app/routers/catalog.py`` + ``app/catalog.py`` + ``BRIDGE_ROUTES``.

The v0.1 behaviour (create/list/search) is covered against the assembled application by ``tests/test_catalog.py``.
Everything here is new: ``GET``/``PATCH`` stores, ``PATCH``/``DELETE`` products, ``include_inactive``, ``updated_at``,
the per-product stock view, LIKE wildcard escaping, machine-readable error codes and the bridge handlers — exercised
through the router mounted on its own ``FastAPI()`` plus direct calls into the domain module on the same temporary DB.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import catalog, db, deps, ledger, schemas, service
from app.common import DUPLICATE, NOT_FOUND, VALIDATION, BridgeCall, ServiceError
from app.routers import catalog as catalog_router

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
PRODUCT_KEYS = {"id", "sku", "name", "category", "price_cents", "reorder_point", "active", "updated_at"}

STORE = {"code": "BLR-01", "name": "Bengaluru", "region": "South"}
STORE_2 = {"code": "HYD-01", "name": "Hyderabad", "region": "South"}
PRODUCT = {"sku": "SKU-1", "name": "Widget", "category": "Home", "price_cents": 1000, "reorder_point": 5}


# --------------------------------------------------------------------------- fixtures
@pytest.fixture()
def dbpath(tmp_path, monkeypatch) -> Iterator[str]:
    """Point ``db.DB_PATH`` at a temporary file and give the pool a fresh, unseeded start (and end)."""
    path = str(tmp_path / "catalog.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    deps.reset_state(seed=False)
    yield path
    deps.reset_state(seed=False)


@pytest.fixture()
def api(dbpath) -> Iterator[TestClient]:
    """The catalogue router on a private app with the shared ``ServiceError`` handler."""
    app = FastAPI()
    app.include_router(catalog_router.router)
    app.add_exception_handler(ServiceError, deps.service_error_handler)
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def direct(dbpath) -> Iterator[sqlite3.Connection]:
    """A direct connection to the same temporary database (schema migrated), for raw SQL and domain calls."""
    conn = db.connect(dbpath)
    db.migrate(conn)
    yield conn
    conn.close()


# --------------------------------------------------------------------------- helpers
def _create(api: TestClient, path: str, body: dict) -> dict:
    r = api.post(path, json=body)
    assert r.status_code == 201, (path, r.status_code, r.text)
    return r.json()


def _stock(conn: sqlite3.Connection, store_id: int, product_id: int, delta: int, reason: str = "receipt") -> None:
    with db.transaction(conn):
        ledger.apply_movement(conn, store_id, product_id, delta, reason, "test")


def _insert_store(conn: sqlite3.Connection, code: str = "BLR-01") -> int:
    return conn.execute("INSERT INTO stores (code, name, region) VALUES (?, 'Store', 'South')", (code,)).lastrowid


def _insert_product(conn: sqlite3.Connection, sku: str = "SKU-1", name: str = "Widget", category: str = "Home") -> int:
    return conn.execute(
        "INSERT INTO products (sku, name, category, price_cents, reorder_point) VALUES (?, ?, ?, 1000, 5)", (sku, name, category)
    ).lastrowid


def _route(method: str, path: str):
    """Resolve ``method path`` against ``catalog.BRIDGE_ROUTES`` exactly like the bridge does (exactly one match)."""
    found = [(re.match(pattern, path), handler) for m, pattern, handler in catalog.BRIDGE_ROUTES if m == method]
    found = [(match, handler) for match, handler in found if match]
    assert len(found) == 1, f"expected exactly one bridge route for {method} {path}, found {len(found)}"
    match, handler = found[0]
    return handler, {key: int(value) for key, value in match.groupdict().items()}


def _bridge(conn: sqlite3.Connection, method: str, path: str, *, body=None, query: dict[str, str] | None = None):
    handler, params = _route(method, path)
    call = BridgeCall(conn=conn, params=params, query=dict(query or {}), body={} if body is None else body, headers={}, out_headers={})
    return handler(call)


def _sample(path: str) -> str:
    return re.sub(r"\{[a-z_]+\}", "1", path)


# --------------------------------------------------------------------------- stores
def test_create_store_returns_201_and_duplicate_code_is_409_duplicate(api):
    store = _create(api, "/stores", STORE)
    assert store == {"id": store["id"], **STORE}
    dup = api.post("/stores", json={**STORE, "name": "Other"})
    assert dup.status_code == 409
    assert dup.json() == {"detail": "store code 'BLR-01' already exists", "code": DUPLICATE}
    assert api.get("/stores").status_code == 200
    assert api.get("/stores").json() == [store]


def test_get_store_by_id_and_404_code(api):
    store = _create(api, "/stores", STORE)
    r = api.get(f"/stores/{store['id']}")
    assert r.status_code == 200
    assert r.json() == store
    missing = api.get("/stores/999")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "store 999 not found", "code": NOT_FOUND}


def test_patch_store_updates_name_and_region_but_code_is_immutable(api):
    store = _create(api, "/stores", STORE)
    sid = store["id"]
    r = api.patch(f"/stores/{sid}", json={"name": "Bengaluru Koramangala"})
    assert r.status_code == 200, r.text
    assert r.json() == {**store, "name": "Bengaluru Koramangala"}
    r = api.patch(f"/stores/{sid}", json={"region": "Karnataka"})
    assert r.status_code == 200
    assert r.json() == {**store, "name": "Bengaluru Koramangala", "region": "Karnataka"}
    r = api.patch(f"/stores/{sid}", json={"name": "Both", "region": "West"})
    assert r.status_code == 200
    assert (r.json()["name"], r.json()["region"], r.json()["code"]) == ("Both", "West", "BLR-01")
    assert api.get(f"/stores/{sid}").json() == r.json()
    assert api.get("/stores").json() == [r.json()]
    # code is immutable: asking to change it is rejected (422) and nothing from that body is applied
    r = api.patch(f"/stores/{sid}", json={"code": "NEW-01", "name": "Ignored"})
    assert r.status_code == 422
    assert api.get(f"/stores/{sid}").json()["name"] == "Both"
    r = api.patch("/stores/999", json={"name": "Ghost"})
    assert r.status_code == 404
    assert r.json() == {"detail": "store 999 not found", "code": NOT_FOUND}


def test_patch_store_empty_or_null_only_body_is_422_validation_error(api):
    sid = _create(api, "/stores", STORE)["id"]
    for body in ({}, {"name": None}, {"name": None, "region": None}):
        r = api.patch(f"/stores/{sid}", json=body)
        assert r.status_code == 422, body
        assert r.json() == {"detail": "no fields to update", "code": VALIDATION}, body
    assert api.get(f"/stores/{sid}").json() == {"id": sid, **STORE}


def test_patch_store_enforces_the_store_in_bounds(api):
    sid = _create(api, "/stores", STORE)["id"]
    for body in ({"name": ""}, {"name": "x" * 81}, {"region": ""}, {"region": "r" * 41}, {"name": 5}, {"id": 7}):
        assert api.patch(f"/stores/{sid}", json=body).status_code == 422, body
    ok = api.patch(f"/stores/{sid}", json={"name": "n" * 80, "region": "r" * 40})
    assert ok.status_code == 200
    assert (len(ok.json()["name"]), len(ok.json()["region"])) == (80, 40)


# --------------------------------------------------------------------------- products
def test_create_product_returns_201_with_null_updated_at_and_duplicate_sku_is_409(api):
    product = _create(api, "/products", PRODUCT)
    assert product == {"id": product["id"], **PRODUCT, "active": True, "updated_at": None}
    dup = api.post("/products", json={**PRODUCT, "name": "Other"})
    assert dup.status_code == 409
    assert dup.json() == {"detail": "sku 'SKU-1' already exists", "code": DUPLICATE}
    one = api.get(f"/products/{product['id']}")
    assert one.status_code == 200
    assert one.json() == product
    missing = api.get("/products/999")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "product 999 not found", "code": NOT_FOUND}


def test_patch_product_each_field_and_active_round_trip(api):
    pid = _create(api, "/products", PRODUCT)["id"]
    expected = {"id": pid, **PRODUCT, "active": True}
    last = ""
    patches = ({"name": "Widget Pro"}, {"category": "Tools"}, {"price_cents": 1250}, {"reorder_point": 0}, {"active": False}, {"active": True})
    for patch in patches:
        r = api.patch(f"/products/{pid}", json=patch)
        assert r.status_code == 200, (patch, r.text)
        body = r.json()
        expected.update(patch)
        assert ISO_RE.match(body["updated_at"]), body
        assert body["updated_at"] >= last
        last = body["updated_at"]
        assert {k: v for k, v in body.items() if k != "updated_at"} == expected, patch
        assert api.get(f"/products/{pid}").json() == body
        if patch == {"active": False}:
            assert api.get("/products").json()["total"] == 0  # hidden while inactive ...
    assert api.get("/products").json()["total"] == 1  # ... and listed again once reactivated


def test_patch_product_sku_and_id_are_immutable_and_nothing_is_applied(api):
    pid = _create(api, "/products", PRODUCT)["id"]
    for body in ({"sku": "SKU-9"}, {"sku": "SKU-9", "name": "Renamed"}, {"id": 42, "name": "Renamed"}):
        assert api.patch(f"/products/{pid}", json=body).status_code == 422, body
    current = api.get(f"/products/{pid}").json()
    assert (current["sku"], current["name"], current["updated_at"]) == ("SKU-1", "Widget", None)


def test_patch_product_empty_body_is_422_validation_error(api):
    pid = _create(api, "/products", PRODUCT)["id"]
    for body in ({}, {"price_cents": None}, {"name": None, "active": None}):
        r = api.patch(f"/products/{pid}", json=body)
        assert r.status_code == 422, body
        assert r.json() == {"detail": "no fields to update", "code": VALIDATION}, body
    assert api.get(f"/products/{pid}").json()["updated_at"] is None


def test_patch_product_validation_bounds_and_404(api):
    pid = _create(api, "/products", PRODUCT)["id"]
    bad_bodies = (
        {"price_cents": -1},
        {"reorder_point": -1},
        {"name": ""},
        {"name": "n" * 121},
        {"category": ""},
        {"category": "c" * 41},
        {"active": "maybe"},
        {"price_cents": "free"},
    )
    for body in bad_bodies:
        assert api.patch(f"/products/{pid}", json=body).status_code == 422, body
    assert api.get(f"/products/{pid}").json()["updated_at"] is None
    ok = api.patch(f"/products/{pid}", json={"price_cents": 0, "reorder_point": 0, "name": "n" * 120, "category": "c" * 40})
    assert ok.status_code == 200
    assert (ok.json()["price_cents"], ok.json()["reorder_point"]) == (0, 0)
    r = api.patch("/products/999", json={"name": "Ghost"})
    assert r.status_code == 404
    assert r.json() == {"detail": "product 999 not found", "code": NOT_FOUND}


def test_price_change_does_not_rewrite_existing_order_lines(api, direct):
    store = _create(api, "/stores", STORE)
    product = _create(api, "/products", PRODUCT)
    _stock(direct, store["id"], product["id"], 10)
    line = schemas.OrderLineIn(product_id=product["id"], quantity=2)
    order, created = service.place_order(direct, schemas.OrderIn(store_id=store["id"], lines=[line]), None)
    assert created is True
    assert (order["total_cents"], order["lines"][0]["unit_price_cents"]) == (2000, 1000)

    r = api.patch(f"/products/{product['id']}", json={"price_cents": 2500})
    assert r.status_code == 200
    assert r.json()["price_cents"] == 2500
    after = service.get_order(direct, order["id"])
    assert after["lines"][0]["unit_price_cents"] == 1000  # the order keeps its price snapshot
    assert after["total_cents"] == 2000
    # new orders are priced at the new price
    line = schemas.OrderLineIn(product_id=product["id"], quantity=1)
    fresh, _ = service.place_order(direct, schemas.OrderIn(store_id=store["id"], lines=[line]), None)
    assert fresh["total_cents"] == 2500
    assert api.get(f"/products/{product['id']}/inventory").json()[0]["on_hand"] == 7


def test_soft_delete_is_idempotent_and_hidden_from_the_default_listing(api):
    keep = _create(api, "/products", PRODUCT)
    gone = _create(api, "/products", {**PRODUCT, "sku": "SKU-2", "name": "Gadget", "category": "Electronics"})
    r = api.delete(f"/products/{gone['id']}")
    assert r.status_code == 204
    assert r.content == b""
    first = api.get(f"/products/{gone['id']}")
    assert first.status_code == 200  # soft delete: still addressable by id
    assert first.json()["active"] is False
    assert ISO_RE.match(first.json()["updated_at"])
    again = api.delete(f"/products/{gone['id']}")
    assert again.status_code == 204
    assert api.get(f"/products/{gone['id']}").json() == first.json()  # a repeated delete changes nothing, not even updated_at

    default = api.get("/products").json()
    assert default["total"] == 1
    assert [p["id"] for p in default["items"]] == [keep["id"]]
    everything = api.get("/products", params={"include_inactive": "true"}).json()
    assert everything["total"] == 2
    assert [(p["id"], p["active"]) for p in everything["items"]] == [(keep["id"], True), (gone["id"], False)]
    assert api.get("/products", params={"q": "Gadget"}).json()["total"] == 0
    assert api.get("/products", params={"q": "Gadget", "include_inactive": "true"}).json()["total"] == 1
    assert api.get("/products", params={"category": "Electronics", "include_inactive": "false"}).json()["total"] == 0

    missing = api.delete("/products/999")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "product 999 not found", "code": NOT_FOUND}
    # reactivation through PATCH brings it back into the default listing
    assert api.patch(f"/products/{gone['id']}", json={"active": True}).status_code == 200
    assert api.get("/products").json()["total"] == 2


def test_list_products_filters_paging_and_bounds(api):
    specs = [("SKU-1", "Widget", "Home"), ("SKU-2", "Gadget", "Electronics"), ("SKU-3", "Gizmo", "Electronics"), ("SKU-4", "Widget Mini", "Home")]
    ids = [
        _create(api, "/products", {"sku": sku, "name": name, "category": cat, "price_cents": 100 * (i + 1), "reorder_point": i})["id"]
        for i, (sku, name, cat) in enumerate(specs)
    ]
    first = api.get("/products")
    assert first.status_code == 200
    assert set(first.json()) == {"items", "total", "limit", "offset"}
    assert (first.json()["total"], first.json()["limit"], first.json()["offset"]) == (4, 20, 0)
    assert [p["id"] for p in first.json()["items"]] == ids
    assert all(set(p) == PRODUCT_KEYS for p in first.json()["items"])
    assert [p["sku"] for p in api.get("/products", params={"q": "wid"}).json()["items"]] == ["SKU-1", "SKU-4"]  # name, case-insensitive
    assert [p["sku"] for p in api.get("/products", params={"q": "sku-3"}).json()["items"]] == ["SKU-3"]  # sku too
    assert api.get("/products", params={"category": "Electronics"}).json()["total"] == 2
    assert api.get("/products", params={"category": "Electronics", "q": "Giz"}).json()["total"] == 1
    assert api.get("/products", params={"category": "Toys"}).json()["total"] == 0
    second = api.get("/products", params={"limit": 2, "offset": 2}).json()
    assert [p["id"] for p in second["items"]] == ids[2:]
    assert (second["total"], second["limit"], second["offset"]) == (4, 2, 2)
    # the dashboard's exact query shape: an empty q means "no filter"
    ui = api.get("/products?limit=100&q=&include_inactive=true")
    assert ui.status_code == 200
    assert (ui.json()["total"], ui.json()["limit"]) == (4, 100)
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"q": "x" * 61}, {"limit": "many"}):
        assert api.get("/products", params=params).status_code == 422, params
    assert api.get("/products", params={"limit": 100, "offset": 0, "q": "x" * 60}).status_code == 200


def test_search_escapes_like_wildcards(api):
    names = {"A-100": "100%", "B-1000": "1000 Pack", "C-SET": "Set_Alpha", "D-SET": "SetXAlpha", "E-BS": "a\\b", "F-AB": "ab"}
    for sku, name in names.items():
        _create(api, "/products", {**PRODUCT, "sku": sku, "name": name})

    def found(q: str) -> list[str]:
        r = api.get("/products", params={"q": q})
        assert r.status_code == 200, r.text
        return sorted(p["name"] for p in r.json()["items"])

    assert found("100%") == ["100%"]  # a literal percent sign, not a wildcard
    assert found("100") == ["100%", "1000 Pack"]
    assert found("Set_Alpha") == ["Set_Alpha"]  # a literal underscore, not "any character"
    assert found("Set") == ["SetXAlpha", "Set_Alpha"]
    assert found("a\\b") == ["a\\b"]  # a literal backslash
    assert found("%") == ["100%"]
    assert found("_") == ["Set_Alpha"]


def test_product_inventory_across_stores(api, direct):
    s1 = _create(api, "/stores", STORE)
    s2 = _create(api, "/stores", STORE_2)
    _create(api, "/stores", {"code": "MUM-01", "name": "Mumbai", "region": "West"})  # never stocked → no row
    product = _create(api, "/products", PRODUCT)  # reorder_point 5, price 1000
    other = _create(api, "/products", {**PRODUCT, "sku": "SKU-2", "name": "Gadget"})
    _stock(direct, s1["id"], product["id"], 7)
    _stock(direct, s2["id"], product["id"], 3)
    _stock(direct, s2["id"], product["id"], -1, "adjustment")
    _stock(direct, s1["id"], other["id"], 4)

    r = api.get(f"/products/{product['id']}/inventory")
    assert r.status_code == 200
    base = {"product_id": product["id"], "sku": "SKU-1", "name": "Widget", "reorder_point": 5, "price_cents": 1000}
    assert r.json() == [
        {**base, "store_id": s1["id"], "store_code": "BLR-01", "on_hand": 7, "version": 1, "below_reorder": False, "value_cents": 7000},
        {**base, "store_id": s2["id"], "store_code": "HYD-01", "on_hand": 2, "version": 2, "below_reorder": True, "value_cents": 2000},
    ]
    assert api.get(f"/products/{other['id']}/inventory").json() == [
        {
            "store_id": s1["id"],
            "store_code": "BLR-01",
            "product_id": other["id"],
            "sku": "SKU-2",
            "name": "Gadget",
            "on_hand": 4,
            "reorder_point": 5,
            "version": 1,
            "below_reorder": True,
            "price_cents": 1000,
            "value_cents": 4000,
        }
    ]
    # the valuation follows the current price; deactivation keeps the stock view available
    assert api.patch(f"/products/{product['id']}", json={"price_cents": 500}).status_code == 200
    assert api.delete(f"/products/{product['id']}").status_code == 204
    assert [row["value_cents"] for row in api.get(f"/products/{product['id']}/inventory").json()] == [3500, 1000]
    fresh = _create(api, "/products", {**PRODUCT, "sku": "SKU-3", "name": "Nothing"})
    assert api.get(f"/products/{fresh['id']}/inventory").json() == []
    missing = api.get("/products/999/inventory")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "product 999 not found", "code": NOT_FOUND}


# --------------------------------------------------------------------------- domain module
def test_domain_functions_raise_canonical_service_errors(direct):
    sid = _insert_store(direct)
    pid = _insert_product(direct)
    with pytest.raises(ServiceError) as dup_store:
        catalog.create_store(direct, schemas.StoreIn(**STORE))
    assert (dup_store.value.status, dup_store.value.code) == (409, DUPLICATE)
    with pytest.raises(ServiceError) as dup_sku:
        catalog.create_product(direct, schemas.ProductIn(**PRODUCT))
    assert (dup_sku.value.status, dup_sku.value.code) == (409, DUPLICATE)

    missing_calls = (
        lambda: catalog.get_store(direct, 999),
        lambda: catalog.update_store(direct, 999, catalog.StorePatch(name="x")),
        lambda: catalog.get_product(direct, 999),
        lambda: catalog.update_product(direct, 999, catalog.ProductPatch(name="x")),
        lambda: catalog.deactivate_product(direct, 999),
        lambda: catalog.product_inventory(direct, 999),
    )
    for call in missing_calls:
        with pytest.raises(ServiceError) as missing:
            call()
        assert (missing.value.status, missing.value.code) == (404, NOT_FOUND)
        assert missing.value.detail.endswith(" 999 not found")

    empty_calls = (
        lambda: catalog.update_store(direct, sid, catalog.StorePatch()),
        lambda: catalog.update_product(direct, pid, catalog.ProductPatch()),
        lambda: catalog.update_product(direct, pid, catalog.ProductPatch(name=None)),
    )
    for call in empty_calls:
        with pytest.raises(ServiceError) as empty:
            call()
        assert (empty.value.status, empty.value.detail, empty.value.code) == (422, "no fields to update", VALIDATION)

    assert catalog.get_product(direct, pid) == {"id": pid, **PRODUCT, "active": True, "updated_at": None}
    assert catalog.deactivate_product(direct, pid) is None
    assert catalog.get_product(direct, pid)["active"] is False
    assert catalog.get_store(direct, sid) == {"id": sid, "code": "BLR-01", "name": "Store", "region": "South"}


def test_list_products_keeps_the_v01_positional_signature(direct):
    ids = [_insert_product(direct, sku) for sku in ("SKU-1", "SKU-2", "SKU-3")]
    direct.execute("UPDATE products SET active = 0 WHERE id = ?", (ids[1],))
    items, total = catalog.list_products(direct, None, None, 20, 0)
    assert total == 2
    assert [p["id"] for p in items] == [ids[0], ids[2]]
    items, total = catalog.list_products(direct, "sku", "Home", 1, 1)
    assert total == 2
    assert [p["id"] for p in items] == [ids[2]]
    items, total = catalog.list_products(direct, None, None, 20, 0, include_inactive=True)
    assert total == 3
    assert [p["active"] for p in items] == [True, False, True]
    assert all(set(p) == PRODUCT_KEYS for p in items)


def test_patch_models_reject_unknown_fields_and_out_of_range_values():
    assert catalog.StorePatch(name="x").model_dump(exclude_unset=True) == {"name": "x"}
    assert catalog.ProductPatch(active=False).model_dump(exclude_unset=True) == {"active": False}
    for bad in ({"code": "X"}, {"name": ""}, {"region": "r" * 41}, {"id": 1}):
        with pytest.raises(ValidationError):
            catalog.StorePatch.model_validate(bad)
    for bad in ({"sku": "SKU-9"}, {"price_cents": -1}, {"reorder_point": -1}, {"name": "n" * 121}, {"category": ""}, {"active": "maybe"}):
        with pytest.raises(ValidationError):
            catalog.ProductPatch.model_validate(bad)
    assert catalog.ProductOut.model_fields["updated_at"].default is None
    assert catalog.ProductPage.model_fields["items"].annotation == list[catalog.ProductOut]
    assert set(catalog.ProductInventoryRow.model_fields) == set(schemas.InventoryRow.model_fields) | {"price_cents", "value_cents"}


# --------------------------------------------------------------------------- bridge routes
def test_bridge_routes_mirror_the_router_one_to_one():
    http = {(method, route.path) for route in catalog_router.router.routes if isinstance(route, APIRoute) for method in route.methods}
    assert len(http) == 10
    assert len(catalog.BRIDGE_ROUTES) == len(http)
    for method, path in http:
        matches = [pattern for m, pattern, _handler in catalog.BRIDGE_ROUTES if m == method and re.match(pattern, _sample(path))]
        assert len(matches) == 1, (method, path, matches)
    for method, pattern, handler in catalog.BRIDGE_ROUTES:
        assert callable(handler)
        assert pattern.startswith("^") and pattern.endswith("$"), pattern
        assert any(m == method and re.match(pattern, _sample(path)) for m, path in http), (method, pattern)


def test_bridge_handlers_match_the_http_semantics(direct):
    status, store = _bridge(direct, "POST", "/stores", body=STORE)
    assert status == 201
    assert store == {"id": store["id"], **STORE}
    assert _bridge(direct, "GET", "/stores") == (200, [store])
    assert _bridge(direct, "GET", f"/stores/{store['id']}") == (200, store)
    status, patched = _bridge(direct, "PATCH", f"/stores/{store['id']}", body={"region": "West"})
    assert (status, patched) == (200, {**store, "region": "West"})

    status, product = _bridge(direct, "POST", "/products", body=PRODUCT)
    assert status == 201
    assert product == {"id": product["id"], **PRODUCT, "active": True, "updated_at": None}
    status, listed = _bridge(direct, "GET", "/products", query={"q": "wid", "limit": "5", "offset": "0"})
    assert status == 200
    assert listed == {"items": [product], "total": 1, "limit": 5, "offset": 0}
    assert _bridge(direct, "GET", "/products")[1] == {"items": [product], "total": 1, "limit": 20, "offset": 0}  # same defaults as HTTP
    status, patched = _bridge(direct, "PATCH", f"/products/{product['id']}", body={"price_cents": 2000})
    assert (status, patched["price_cents"]) == (200, 2000)
    assert ISO_RE.match(patched["updated_at"])
    assert _bridge(direct, "GET", f"/products/{product['id']}") == (200, patched)
    assert _bridge(direct, "GET", f"/products/{product['id']}/inventory") == (200, [])
    assert _bridge(direct, "DELETE", f"/products/{product['id']}") == (204, None)
    assert _bridge(direct, "DELETE", f"/products/{product['id']}") == (204, None)
    assert _bridge(direct, "GET", "/products")[1]["total"] == 0
    assert _bridge(direct, "GET", "/products", query={"include_inactive": "true"})[1]["total"] == 1
    assert _bridge(direct, "GET", "/products", query={"include_inactive": "false"})[1]["total"] == 0
    assert _bridge(direct, "GET", f"/products/{product['id']}")[1]["active"] is False


def test_bridge_handlers_raise_the_same_errors(direct):
    store_status, store = _bridge(direct, "POST", "/stores", body=STORE)
    product_status, product = _bridge(direct, "POST", "/products", body=PRODUCT)
    assert (store_status, product_status) == (201, 201)
    with pytest.raises(ServiceError) as dup:
        _bridge(direct, "POST", "/products", body=PRODUCT)
    assert (dup.value.status, dup.value.code) == (409, DUPLICATE)
    missing_calls = (
        ("GET", "/stores/999", None),
        ("PATCH", "/stores/999", {"name": "x"}),
        ("GET", "/products/999", None),
        ("PATCH", "/products/999", {"name": "x"}),
        ("DELETE", "/products/999", None),
        ("GET", "/products/999/inventory", None),
    )
    for method, path, body in missing_calls:
        with pytest.raises(ServiceError) as missing:
            _bridge(direct, method, path, body=body)
        assert (missing.value.status, missing.value.code) == (404, NOT_FOUND), (method, path)
    for query in ({"limit": "0"}, {"limit": "101"}, {"offset": "-1"}, {"q": "x" * 61}, {"limit": "many"}):
        with pytest.raises(ServiceError) as bad:
            _bridge(direct, "GET", "/products", query=query)
        assert (bad.value.status, bad.value.code) == (422, VALIDATION), query
    with pytest.raises(ServiceError) as empty:
        _bridge(direct, "PATCH", f"/products/{product['id']}", body={})
    assert (empty.value.status, empty.value.detail, empty.value.code) == (422, "no fields to update", VALIDATION)
    with pytest.raises(ServiceError) as empty_store:
        _bridge(direct, "PATCH", f"/stores/{store['id']}")  # no body at all → {} → nothing to update
    assert (empty_store.value.status, empty_store.value.code) == (422, VALIDATION)
    # malformed bodies surface as pydantic errors, which the bridge renders as 422 validation_error
    invalid_bodies = (
        ("POST", "/stores", {"code": "bad code!", "name": "x", "region": "y"}),
        ("POST", "/products", {**PRODUCT, "price_cents": -1}),
        ("PATCH", f"/products/{product['id']}", {"sku": "SKU-9"}),
        ("PATCH", f"/stores/{store['id']}", {"code": "NEW-01"}),
        ("POST", "/stores", ["not", "an", "object"]),
    )
    for method, path, body in invalid_bodies:
        with pytest.raises(ValidationError):
            _bridge(direct, method, path, body=body)
    assert _bridge(direct, "GET", f"/products/{product['id']}")[1]["updated_at"] is None  # nothing above was applied


# --------------------------------------------------------------------------- OpenAPI contract
def test_openapi_documents_summaries_models_and_error_bodies(api):
    spec = api.get("/openapi.json").json()
    paths = spec["paths"]
    expected_errors = {
        ("post", "/stores"): {"409", "422"},
        ("get", "/stores"): set(),
        ("get", "/stores/{store_id}"): {"404", "422"},
        ("patch", "/stores/{store_id}"): {"404", "422"},
        ("post", "/products"): {"409", "422"},
        ("get", "/products"): {"422"},
        ("get", "/products/{product_id}"): {"404", "422"},
        ("patch", "/products/{product_id}"): {"404", "422"},
        ("delete", "/products/{product_id}"): {"404", "422"},
        ("get", "/products/{product_id}/inventory"): {"404", "422"},
    }
    assert set(paths) == {path for _method, path in expected_errors}
    seen = set()
    for path, operations in paths.items():
        for method, op in operations.items():
            seen.add((method, path))
            assert op.get("summary"), (method, path)
            assert op.get("tags"), (method, path)
            errors = {code for code in op["responses"] if code.startswith("4")}
            assert errors == expected_errors[(method, path)], (method, path, errors)
            for code in errors:
                ref = op["responses"][code]["content"]["application/json"]["schema"]["$ref"]
                assert ref.endswith("/ErrorBody"), (method, path, code, ref)
    assert seen == set(expected_errors)
    assert "204" in paths["/products/{product_id}"]["delete"]["responses"]
    assert "content" not in paths["/products/{product_id}"]["delete"]["responses"]["204"]
    assert paths["/stores"]["post"]["responses"]["201"]["content"]["application/json"]["schema"]["$ref"].endswith("/Store")
    assert paths["/products"]["post"]["responses"]["201"]["content"]["application/json"]["schema"]["$ref"].endswith("/ProductOut")
    page_schema = spec["components"]["schemas"]["ProductPage"]
    assert page_schema["properties"]["items"]["items"]["$ref"].endswith("/ProductOut")
    assert set(page_schema["required"]) == {"items", "total", "limit", "offset"}
    inventory = paths["/products/{product_id}/inventory"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert inventory["type"] == "array"
    assert inventory["items"]["$ref"].endswith("/ProductInventoryRow")
    assert set(spec["components"]["schemas"]["ProductOut"]["properties"]) == PRODUCT_KEYS
