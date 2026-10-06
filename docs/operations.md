# Operating StockLine

How to configure, run, observe and protect the API server (`app.main:app`). The browser demo on
GitHub Pages runs the same service layer without a server, so nothing here applies to it except
the Content-Security-Policy notes in `docs/architecture.md`.

## Configuration (environment variables)

All settings are read once at start-up (`app/deps.py`, `Settings.from_env`). Malformed numbers and
unknown choices fall back to the default instead of failing the start.

| Variable | Default | Meaning |
|---|---|---|
| `STOCKLINE_DB` | `./stockline.db` (repository root) | SQLite database file. Created on first start; an existing v0.1 file is migrated in place (schema version 2, additive columns only). The container image sets `/data/stockline.db`. |
| `STOCKLINE_SEED` | `1` | `1`/`true`/`yes`: load the deterministic demo dataset (3 stores, 12 products, back-dated orders) into an *empty* database. Never touches a database that already has stores. |
| `STOCKLINE_POOL_SIZE` | `8` | Connections in the in-process SQLite pool (minimum 1). One request holds one connection for its whole lifetime. |
| `STOCKLINE_POOL_TIMEOUT` | `10` | Seconds a request waits for a free connection before answering `503 pool_exhausted` with `Retry-After: 1`. |
| `STOCKLINE_API_KEY` | unset | When set, every `POST`/`PUT`/`PATCH`/`DELETE` must present the key (see *API key*). Unset = all writes open (demo default). |
| `STOCKLINE_CORS_ORIGINS` | unset | Comma-separated list of origins allowed to call the API from a browser on another site (see *CORS*). Unset = no CORS headers at all. |
| `STOCKLINE_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` (case-insensitive) for the `stockline` logger tree. |
| `STOCKLINE_LOG_FORMAT` | `text` | `text` (one readable line) or `json` (one JSON object per line). The container image defaults to `json`. |
| `STOCKLINE_CSP` | strict default policy | Verbatim replacement for the `Content-Security-Policy` value served on every response except the API docs. Use only if you embed the dashboard somewhere that needs extra sources; the default is what the shipped UI is tested against. |
| `STOCKLINE_HSTS` | `0` | `1`: add `Strict-Transport-Security: max-age=31536000; includeSubDomains`. Enable only when the service (and every subdomain of its host) is served over TLS. |
| `PORT` | `8000` | Container image only: the port uvicorn binds inside the container. |

Developer tooling (`scripts/dev.py`, `Makefile`) additionally honours `STOCKLINE_PYTHON` to pick the
interpreter that runs the tests and the server; it has no effect on the application itself.

## Running

```bash
pip install -r requirements.txt                 # or requirements-dev.txt for tests/lint
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers
```

**One uvicorn worker per database file — always.** Writers serialise on SQLite's file lock through
`BEGIN IMMEDIATE`, the connection pool is an in-process object, and `/metrics` is an in-process
registry. A second worker process would open a second pool (and a second metrics registry) on the
same file: correctness holds because every write still runs in an exclusive SQLite transaction, but
`/metrics` would only ever show the worker that happened to answer. Scale reads by giving the single
process a bigger pool (`STOCKLINE_POOL_SIZE`), not by adding processes.

`python3 scripts/dev.py serve --reload` (or `make run`) starts the same server for development.

### Behind TLS / a reverse proxy

Terminate TLS at a reverse proxy (nginx, Caddy, Traefik, a cloud load balancer) and forward to the
single uvicorn process over the loopback or a private network:

* keep `--proxy-headers` (the container image sets it) and restrict it to your proxy with
  `--forwarded-allow-ips <proxy address>` so the `client` field of the access log shows the real
  client address instead of the proxy;
* if the service lives under a path prefix, start uvicorn with `--root-path /prefix`; the security
  middleware recognises the documentation pages relative to that prefix;
* set `STOCKLINE_HSTS=1` once every request reaches the proxy over HTTPS;
* do **not** add a second `Content-Security-Policy` at the proxy — browsers enforce *all* policies
  they receive, so an extra one can only break the dashboard; override the application policy with
  `STOCKLINE_CSP` if you really need a different one;
* let the proxy set `X-Request-ID` if it already has a correlation id: the application keeps a
  well-formed inbound value (`^[A-Za-z0-9._-]{1,64}$`) and otherwise mints its own.

## Health semantics

`GET /health` executes `SELECT 1` on a pooled connection and reports the schema version:

```json
{"status": "ok", "version": "0.2.0", "schema_version": 2, "runtime": "server", "uptime_s": 12.3}
```

| Result | Meaning | What to do |
|---|---|---|
| `200` with `schema_version: 2` | database reachable and migrated; the process is ready | nothing |
| `503` with `code: "pool_exhausted"` and `Retry-After: 1` | every pooled connection was busy for `STOCKLINE_POOL_TIMEOUT` seconds | back off and retry; if it persists, requests are blocking on long transactions or the pool is too small |
| `500` with `code: "internal"` | the database query failed (file unreadable, disk full, …) | read the error line in the log (it carries the `request_id` and a traceback) |

