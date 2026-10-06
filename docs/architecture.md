# StockLine architecture

StockLine is a small multi-store inventory and order service: Python, FastAPI, SQLite,
plain SQL. The same business logic runs in two places:

* behind a FastAPI server (`app/main.py`), and
* inside the browser demo, where Pyodide executes the identical Python modules through a
  framework-free router (`app/bridge.py`).

This document describes how the pieces fit together in v0.2.

## Module layout

```
app/
  common.py        shared primitives: ServiceError(+code), error codes, now_iso, request_hash,
                   like_pattern, CSV helpers, BridgeCall, BROWSER_MODULES / SERVER_ONLY_MODULES
  db.py            connection factory, frozen v1 schema, versioned migrations (PRAGMA user_version)
  ledger.py        apply_movement (ledger + cached balance + running balance), integrity, rebuild
  catalog.py       stores and products (create / get / list / patch / soft delete)
  inventory.py     adjustments, inventory views, movement feed, receipts, transfers, CSV exports
  orders.py        place / get / list / cancel / fulfil, idempotency, CSV export
  reports.py       reorder, summary, sales reports, CSV export
  service.py       compatibility facade re-exporting the domain modules (single import point)
  seed.py          deterministic demo data (3 stores, 12 products, back-dated orders)
  bridge.py        framework-free router used by the browser demo (aggregates BRIDGE_ROUTES)
  deps.py          Settings, connection pool, FastAPI dependency, lifespan        (server only)
  observability.py request ids, access log, metrics, error handlers              (server only)
  security.py      security headers, Content-Security-Policy, API key, CORS      (server only)
  main.py          application assembly                                           (server only)
  routers/         thin FastAPI routers per domain: system, catalog, inventory, orders, reports
public/            dashboard: index.html, app.js, lib.js, styles.css (no build step)
scripts/           dev.py task runner, build_site.py, smoke.sh, bench.py, browser_check.mjs
```

Every module listed in `common.BROWSER_MODULES` is shipped to the browser and therefore must not
import FastAPI, Starlette, uvicorn or threading primitives. `SERVER_ONLY_MODULES` names the rest.
The static-site builder and the parity tests both read that literal, so adding a module to the
demo is a one-line change.

## Request flow (server)

```
client ─► security headers / CSP ─► request id + timing + access log + metrics
       ─► API-key check (mutating methods, when configured) ─► router
       ─► domain function(conn, ...) ─► SQLite
```

* `deps.get_conn()` checks a connection out of a small pool for the lifetime of one request.
  No two requests share a connection; writers serialise on SQLite's file lock.
* Domain functions receive an open connection as their first argument and raise
  `ServiceError(status, detail, code)`; the API layer turns that into
  `{"detail", "code", "request_id"}` with the right status.
* Middlewares are pure ASGI, so headers and request ids are present on every response,
  including static files, validation errors and unhandled exceptions (answered as JSON 500).
* The dashboard is mounted at `/` after every router. A small guard in front of the static files
  checks the declared routes first, so a known API path with the wrong method is a `405` with an
  `Allow` header rather than a file-server answer; unknown paths still reach the static mount.

## Write model

* `BEGIN IMMEDIATE` transactions (`db.transaction`) take the write lock up front, so
  concurrent writers queue instead of failing half-way.
* Stock is an append-only ledger (`stock_movements`); `inventory.on_hand` is a cached balance
  updated in the same transaction, guarded by `CHECK (on_hand >= 0)`. Every movement also
  stores `balance_after`, the running balance at that point.
* `ledger.ledger_integrity` compares the cache with the ledger, checks the running-balance
  chain and order totals; `ledger.rebuild_balances` repairs the cache from the ledger.
* Orders reserve stock atomically: pricing comes from the catalogue at order time and the
  whole order rolls back if any line is short.
* One uvicorn worker per database file: the pool and the metrics registry are in-process.

## Idempotency

`POST /orders` and `POST /transfers` accept an `Idempotency-Key` header (≤ 64 chars).

1. A canonical fingerprint of the request body is hashed (`common.request_hash`).
2. A fast read checks for an existing row with that key; a matching (or legacy, empty)
   fingerprint returns the original record with status 200 and `Idempotent-Replayed: true`.
3. Inside the write transaction the key is checked again, so concurrent replays never race:
   the first wins (201), the others see the stored row (200). A reused key with a different
   body is rejected with 422 `idempotency_key_reuse`.

Keys are global and never expire.

## Schema versioning

`db.migrate` uses `PRAGMA user_version`. Version 0 files (either empty or created by v0.1)
receive the frozen v1 schema (`IF NOT EXISTS` only) and are stamped 1; later steps run one
statement at a time inside a transaction and stamp the new version on success. Migrations are
additive — v0.1 code still runs against a v2 file. See `docs/migrations.md`.

## Browser demo

`public/app.js` first probes `health`. If a server answers, it talks to it with relative
URLs. Otherwise it loads Pyodide from the CDN, writes the `PY_FILES` modules into the virtual
file system and dispatches every request through `bridge.handle_json`, which mirrors the
FastAPI routes one-to-one (same status codes, bodies, headers, 405/422 behaviour). The static
site is produced by `scripts/build_site.py`, which also injects the Content-Security-Policy
meta tag the Pages build needs for the WebAssembly runtime.

## Security headers

`security.install` adds a strict Content-Security-Policy (`default-src 'self'`, no inline
script or style), `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`,
`Permissions-Policy`, cross-origin isolation headers and `Cache-Control: no-store` for JSON
and CSV responses. The interactive API docs receive a dedicated policy because Swagger UI
loads from a CDN. `STOCKLINE_API_KEY` optionally protects every mutating method;
`STOCKLINE_CORS_ORIGINS` enables CORS for named origins. Details in `docs/operations.md`.

## Testing strategy

* Unit and HTTP tests per domain, hermetic temporary databases, no network.
* Migration tests on legacy database files; ledger integrity and rebuild tests.
* Parity tests: every FastAPI route has a bridge route and vice versa; the bridge imports no
  web framework; the demo module list is consistent everywhere; OpenAPI contract checks.
* Model-based randomised ledger test and threaded concurrency tests (oversell, idempotency
  race, transfer race).
* Static UI checks (CSP cleanliness, assets, module list) plus node tests for the pure
  helpers, and a Playwright workflow check (`scripts/browser_check.mjs`) that asserts the served
  security headers byte for byte, runs the dashboard workflows and CSV downloads under the enforced
  CSP while recording `securitypolicyviolation` events, console refusals and any foreign request,
  and — in a separately labelled stage — serves the built static site and boots the real Pyodide
  runtime from the CDN to exercise browser mode.
* `python3 scripts/dev.py check` runs the local gates sequentially.
