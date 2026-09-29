# StockLine — multi-store inventory & order API

[![CI](https://github.com/D-L-Narayana/stockline/actions/workflows/ci.yml/badge.svg)](https://github.com/D-L-Narayana/stockline/actions/workflows/ci.yml)
[![Demo](https://img.shields.io/badge/demo-live-CC0000)](https://d-l-narayana.github.io/stockline/)

A small but production-shaped REST API for tracking stock across retail stores and
taking orders against it **without ever overselling**. Python + FastAPI + SQLite, no ORM —
the interesting parts are plain SQL and transactions.

**Live demo:** <https://d-l-narayana.github.io/stockline/> — runs the actual Python
service layer in your browser via Pyodide, so you can place orders, replay an
idempotency key, trigger a stale-version 409 and check ledger integrity without a server.

## What it does

| Capability | How |
|---|---|
| Stock is never negative, even under concurrent orders | `BEGIN IMMEDIATE` transactions + `CHECK (on_hand >= 0)`; whole order rolls back if any line is short |
| Safe client retries | `Idempotency-Key` header → same order returned, no double reservation (`UNIQUE` column) |
| Detect stale writes | `expected_version` on stock adjustments → 409 on conflict (optimistic locking) |
| Full audit trail | Every change is an append-only row in `stock_movements`; `on_hand` is a cached balance that `/integrity` can verify against the ledger |
| Store-to-store transfers | Two ledger entries in one transaction |
| Replenishment | `/reports/reorder` lists SKUs at/below reorder point with 30-day sales velocity |

## API

```
GET  /health                              GET  /integrity
GET  /stores            POST /stores      GET  /products  POST /products  GET /products/{id}
GET  /inventory?store_id=&low_stock=      GET  /inventory/{store}/{product}
POST /inventory/{store}/{product}/adjust  GET  /inventory/{store}/{product}/movements
POST /transfers
POST /orders  (Idempotency-Key header)    GET  /orders    GET /orders/{id}
POST /orders/{id}/cancel                  POST /orders/{id}/fulfil
GET  /reports/reorder
```
Interactive OpenAPI docs at `/docs` when running the server.

## Run locally

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # http://localhost:8000  (UI + /docs)
python -m pytest --cov=app             # 27 tests, ~96% coverage
```
or `docker build -t stockline . && docker run -p 8000:8000 stockline`.

## Layout

```
app/
  db.py        schema, connection, IMMEDIATE-transaction helper
  schemas.py   pydantic request/response models & validation
  service.py   business logic (all SQL lives here)
  main.py      FastAPI routes — thin wrappers over service
  bridge.py    framework-free router used by the browser demo
  seed.py      demo data: 3 stores, 12 SKUs, opening stock, sample orders
tests/         pytest + httpx TestClient, incl. a threaded oversell test
public/        single-file dashboard (vanilla JS)
```

## Design notes

* **Ledger + cache** rather than mutating a single quantity column: cheap reads, and the balance
  can always be rebuilt or audited.
* **SQLite on purpose.** It is enough for a single-node service and keeps the project runnable
  anywhere (including a browser). Swapping to Postgres means changing `db.py` and two `ON CONFLICT` clauses.
* **Server-side pricing.** Order totals are computed from the catalogue at order time, never trusted from the client.

MIT © D L Narayana