The container `HEALTHCHECK` and the compose file poll `/health` every 30 s. `uptime_s` counts from
process start (one decimal place); `runtime` is `"server"` here and `"pyodide"` in the browser demo.

`GET /integrity` audits the ledger (cached balances vs. the sum of movements, running-balance chain,
order totals) and `POST /integrity/rebuild` repairs the cache from the ledger. The rebuild is a write,
so it requires the API key whenever one is configured.

## Response headers

Every response — JSON, CSV exports, static dashboard files, 401/404/422 errors and the JSON 500s —
carries the following headers (`app/security.py`, applied by the outermost middleware):

| Header | Value |
|---|---|
| `Content-Security-Policy` | `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'` (or `STOCKLINE_CSP`) |
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `no-referrer` |
| `X-Frame-Options` | `DENY` |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=()` |
| `Cross-Origin-Opener-Policy` | `same-origin` |
| `Cross-Origin-Resource-Policy` | `same-origin` |
| `Cache-Control` | `no-store` — only on `application/json` and `text/csv` responses |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` — only with `STOCKLINE_HSTS=1` |
| `X-Request-ID` | the request id (inbound value if well-formed, otherwise generated) |
| `X-Response-Time-ms` | server-side processing time, one decimal place |

Why these values:

* The dashboard is written to run under the strict policy: no inline scripts or styles, no remote
  fonts or images, same-origin requests only. `object-src 'none'`, `base-uri 'self'`,
  `frame-ancestors 'none'` (plus the legacy `X-Frame-Options: DENY`) and `form-action 'self'` close
  the usual injection and framing paths.
* `Cache-Control: no-store` keeps stock levels, orders and exports out of browser and proxy caches;
  static assets keep their normal caching (`ETag`/`Last-Modified`).
* The cross-origin isolation headers stop other origins from embedding or reading the responses.
* A route may set its own `Content-Security-Policy`; the middleware never overwrites a policy that is
  already present on that response.

**Why `/docs` differs.** Swagger UI (`/docs`, `/docs/oauth2-redirect`) and ReDoc (`/redoc`) are
third-party pages that load their JavaScript and CSS from `cdn.jsdelivr.net`, use inline bootstrap
script/style and show the FastAPI logo from `fastapi.tiangolo.com`. Those three paths therefore get
a dedicated policy, regardless of `STOCKLINE_CSP`:

```
default-src 'self'; script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; img-src 'self' data: https://fastapi.tiangolo.com https://cdn.jsdelivr.net; font-src 'self' https://cdn.jsdelivr.net data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'
```

Nothing else — in particular the dashboard and the JSON API — is ever allowed to talk to the CDN.

## Error bodies

Errors are JSON with a stable shape so clients and log pipelines can branch on `code`:

```json
{"detail": "order 999 not found", "code": "not_found", "request_id": "0f3a9c2b7d1e"}
```

