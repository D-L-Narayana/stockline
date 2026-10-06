# Concurrency, idempotency and correctness evidence

How StockLine keeps stock correct when many clients write at once, what the automated tests
prove about it, and how to run the benchmark and the browser workflow check yourself.

## The write model

* **One IMMEDIATE transaction per write.** Every mutation (`app/db.py`, `transaction()`) starts
  with `BEGIN IMMEDIATE`, which takes SQLite's single write lock *before* the first read inside the
  transaction. Concurrent writers therefore queue on the lock (`busy_timeout` 5 s) instead of
  reading stale data and failing half-way; each transaction sees the balances left by the previous
  one. Reads outside a transaction run concurrently against the WAL snapshot.
* **One connection per request.** `deps.get_conn()` checks a connection out of a small in-process
  pool (`STOCKLINE_POOL_SIZE`, default 8) for the lifetime of one request and rolls it back if the
  handler left a transaction open. No two requests ever share a connection; a request that cannot
  get one within `STOCKLINE_POOL_TIMEOUT` seconds is answered `503 pool_exhausted` with `Retry-After: 1`.
* **Ledger first, cache second — in the same transaction.** `ledger.apply_movement` appends an
  immutable `stock_movements` row carrying `balance_after` and updates the cached
  `inventory.on_hand` / `version` in one statement each. The cache is guarded by
  `CHECK (on_hand >= 0)`, and the function refuses a movement that would go negative with
  `409 insufficient_stock` before writing anything. An order with several lines either reserves all
  of them or rolls back entirely.
* **Optimistic locking for adjustments.** `expected_version` on `POST /inventory/{s}/{p}/adjust`
  must equal the row's current `version` (which increases by one per movement); a stale value is
  `409 version_conflict` and nothing is written.
* **Audit and repair.** `GET /integrity` compares every cached balance with the sum of its deltas,
  walks the `balance_after` chain and re-adds order totals; `POST /integrity/rebuild` recomputes the
  cache from the ledger inside one IMMEDIATE transaction.

## One uvicorn worker per database file

SQLite serialises writers through the file lock, so correctness would survive several processes —
but the connection pool, the request metrics (`/metrics`) and the seeding guard are in-process
objects. A second worker would open a second pool on the same file and `/metrics` would only ever
describe the process that happened to answer. Run exactly one worker (the container image does)
and scale reads with a larger pool, not with more processes. See `docs/operations.md`.

## Idempotency guarantees (orders and transfers)

`POST /orders` and `POST /transfers` accept an `Idempotency-Key` header (≤ 64 characters).

1. The request body is fingerprinted — `{"store_id", "lines": [[product_id, quantity], …]}` sorted
   by product for orders, `{"from", "to", "product_id", "quantity"}` for transfers — and the
   fingerprint is stored next to the key.
2. **Fast path (no lock):** if the key exists and the stored fingerprint is equal (or `NULL`, for rows
   written before fingerprints existed) the stored record is returned with **200** and
   `Idempotent-Replayed: true`. Nothing is written.
3. **Authoritative path (inside `BEGIN IMMEDIATE`):** the key is looked up again under the write lock.
   A concurrent retry that lost the race sees the winner's row and replays it; the first request to
   hold the lock creates the record (**201**, `Idempotent-Replayed: false`).
4. The same key with a **different** body is `422 idempotency_key_reuse` on either path; the stored
   record is never touched.
5. The `UNIQUE` constraint on the key column is kept as a defensive last line (`409
   idempotency_conflict`, covered by a test that forces the race) — the in-lock re-check means a
   client should never see it.
6. Keys are global per entity type and never expire. A replay returns the record's *current* state
   (a cancelled order replays as `cancelled`).

A failed keyed request (shortage, unknown product, inactive product) writes nothing, so the key is
not consumed and a corrected retry with the same key can succeed.

## What each test proves

