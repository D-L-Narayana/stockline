def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert "X-Request-ID" in r.headers and "X-Response-Time-ms" in r.headers


def test_create_store_and_duplicate(client):
    r = client.post("/stores", json={"code": "BLR-01", "name": "Bengaluru", "region": "South"})
    assert r.status_code == 201
    r2 = client.post("/stores", json={"code": "BLR-01", "name": "Dup", "region": "South"})
    assert r2.status_code == 409


def test_store_validation(client):
    r = client.post("/stores", json={"code": "bad code!", "name": "x", "region": "y"})
    assert r.status_code == 422


def test_product_crud_and_search(seeded, client):
    r = client.get("/products", params={"q": "Wid"})
    assert r.json()["total"] == 1 and r.json()["items"][0]["sku"] == "SKU-1"
    r = client.get("/products", params={"category": "Electronics"})
    assert r.json()["total"] == 1
    r = client.get("/products", params={"limit": 1, "offset": 1})
    assert len(r.json()["items"]) == 1 and r.json()["total"] == 2
    assert client.get("/products/9999").status_code == 404


def test_product_negative_price_rejected(client):
    r = client.post("/products", json={"sku": "SKU-X", "name": "x", "category": "c", "price_cents": -1})
    assert r.status_code == 422


# The tests below run against the assembled application and hold for v0.1 and v0.2 alike (additive surface only);
# the v0.2-only catalogue lifecycle (GET/PATCH stores, PATCH/DELETE products, include_inactive, updated_at,
# per-product stock view, error codes) is covered on the private router app in tests/test_catalog_router.py.
def test_list_stores_returns_created_stores_in_id_order(client):
    a = client.post("/stores", json={"code": "BLR-01", "name": "Bengaluru", "region": "South"}).json()
    b = client.post("/stores", json={"code": "HYD-01", "name": "Hyderabad", "region": "South"}).json()
    r = client.get("/stores")
    assert r.status_code == 200
    assert [s["id"] for s in r.json()] == [a["id"], b["id"]]
    assert r.json()[0] == {"id": a["id"], "code": "BLR-01", "name": "Bengaluru", "region": "South"}


def test_duplicate_errors_name_the_offending_code_or_sku(seeded, client):
    r = client.post("/stores", json={"code": "S1", "name": "Again", "region": "South"})
    assert r.status_code == 409 and "S1" in r.json()["detail"]
    r = client.post("/products", json={"sku": "SKU-1", "name": "Again", "category": "Home", "price_cents": 1})
    assert r.status_code == 409 and "SKU-1" in r.json()["detail"]


def test_get_product_matches_the_created_product(seeded, client):
    r = client.get(f"/products/{seeded['p1']['id']}")
    assert r.status_code == 200
    body = r.json()
    fields = ("id", "sku", "name", "category", "price_cents", "reorder_point", "active")
    assert {k: body[k] for k in fields} == {
        "id": seeded["p1"]["id"],
        "sku": "SKU-1",
        "name": "Widget",
        "category": "Home",
        "price_cents": 1000,
        "reorder_point": 5,
        "active": True,
    }


def test_product_search_matches_sku_case_insensitively_with_the_standard_page_shape(seeded, client):
    r = client.get("/products", params={"q": "sku-2"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 1 and body["items"][0]["name"] == "Gadget"
    assert (body["limit"], body["offset"]) == (20, 0)
    assert client.get("/products", params={"q": "nothing-here"}).json()["total"] == 0


def test_product_list_bounds_are_enforced(seeded, client):
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"q": "x" * 61}):
        assert client.get("/products", params=params).status_code == 422, params
    assert client.get("/products", params={"limit": 100, "offset": 0}).status_code == 200