Operational codes: `not_found` (404), `method_not_allowed` (405, with an `Allow` header),
`validation_error` (422, `detail` is pydantic's error list), `unauthorized` (401),
`pool_exhausted` (503, `Retry-After: 1`) and `internal` (500, detail is always `"internal error"`;
the traceback is only in the log). Domain codes (`insufficient_stock`, `version_conflict`,
`idempotency_key_reuse`, `product_inactive`, …) are listed in the README and `docs/architecture.md`.

## Logging

`observability.setup_logging(level, fmt)` configures the `stockline` logger tree once with a single
stderr handler (`stockline` does not propagate to the root logger, so lines are never duplicated by
another logging configuration). Two loggers matter:

* `stockline.access` — one line per request after the response is complete (`INFO`, or `WARNING`
  for 5xx). Fields: `ts` (UTC, millisecond precision), `level`, `request_id`, `method`, `path`
  (never the query string, which may contain idempotency keys), `status`, `duration_ms`, `client`
  (`host:port` as seen by uvicorn, so enable `--proxy-headers` behind a proxy).
* `stockline` — application events: unhandled exceptions with their traceback (`ERROR`) and pool
  exhaustion (`WARNING`), each tagged with the `request_id`.

`STOCKLINE_LOG_FORMAT=json` (the container default):

```
{"ts": "2026-10-05T12:00:00.123Z", "level": "INFO", "logger": "stockline.access", "msg": "GET /inventory 200 3.4ms client=127.0.0.1:54321", "request_id": "0f3a9c2b7d1e", "method": "GET", "path": "/inventory", "status": 200, "duration_ms": 3.41, "client": "127.0.0.1:54321"}
{"ts": "2026-10-05T12:00:01.456Z", "level": "ERROR", "logger": "stockline", "msg": "unhandled exception during GET /reports/summary", "request_id": "a1b2c3d4e5f6", "exc": "Traceback (most recent call last):\n  ..."}
```

`STOCKLINE_LOG_FORMAT=text`:

```
2026-10-05T12:00:00.123Z INFO stockline.access [0f3a9c2b7d1e] GET /inventory 200 3.4ms client=127.0.0.1:54321
```

Correlate a client-side failure with the server log through the `X-Request-ID` response header and
the `request_id` inside every error body. uvicorn prints its own (unstructured) access log as well;
start it with `--no-access-log` when you only want the structured line.

## API key

Writes are open by default so the demo works out of the box. Set `STOCKLINE_API_KEY` to protect every
mutating method (`POST`, `PUT`, `PATCH`, `DELETE`, including `POST /integrity/rebuild`). Reads
(`GET`, `HEAD`, `OPTIONS`) and `GET /health` stay open; `/metrics` is a read as well — restrict it at
the proxy if the API is reachable from the internet. The key is compared in constant time and may be
presented either way:

```bash
export STOCKLINE_API_KEY='change-me'     # before starting the server

curl -s -X POST http://localhost:8000/orders \
     -H 'X-API-Key: change-me' -H 'Content-Type: application/json' -H 'Idempotency-Key: order-42' \
     -d '{"store_id": 1, "lines": [{"product_id": 1, "quantity": 2}]}'

curl -s -X POST http://localhost:8000/integrity/rebuild -H 'Authorization: Bearer change-me'

curl -s -X POST http://localhost:8000/orders -d '{}' -H 'Content-Type: application/json'
# -> 401 {"detail": "missing or invalid API key", "code": "unauthorized", "request_id": "..."}
#    with WWW-Authenticate: Bearer
```

The dashboard served by the API does not store a key; use it for reads, or run it with the key
unset in trusted environments. Rotate the key by restarting the process with the new value. Always
combine the key with TLS — it travels in a header.

## CORS

`STOCKLINE_CORS_ORIGINS=https://ops.example.com,https://admin.example.com` enables Starlette's
`CORSMiddleware` for exactly those origins (all methods, all request headers, no credentials).
Browsers on those origins can read the custom response headers `Idempotent-Replayed`,
`X-Request-ID`, `X-Response-Time-ms`, `X-Row-Count`, `X-Truncated` and `Content-Disposition`.
Preflight (`OPTIONS`) requests are answered before the API-key check, as the CORS protocol requires.
Leave the variable unset when the dashboard is served by the API itself — same-origin requests need
no CORS.

## Metrics

`GET /metrics` renders the in-process registry in the Prometheus text exposition format
(`Content-Type: text/plain; version=0.0.4; charset=utf-8`). It never opens a database connection.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `stockline_requests_total` | counter | `method`, `path`, `status` | requests by method, path template and status. Numeric path segments are collapsed (`/orders/42/cancel` → `/orders/{id}/cancel`); after 1000 distinct series new paths are counted under `path="other"` |
| `stockline_request_duration_seconds_bucket` | histogram | `le` = `0.005 0.01 0.025 0.05 0.1 0.25 0.5 1.0 +Inf` | cumulative latency buckets (seconds) |
| `stockline_request_duration_seconds_sum` / `_count` | histogram | — | total seconds and number of requests |
| `stockline_up` | gauge | — | always `1` while the process serves |
| `stockline_pool_connections` | gauge | `state` = `created`, `idle` | open pooled connections and how many are waiting in the pool (`created - idle` = in use) |
| `stockline_pool_size` | gauge | — | pool capacity (`STOCKLINE_POOL_SIZE`) |

Example scrape configuration:

```yaml
scrape_configs:
  - job_name: stockline
    static_configs:
      - targets: ["stockline:8000"]
```

Useful alerts: `stockline_pool_connections{state="idle"} == 0` for more than a minute (pool pressure,
precedes `503 pool_exhausted`), a rising rate of `status=~"5.."`, and p95 latency from the histogram.

## Docker and compose

The image (`Dockerfile`) is `python:3.12-slim`, runs as the non-root user `10001`, owns the `/data`
volume, sets `STOCKLINE_DB=/data/stockline.db` and `STOCKLINE_LOG_FORMAT=json`, exposes `PORT`
(default 8000), polls `/health` as its `HEALTHCHECK` and starts exactly one uvicorn worker with
`--proxy-headers`.

```bash
docker compose up --build        # http://localhost:8000 — dashboard, /docs, /health, /metrics
STOCKLINE_API_KEY=change-me STOCKLINE_SEED=0 docker compose up -d
docker compose logs -f stockline # JSON lines with request ids
```

`docker-compose.yml` keeps the database in the named volume `stockline-data` and passes the
`STOCKLINE_*` variables through from your shell or a `.env` file. Notes:

* A database created by v0.1 keeps working: the first start migrates it in place (`schema_version` 2)
  and the old code can still read the file, so rolling back the image is safe.
* Back up the SQLite file with the `sqlite3` command-line tool's `.backup` command run against the
  volume from the host (the slim image does not ship the tool), or stop the container first; the pool
  checkpoints the WAL on shutdown, but copying a live `*.db` without its `-wal` file loses recent writes.
* Keep a single container per volume (see the one-worker rule above).
* Seeded demo data is only loaded into an empty database; set `STOCKLINE_SEED=0` for real deployments
  so an accidentally empty volume does not come up with sample stores.
