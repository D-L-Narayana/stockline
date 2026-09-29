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
