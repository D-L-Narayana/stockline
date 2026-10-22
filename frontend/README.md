# StockLine UI — Angular 21 frontend

Standalone Angular 21 application for the StockLine FastAPI backend in the parent directory.
Zoneless change detection, signals, lazy standalone routes, reactive forms, `HttpClient` with a
functional interceptor — no UI framework, ~74 kB initial transfer.

| Route | What it does | API calls |
| --- | --- | --- |
| `/dashboard` | KPI cards (stock value, low stock, orders, revenue) and per-store table | `GET /reports/summary`, `GET /inventory?low_stock=true` |
| `/products` | Searchable, paginated catalogue; create form mirroring the Pydantic rules; soft delete | `GET/POST /products`, `DELETE /products/{id}` |
| `/inventory` | Store / low-stock / search filters (kept in the URL), CSV export, receive-stock form | `GET /inventory`, `POST /inventory/{store}/receipts`, `GET /inventory/export.csv` |
| `/orders` | Filter by store and status, paginated | `GET /orders` |
| `/orders/new` | `FormArray` of lines, cross-field duplicate check, live total, `Idempotency-Key` per checkout | `POST /orders` |
| `/orders/:id` | Lines, totals, fulfil / cancel actions, replay banner | `GET /orders/{id}`, `POST .../fulfil`, `POST .../cancel` |

## Run

```bash
# terminal 1 — backend with demo data
cd .. && STOCKLINE_SEED=1 uvicorn app.main:app --reload       # http://localhost:8000

# terminal 2 — frontend (proxies /api -> localhost:8000, see proxy.conf.json)
npm install
npm start                                                      # http://localhost:4200
```

```bash
npm test          # Vitest unit tests (services, interceptor, pipe, forms, components)
npm run build     # production bundle in dist/frontend/browser
```

## Structure

```
src/app
├── core/                       # API layer
│   ├── api-base.token.ts       # API_BASE injection token (/api, or window.__STOCKLINE_API__)
│   ├── api.service.ts          # typed HttpClient wrapper: params, Idempotency-Key, replay flag
│   ├── api-error.interceptor.ts# Accept + X-API-Key headers; {detail, code, request_id} -> ApiError
│   ├── catalog|inventory|orders|reports.service.ts
│   └── models.ts               # TS mirrors of app/schemas.py
├── features/                   # one lazy standalone component per route
│   ├── dashboard/ products/ inventory/ orders/
├── shared/                     # MoneyPipe (paise -> ₹), ErrorBanner
├── app.routes.ts               # loadComponent routes with titles
└── app.config.ts               # provideRouter, provideHttpClient(withInterceptors)
```

## Design notes

- **Validation mirrors the backend.** SKU pattern `^[A-Z0-9-]+$`, quantity 1–1000, 1–50 lines, no
  duplicate products — the same rules as `app/schemas.py`, so most mistakes are caught before a round-trip
  and the 422 path is only a fallback (flattened into one message by the interceptor).
- **Idempotent checkout.** The order form generates one `Idempotency-Key` per attempt and keeps it until
  the order succeeds; a retried or double-clicked submit replays the same order (HTTP 200 +
  `Idempotent-Replayed: true`), and the detail page says so.
- **Errors are data.** Every failure reaches components as `ApiError {status, code, detail, requestId}`;
  `insufficient_stock` (409) gets an explanation that nothing was reserved.
- **Pointing at another API.** Set `window.__STOCKLINE_API__` in `index.html`, or enable CORS on the
  backend with `STOCKLINE_CORS_ORIGINS=http://localhost:4200`. An optional API key is stored in
  `localStorage` and sent as `X-API-Key`.