| Test | Property |
|---|---|
| `tests/test_orders.py::test_concurrent_orders_never_oversell` | 20 threads buying 2 units from a stock of 20: exactly 10 succeed, the balance ends at 0, `/integrity` is ok (the original v0.1 oversell test). |
| `tests/test_idempotency.py` | Deterministic re-creations of the race window between the fast path and `BEGIN IMMEDIATE` (a competitor commits the key in between): the loser replays the winner, a different body is rejected, the `UNIQUE` fallback answers 409, legacy `NULL` fingerprints replay, the fast path runs no write statements. |
| `tests/test_transfers.py`, `tests/test_movements.py` | Transfers are atomic entities (a failure leaves no row and both balances unchanged) and both movements reference `transfer:{id}`; feeds page by keyset. |
| `tests/test_concurrency.py::test_idempotency_race_creates_exactly_one_order` | 16 threads release the same keyed request at the same instant (barrier): exactly one 201 and fifteen 200 replays with the identical id and `Idempotent-Replayed: true`; one sale movement; `/integrity` ok. |
| `tests/test_concurrency.py::test_transfer_race_on_one_source_never_goes_negative` | 16 concurrent 3-unit transfers out of a store holding 10 succeed exactly three times; every success has its transfer row and its `transfer_out` / `transfer_in` pair; no `balance_after` is negative. |
| `tests/test_concurrency.py::test_opposing_transfers_conserve_units_and_stay_consistent` | Transfers in both directions at once: only 201/409, total units conserved, the final balance equals the opening stock ± the recorded transfers. |
| `tests/test_concurrency.py::test_mixed_workload_has_no_5xx_conserves_units_and_keeps_integrity` | Orders, cancels, fulfilments, transfers, receipts and reads from 16 threads: no 5xx, every balance is explained by the orders that exist, units are conserved across transfers, `/integrity` ok. |
| `tests/test_properties.py` | Model-based randomised test: three seeds × 300 operations over 2 stores × 4 products (receipts, signed adjustments with and without `expected_version`, 1–3-line orders, keyed replays and key reuse, cancel/fulfil in every state, transfers, soft delete and reactivation). After **every** operation the cached `on_hand` and `version` of every pair equal a Python reference model, nothing is negative, and every 4xx is exactly the one the model predicted. At the end: `/integrity` ok, ledger sums equal the cache, the `balance_after` chain is intact, the movement feed pages through exactly the model's movement count, and every order has the model's status. |
| `tests/test_ledger.py`, `tests/test_migrations.py` | Integrity detection (forced mismatch, chain break, order-total mismatch, negative ledger), rebuild repairs and bumps `version`, legacy v0.1 files migrate in place with `balance_after` backfilled. |

All tests use hermetic temporary databases, no sleeps and at most 16 threads (the v0.1 oversell test keeps its 20).

## Running the benchmark

```bash
python3 scripts/dev.py py scripts/bench.py                       # in-process, n=200 per endpoint, 4 threads
python3 scripts/dev.py py scripts/bench.py --n 500 --concurrency 8 --json bench.json
python3 scripts/dev.py py scripts/bench.py --url http://127.0.0.1:8000   # against a running, seeded server
make bench BENCH_ARGS="--n 500 --concurrency 8"
```

`scripts/bench.py` stocks one product, then fires `--n` requests per endpoint — `POST /orders`
(one line, quantity 1), `GET /inventory` (limit 50) and `GET /reports/summary` — over
`--concurrency` threads and prints ops/s, p50/p95/p99/max latency in milliseconds and the status
histogram per endpoint. It exits 1 when any request answered 5xx or failed to connect.

How to read the numbers: in the default mode the application runs in-process behind a
`TestClient` on a temporary seeded SQLite file, so figures include the full middleware stack but no
network hop. `POST /orders` is bounded by SQLite's single writer and the per-commit `fsync`; the two
reads run concurrently. Use `--url` against a real uvicorn process (one worker, see above) for
end-to-end numbers, and keep the defaults small on shared machines.

