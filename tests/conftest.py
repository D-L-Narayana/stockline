import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKLINE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("STOCKLINE_SEED", "0")
    from app import main, db
    main.reset_state()
    db.DB_PATH = str(tmp_path / "t.db")
    with TestClient(main.app) as c:
        yield c
    main.reset_state()


@pytest.fixture()
def seeded(client):
    s1 = client.post("/stores", json={"code": "S1", "name": "Store One", "region": "South"}).json()
    s2 = client.post("/stores", json={"code": "S2", "name": "Store Two", "region": "West"}).json()
    p1 = client.post("/products", json={"sku": "SKU-1", "name": "Widget", "category": "Home", "price_cents": 1000, "reorder_point": 5}).json()
    p2 = client.post("/products", json={"sku": "SKU-2", "name": "Gadget", "category": "Electronics", "price_cents": 2500, "reorder_point": 2}).json()
    client.post(f"/inventory/{s1['id']}/{p1['id']}/adjust", json={"delta": 20, "reason": "receipt"})
    client.post(f"/inventory/{s1['id']}/{p2['id']}/adjust", json={"delta": 3, "reason": "receipt"})
    return {"s1": s1, "s2": s2, "p1": p1, "p2": p2}
