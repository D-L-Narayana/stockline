# StockLine — multi-store inventory & order API

[![CI](https://github.com/D-L-Narayana/stockline/actions/workflows/ci.yml/badge.svg)](https://github.com/D-L-Narayana/stockline/actions/workflows/ci.yml)
[![Demo](https://img.shields.io/badge/demo-live-CC0000)](https://d-l-narayana.github.io/stockline/)

A small but production-shaped REST API for tracking stock across retail stores and
taking orders against it **without ever overselling**. Python + FastAPI + SQLite, no ORM —
the interesting parts are plain SQL and transactions.

**Live demo:** <https://d-l-narayana.github.io/stockline/> — runs the actual Python
service layer in your browser via Pyodide, so you can place orders, replay an
idempotency key, trigger a stale-version 409, audit the ledger and export CSVs without a server.

## What it does

| Capability | How |
|---|---|
| Stock is never negative, even under concurrent orders | `BEGIN IMMEDIATE` transactions + `CHECK (on_hand >= 0)`; whole order rolls back if any line is short |
| Safe client retries | `Idempotency-Key` header on orders **and** transfers → same record returned (`Idempotent-Replayed: true`), no double reservation; the key is re-checked inside the write transaction, and reusing it with a different body is rejected (422) |
| Detect stale writes | `expected_version` on stock adjustments → 409 `version_conflict` (optimistic locking) |
| Full audit trail | Every change is an append-only row in `stock_movements` with its running `balance_after`; `/integrity` verifies the cached balance, the balance chain and order totals; `/integrity/rebuild` repairs the cache from the ledger |
| Store-to-store transfers | First-class records (`/transfers`), two ledger entries in one transaction |
| Receiving & auditing | Multi-line receipts (`/inventory/{store}/receipts`), a filterable movement feed (`/movements`), CSV exports |
| Replenishment & reporting | `/reports/reorder` (net velocity over a chosen window, days of cover, suggested quantity), `/reports/summary` (valuation per store), `/reports/sales` |
| Operable | Versioned schema migrations, structured logs with request ids, Prometheus `/metrics`, strict security headers and Content-Security-Policy, optional API key and CORS |

## API

```
GET  /health          GET  /integrity        POST /integrity/rebuild        GET /metrics
GET  /stores          POST /stores           GET  /stores/{id}              PATCH /stores/{id}
GET  /products        POST /products         GET  /products/{id}            PATCH /products/{id}
DELETE /products/{id} (soft)                 GET  /products/{id}/inventory
GET  /inventory?store_id=&low_stock=&q=      GET  /inventory/{store}/{product}
POST /inventory/{store}/{product}/adjust     GET  /inventory/{store}/{product}/movements?before_id=
POST /inventory/{store}/receipts             GET  /inventory/export.csv
GET  /movements?store_id=&product_id=&reason=&since=&before_id=   GET /movements/export.csv
POST /transfers  (Idempotency-Key header)    GET  /transfers                GET /transfers/{id}
POST /orders     (Idempotency-Key header)    GET  /orders                   GET /orders/{id}
POST /orders/{id}/cancel                     POST /orders/{id}/fulfil       GET /orders/export.csv
GET  /reports/reorder?days=&store_id=        GET  /reports/reorder.csv
GET  /reports/summary                        GET  /reports/sales?days=&store_id=&group_by=day|product
```

Interactive OpenAPI docs at `/docs` when running the server. Errors are JSON:
`{"detail": "...", "code": "insufficient_stock", "request_id": "..."}`.

## Run locally

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # http://localhost:8000  (dashboard + /docs)
python scripts/dev.py cov              # tests with the coverage gate
python scripts/dev.py check            # compile, tests, node tests, site build, smoke test
```

or `docker compose up --build` (non-root image, SQLite file in the `stockline-data` volume),
or `docker build -t stockline . && docker run -p 8000:8000 stockline`.

`scripts/dev.py` picks `./.venv/bin/python` when it exists; `make help` lists the same tasks.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `STOCKLINE_DB` | `./stockline.db` | SQLite file (migrated in place on start) |
| `STOCKLINE_SEED` | `1` | Seed demo data into an empty database |
| `STOCKLINE_API_KEY` | unset | When set, mutating requests need `X-API-Key` or `Authorization: Bearer` |
| `STOCKLINE_CORS_ORIGINS` | unset | Comma-separated origins allowed for CORS |
| `STOCKLINE_LOG_FORMAT` / `STOCKLINE_LOG_LEVEL` | `text` / `INFO` | Access-log format (`json` for machines) and level |
| `STOCKLINE_POOL_SIZE` / `STOCKLINE_POOL_TIMEOUT` | `8` / `10` | Connection pool size and acquire timeout (seconds) |
| `STOCKLINE_CSP` / `STOCKLINE_HSTS` | strict default / `0` | Override the Content-Security-Policy; send HSTS behind TLS |

See `docs/operations.md` for headers, logs, metrics and deployment notes.

## Layout

```
app/
  common.py      shared primitives: errors + codes, hashing, CSV helpers, browser module list
  db.py          schema v1 (frozen), versioned migrations, IMMEDIATE-transaction helper
  ledger.py      ledger writes with running balance, integrity check, rebuild
  catalog.py  inventory.py  orders.py  reports.py   business logic (all SQL lives here)
  service.py     compatibility facade over the domain modules
  routers/       FastAPI routes — thin wrappers over the domain modules
  deps.py  observability.py  security.py  main.py   server assembly (pool, logs, headers)
  bridge.py      framework-free router used by the browser demo (same routes, same codes)
  seed.py        deterministic demo data: 3 stores, 12 SKUs, 45 days of orders, transfers
tests/           pytest + httpx TestClient: domain, migrations, parity, OpenAPI, concurrency,
                 model-based ledger test, static UI checks; node tests for the UI helpers
public/          dashboard: index.html, app.js, lib.js, styles.css (vanilla JS, no build step, CSP-clean)
scripts/         dev.py task runner, build_site.py, smoke.sh, bench.py, browser_check.mjs
docs/            architecture, migrations, operations, concurrency
```

## Design notes

* **Ledger + cache** rather than mutating a single quantity column: cheap reads, and the balance
  can always be rebuilt or audited (`POST /integrity/rebuild`).
* **SQLite on purpose.** It is enough for a single-node service and keeps the project runnable
  anywhere (including a browser). Run one uvicorn worker per database file.
* **Server-side pricing.** Order totals are computed from the catalogue at order time, never trusted
  from the client; later price changes never alter existing order lines.
* **Additive migrations.** New columns are nullable and new tables are separate, so a v0.1 database
  file works with v0.2 — and v0.1 code still runs against a v0.2 file.
* **Same code in the browser.** The demo executes the identical modules through `bridge.py`; a parity
  test keeps the two surfaces aligned route by route.

MIT © D L Narayana
