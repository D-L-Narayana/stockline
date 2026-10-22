import { InjectionToken } from '@angular/core';

/**
 * Base URL of the StockLine REST API.
 *
 * - `ng serve` -> `/api`, rewritten to http://localhost:8000 by `proxy.conf.json`.
 * - Production -> set `window.__STOCKLINE_API__` in `index.html` (e.g. "https://api.example.com")
 *   or leave it unset to call the same origin under `/api`.
 */
export const API_BASE = new InjectionToken<string>('API_BASE', {
  providedIn: 'root',
  factory: () => (globalThis as { __STOCKLINE_API__?: string }).__STOCKLINE_API__ ?? '/api',
});
