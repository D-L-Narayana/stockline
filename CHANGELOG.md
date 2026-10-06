# Changelog

All notable changes to StockLine are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] — unreleased

This release was produced with automated coding assistance and had not been independently
human-reviewed at the time of writing.

### Added
- Versioned schema migrations (`PRAGMA user_version`); existing v0.1 database files migrate in
  place on first start. New columns: `orders.request_hash`, `orders.updated_at`,
  `products.updated_at`, `stock_movements.balance_after`; new `transfers` table and indexes.
- Running balance (`balance_after`) on every stock movement; `GET /integrity` now also checks
  the balance chain, negative balances and order totals; `POST /integrity/rebuild` repairs the
  cached balances from the ledger.
- Transfers are first-class records: `POST /transfers` (idempotent via `Idempotency-Key`),
  `GET /transfers`, `GET /transfers/{id}`.
- Multi-line receipts: `POST /inventory/{store_id}/receipts` (all-or-nothing).
- Global movement feed: `GET /movements` with store/product/reason/since filters and keyset
  paging.
- Catalogue lifecycle: `GET /stores/{id}`, `PATCH /stores/{id}`, `PATCH /products/{id}`,
  `DELETE /products/{id}` (soft delete), `GET /products/{id}/inventory`,
  `GET /products?include_inactive=`.
- Reports: `GET /reports/reorder?days=&store_id=` (net of cancellations, daily velocity, days
  of cover), `GET /reports/summary`, `GET /reports/sales?group_by=day|product`.
- CSV exports: `/inventory/export.csv`, `/movements/export.csv`, `/orders/export.csv`,
  `/reports/reorder.csv` (formula-injection safe, row cap with `X-Truncated`).
- Operations: structured access logs (text or JSON) with request ids, `GET /metrics`
  (Prometheus text format), `GET /health` with schema version and uptime, graceful pool
  shutdown, 503 with `Retry-After` when the pool is exhausted.
- Security: strict Content-Security-Policy and security headers on every response, optional
  API key for mutating requests (`STOCKLINE_API_KEY`), optional CORS
  (`STOCKLINE_CORS_ORIGINS`), optional HSTS.
- Dashboard rewritten as a CSP-clean, framework-free app: inventory with search/sort and a
  movements drawer, receipts, order composer with idempotency keys, transfers, reports with
  charts, catalogue admin, guided scenarios, a request console with "copy as curl", CSV
  downloads, dark mode and keyboard support.
- Developer tooling: `scripts/dev.py` task runner, `scripts/build_site.py` (tested static-site
  builder, single source of truth for the demo module list), `scripts/smoke.sh`,
  `scripts/bench.py`, `scripts/browser_check.mjs` (Playwright: asserts the served security
  headers and Content-Security-Policy, drives the dashboard workflows and CSV downloads under
  the enforced policy while recording policy violations and foreign requests, and boots the
  Pyodide demo from the built static site in a separately labelled stage), `docker-compose.yml`,
  `Makefile`, non-root Docker image, CI matrix 3.11–3.13 with lint and coverage gates.
- Documentation: `docs/architecture.md`, `docs/migrations.md`, `docs/operations.md`,
  `docs/concurrency.md`.

### Changed
- Error bodies carry a machine-readable `code` (and `request_id` on the server).
- `POST /transfers` returns 201 on creation and includes the transfer record
  (`id`, `from_store_id`, `to_store_id`, `product_id`, `quantity`, `idempotency_key`,
  `created_at`) alongside the existing `from` / `to` inventory rows.
- `GET /reports/reorder` rows use `sold_window` instead of `sold_30d` and gain `days`,
  `returned_window`, `net_sold`, `daily_velocity`, `days_of_cover`, `store_id`, `product_id`.
- Ordering an inactive product now returns 409 `product_inactive` (was 404).
- The browser bridge returns 405 for a known path with the wrong method (was 404) and
  enforces the same query bounds as the HTTP API.
- Seed data is richer and deterministic: back-dated orders over 45 days, cancellations,
  fulfilments and transfers, so reports show real velocity.

### Fixed
- Idempotent order replay is now race-safe (the key is re-checked inside the write
  transaction) and a reused key with a different body is rejected with 422.
- Product names, SKUs and idempotency keys are HTML-escaped in the dashboard.
- `GET /orders` no longer issues one query per order.
- A request to a known API path with the wrong method answers `405` with an `Allow` header
  instead of being handled by the dashboard's static mount (which answered `404` for `GET` on
  write-only paths such as `/integrity/rebuild`).
- On narrow viewports (390 px) the dashboard no longer widens the document: the visually hidden
  "Ledger" column header is absolutely positioned and escaped the scrolling table wrapper, adding
  277 px of horizontal page overflow; the wrapper is now its containing block, so tables keep
  scrolling within their own container while the page itself stays within the viewport. The
  browser check gained a responsive stage (390 and 1280 px, every view) that guards this.

## [0.1.0]

Initial release: stores, products, inventory ledger with cached balance, idempotent orders,
optimistic locking on adjustments, store-to-store transfers, reorder report, single-file
dashboard, in-browser demo via Pyodide, Docker image and CI.