## Running the browser workflow check

```bash
node scripts/browser_check.mjs --out /tmp/stockline-browser-check [--browsers-path DIR]
node scripts/browser_check.mjs --help
```

The script needs Node ≥ 18 and Playwright with Chromium (resolved at run time from
`$PLAYWRIGHT_MODULE`, `./node_modules`, the ancestor `node_modules` chain or `npm root -g`;
`--browsers-path` sets `PLAYWRIGHT_BROWSERS_PATH`). It exits 2 when Playwright or Chromium is
unavailable — never a silent pass. Stages:

1. **headers** — plain HTTP against a self-started `uvicorn app.main:app` (temporary database, demo
   seed, production defaults; `--base URL` targets a running server instead): the exact
   `Content-Security-Policy` (`security.DEFAULT_CSP`, `security.DOCS_CSP` on `/docs` and `/redoc`),
   every `security.SECURITY_HEADERS` entry, `Cache-Control: no-store` on JSON and CSV, the content
   type of `/`, `/app.js`, `/lib.js`, `/styles.css`, and the CSV export contract. The expected values
   are parsed from `app/security.py` at run time.
2. **context** — one Chromium context with the served CSP enforced: a `securitypolicyviolation`
   listener on every page, every console message, page error, failed request and request URL
   recorded; any request to another origin fails the run. Chromium's "Failed to load resource …
   4xx" console lines are accepted only for same-origin responses the workflows provoke on purpose
   (409 / 422); everything else at error level fails.
3. **workflows** — the dashboard through its UI: inventory filters/sort, a two-line delivery, the
   movements drawer with `balance_after`, stale/current `expected_version`, a keyed order (201 →
   200 replay → 422 key reuse → 409 oversell), cancel and fulfil, a keyed transfer with replay,
   reports (summary, reorder window, sales chart, integrity audit and rebuild), catalog patch /
   soft delete / reactivate / store patch, an XSS probe, all five guided scenarios (individually and
   via "Run all"), the request console with "copy as curl", five CSV exports as real downloads
   (response headers *and* saved file content), keyboard navigation with a visible focus ring,
   dark mode, built-in accessibility checks and a positive control proving the CSP blocks an
   injected inline script.
4. **axe** — only when `axe-core` is resolvable (`$AXE_CORE_PATH`, local or global `node_modules`),
   in a separate `bypassCSP` context that is never used for application-flow steps; otherwise the
   report says `not run: axe-core unavailable`.
5. **responsive** — server mode under the enforced CSP, a fresh context per viewport (390×900 and
   1280×900): every one of the seven views is opened and must not widen the document
   (`document.documentElement.scrollWidth - window.innerWidth` ≤ 1 px), the seven tab buttons must
   stay reachable (the tab bar may scroll inside its own `[role=tablist]`), and every `.table-wrap`
   whose table is wider than the wrapper must still scroll horizontally. A failing view lists every
   box that crosses the right edge together with the ancestor that clips it (or "clipped by nothing").
6. **pages-demo** — the static GitHub Pages build (`scripts/build_site.py`) served by a header-less
   static server: the injected CSP meta tag must equal `build_site.PAGES_CSP`, the Pyodide runtime
   boots from the CDN (the only foreign origin allowed, every request recorded) and a reduced
   workflow set runs in browser mode, including the 390×900 overflow measurement on every view and
   a real CSV download. The stage needs internet access; an unreachable CDN is a *failed* stage, and
   `--no-pages-demo` records it as not run.

Outputs under `--out`: `report.json` (every step with status and details, header values, CSP
violations, console classification, downloads, server stop evidence, `responsive.overflowByView`
and the per-view measurements), `console.log`, `requests.log`,
`csp-violations.json`, `headers.json`, `session.har`, `server.log`, `downloads/` and numbered
screenshots (including the focus-ring and dark-mode captures). Exit code 0 only when every step passed.
