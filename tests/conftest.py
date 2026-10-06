"""Shared fixtures: a hermetic temporary database per test and a tiny known catalogue.

The pool is reset before the app starts and again after it stops, seeding is disabled so ``seeded``
builds a small deterministic data set, and every ``STOCKLINE_*`` variable that would change middleware
behaviour (API key, CORS, CSP override, HSTS, pool sizing) is cleared so a developer's shell never
alters test results.
"""
import pytest
from fastapi.testclient import TestClient

ENV_RESET = ("STOCKLINE_API_KEY", "STOCKLINE_CORS_ORIGINS", "STOCKLINE_CSP", "STOCKLINE_HSTS", "STOCKLINE_POOL_SIZE", "STOCKLINE_POOL_TIMEOUT")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for var in ENV_RESET:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("STOCKLINE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("STOCKLINE_SEED", "0")
    from app import db, main

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    main.reset_state(seed=False)
    with TestClient(main.app) as c:
        yield c
    main.reset_state(seed=False)


@pytest.fixture()
def seeded(client):
    s1 = client.post("/stores", json={"code": "S1", "name": "Store One", "region": "South"}).json()
    s2 = client.post("/stores", json={"code": "S2", "name": "Store Two", "region": "West"}).json()
    p1 = client.post("/products", json={"sku": "SKU-1", "name": "Widget", "category": "Home", "price_cents": 1000, "reorder_point": 5}).json()
    p2 = client.post("/products", json={"sku": "SKU-2", "name": "Gadget", "category": "Electronics", "price_cents": 2500, "reorder_point": 2}).json()
    client.post(f"/inventory/{s1['id']}/{p1['id']}/adjust", json={"delta": 20, "reason": "receipt"})
    client.post(f"/inventory/{s1['id']}/{p2['id']}/adjust", json={"delta": 3, "reason": "receipt"})
    return {"s1": s1, "s2": s2, "p1": p1, "p2": p2}
