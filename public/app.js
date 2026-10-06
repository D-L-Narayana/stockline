/* StockLine dashboard — ES module, no framework, no build step.
 *
 * Server mode: relative fetch() calls against the FastAPI app that serves this page.
 * Browser mode (GitHub Pages): when no server answers `health`, the same Python service layer
 * runs inside the tab through Pyodide and app/bridge.py, so every view keeps working offline.
 *
 * Rules that keep the page safe under a strict Content-Security-Policy and free of XSS:
 * every interpolated value goes through esc(); all interaction is delegated from a few root
 * listeners keyed by data-action/data-form/data-change (no inline handlers, no inline styles).
 */
import {
  classifyStatus,
  csvFilename,
  escapeHtml as esc,
  fillDays,
  fmtMs,
  money,
  parseFilename,
  qs,
  sparklinePath,
  toCurl,
} from './lib.js';

// Python modules shipped to the browser. Must equal app.common.BROWSER_MODULES (same order);
// tests/test_static_ui.py, tests/test_parity.py and scripts/build_site.py parse this statement.
const PY_FILES = ["__init__.py", "common.py", "db.py", "schemas.py", "ledger.py", "catalog.py", "inventory.py", "orders.py", "reports.py", "service.py", "seed.py", "bridge.py"];
const PYODIDE_SCRIPT = 'https://cdn.jsdelivr.net/pyodide/v0.27.5/full/pyodide.js';
const VIEWS = ['inventory', 'orders', 'transfers', 'reports', 'catalog', 'scenarios', 'console'];
const MAX_LINES = 5;
const PAGE = 10;
const HISTORY_CAP = 200;

const state = {
  mode: null, // 'server' | 'pyodide'
  handle: null, // bridge.handle_json (browser mode)
  health: null,
  stores: [],
  products: [], // active products, used by every product select
  catalog: [], // products shown in the Catalog view (may include inactive ones)
  inventory: { rows: [], total: 0, ms: null, sort: { key: 'store_code', dir: 1 } },
  orders: { offset: 0, total: 0 },
  drawer: { store: null, product: null, rows: [], label: '', opener: null },
  history: [],
  expanded: new Set(),
  view: 'inventory',
};

let seq = 0;
const nextId = () => {
  seq += 1;
  return seq;
};
const byId = (id) => document.getElementById(id);
const $all = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const tbodyOf = (tableId) => byId(tableId).querySelector('tbody');

// ----------------------------------------------------------------------------- transport
function lowerKeys(obj) {
  const out = {};
  for (const [k, v] of Object.entries(obj || {})) out[String(k).toLowerCase()] = v;
  return out;
}

function parseBody(text, contentType) {
  if (!text) return null;
  if (/json/i.test(contentType || '')) {
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }
  return text;
}

function newEntry(method, rel) {
  return {
    id: nextId(),
    ts: new Date().toISOString(),
    method,
    path: `/${rel}`,
    status: 0,
    ms: null,
    replayed: null,
    body: null,
    requestBody: null,
    headers: {},
    requestHeaders: {},
    ok: false,
    error: null,
  };
}

function record(entry) {
  state.history.unshift(entry);
  if (state.history.length > HISTORY_CAP) state.history.length = HISTORY_CAP;
  if (state.view === 'console') renderConsole();
}

/** One API call for both modes. Returns the history entry: {status, ok, body, headers, replayed, ms, …}. */
async function api(path, opts = {}) {
  const method = String(opts.method || 'GET').toUpperCase();
  const headers = { ...(opts.headers || {}) };
  let body = opts.body;
  if (body !== undefined && body !== null && typeof body !== 'string') body = JSON.stringify(body);
  if (body !== undefined && body !== null && !Object.keys(headers).some((k) => k.toLowerCase() === 'content-type')) {
    headers['Content-Type'] = 'application/json';
  }
  const rel = String(path).replace(/^\/+/, '');
  const entry = newEntry(method, rel);
  entry.requestBody = body === undefined ? null : body;
  entry.requestHeaders = headers;
  const t0 = performance.now();
  try {
    if (state.mode === 'pyodide') {
      const res = JSON.parse(state.handle(method, `/${rel}`, body === undefined ? null : body, JSON.stringify(headers)));
      entry.status = res.status;
      entry.headers = lowerKeys(res.headers);
      entry.body = res.body;
      entry.ms = performance.now() - t0;
    } else {
      const r = await fetch(rel, { method, headers, body: body === undefined ? undefined : body, cache: 'no-store' });
      entry.status = r.status;
      r.headers.forEach((v, k) => {
        entry.headers[k.toLowerCase()] = v;
      });
      entry.body = parseBody(await r.text(), entry.headers['content-type']);
      const served = Number(entry.headers['x-response-time-ms']);
      entry.ms = Number.isFinite(served) && served > 0 ? served : performance.now() - t0;
    }
  } catch (err) {
    entry.error = String((err && err.message) || err);
    entry.body = { detail: entry.error, code: 'network_error' };
    entry.ms = performance.now() - t0;
  }
  entry.replayed = entry.headers['idempotent-replayed'] === undefined ? null : entry.headers['idempotent-replayed'];
  entry.ok = entry.status >= 200 && entry.status < 300;
  record(entry);
  return entry;
}

/** CSV export: fetch → check status → blob → object URL → programmatic <a download> click (browser mode wraps the bridge body). */
async function download(path, stem, outId) {
  const rel = String(path).replace(/^\/+/, '');
  const entry = newEntry('GET', rel);
  entry.csv = true;
  const t0 = performance.now();
  let text = '';
  try {
    if (state.mode === 'pyodide') {
      const res = JSON.parse(state.handle('GET', `/${rel}`, null, '{}'));
      entry.status = res.status;
      entry.headers = lowerKeys(res.headers);
      text = typeof res.body === 'string' ? res.body : JSON.stringify(res.body);
    } else {
      const r = await fetch(rel, { cache: 'no-store' });
      entry.status = r.status;
      r.headers.forEach((v, k) => {
        entry.headers[k.toLowerCase()] = v;
      });
      const blob = await r.blob();
      text = await blob.text();
    }
  } catch (err) {
    entry.error = String((err && err.message) || err);
  }
  entry.ms = performance.now() - t0;
  entry.ok = entry.status >= 200 && entry.status < 300;
  const lines = text.split(/\r?\n/).filter((line) => line !== '');
  const rows = entry.headers['x-row-count'] !== undefined ? Number(entry.headers['x-row-count']) : Math.max(0, lines.length - 1);
  entry.rowCount = rows;
  if (entry.ok) {
    const preview = lines.slice(0, 25).join('\n');
    entry.body = `${rows} data rows${entry.headers['x-truncated'] ? ' (truncated by the server row cap)' : ''}\n${preview}${lines.length > 25 ? '\n…' : ''}`;
  } else {
    entry.body = entry.error ? { detail: entry.error, code: 'network_error' } : parseBody(text, entry.headers['content-type']);
  }
  record(entry);
  if (!entry.ok) {
    if (outId) showResult(outId, entry);
    notify(errorText(entry), 'danger');
    return entry;
  }
  const name = parseFilename(entry.headers['content-disposition'], csvFilename(stem));
  const url = URL.createObjectURL(new Blob([text], { type: 'text/csv' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
  notify(`Downloaded ${name} · ${rows} rows`);
  return entry;
}

// ----------------------------------------------------------------------------- mode detection
function setModeText(text) {
  byId('mode').textContent = text;
}

async function detect() {
  const t0 = performance.now();
  try {
    const r = await fetch('health', { cache: 'no-store' });
    const contentType = r.headers.get('content-type') || '';
    const body = parseBody(await r.text(), contentType);
    const entry = newEntry('GET', 'health');
    entry.status = r.status;
    entry.body = body;
    entry.ms = performance.now() - t0;
    entry.ok = r.ok;
    record(entry);
    // Any JSON answer means an API server owns this origin: stay in server mode even when health is degraded,
    // so the CDN runtime is only ever loaded on static hosting (where `health` is a 404 HTML page or unreachable).
    if (body && typeof body === 'object') {
      state.mode = 'server';
      state.health = r.ok && body.status === 'ok' ? body : null;
      if (!state.health) notify(`Server health check answered HTTP ${r.status}`, 'warn');
      return;
    }
  } catch {
    // No server behind this origin (static hosting): fall back to the in-browser runtime below.
  }
  await bootPyodide();
}

async function bootPyodide() {
  setModeText('Browser mode — no server found; loading the Python runtime (Pyodide, about 10 MB)…');
  await new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = PYODIDE_SCRIPT;
    script.addEventListener('load', resolve, { once: true });
    script.addEventListener('error', () => reject(new Error('could not load Pyodide from the CDN')), { once: true });
    document.head.appendChild(script);
  });
  const py = await window.loadPyodide({ indexURL: 'https://cdn.jsdelivr.net/pyodide/v0.27.5/full/' });
  setModeText('Browser mode — loading pydantic and sqlite3…');
  await py.loadPackage(['pydantic', 'sqlite3']);
  py.FS.mkdir('/app');
  for (const name of PY_FILES) {
    const r = await fetch(`app/${name}`, { cache: 'no-store' });
    if (!r.ok) throw new Error(`could not fetch app/${name} (HTTP ${r.status})`);
    py.FS.writeFile(`/app/${name}`, await r.text());
  }
  py.runPython(['import os, sys', "os.environ['STOCKLINE_DB'] = '/stockline.db'", "sys.path.insert(0, '/')", 'from app import bridge'].join('\n'));
  state.handle = py.runPython('bridge.handle_json');
  state.mode = 'pyodide';
  const health = await api('health');
  state.health = health.ok && health.body && typeof health.body === 'object' ? health.body : null;
}

function renderMode() {
  const el = byId('mode');
  const h = state.health || {};
  if (state.mode === 'server') {
    el.innerHTML = `Server mode — connected to the FastAPI API${h.version ? ` v${esc(h.version)}` : ''}${
      h.schema_version !== undefined ? ` · schema v${esc(h.schema_version)}` : ''
    }`;
  } else {
    el.innerHTML =
      `Browser mode — no server found, so the same Python service layer${h.version ? ` (v${esc(h.version)})` : ''} ` +
      'runs <b>in this tab</b> via Pyodide: SQLite ledger, idempotency and optimistic locking included. State resets on reload. ' +
      'Run <code>uvicorn app.main:app</code> for the real FastAPI server.';
    byId('docs-link').href = 'https://github.com/D-L-Narayana/stockline#api';
  }
}

// ----------------------------------------------------------------------------- rendering helpers
function detailText(detail) {
  if (detail === null || detail === undefined) return '';
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (d && typeof d === 'object' ? `${(d.loc || []).join('.')}: ${d.msg || JSON.stringify(d)}` : String(d)))
      .join('; ');
  }
  return JSON.stringify(detail);
}

/** "HTTP <status> · <code> · <detail>" (code only when the body carries one). */
function errorText(res) {
  const body = res.body && typeof res.body === 'object' ? res.body : {};
  const detail = detailText(body.detail) || res.error || (typeof res.body === 'string' ? res.body.slice(0, 200) : '') || 'request failed';
  return [`HTTP ${res.status || 0}`, body.code, detail].filter(Boolean).join(' · ');
}

function pretty(body) {
  if (body === null || body === undefined) return '—';
  let text;
  if (typeof body === 'string') {
    try {
      text = JSON.stringify(JSON.parse(body), null, 1);
    } catch {
      text = body;
    }
  } else {
    text = JSON.stringify(body, null, 1);
  }
  return text.length > 20000 ? `${text.slice(0, 20000)}\n…` : text;
}

function httpBadge(status) {
  return `<span class="badge badge-${classifyStatus(status)}">HTTP ${esc(status || 'ERR')}</span>`;
}

function resultMarkup(res) {
  const bits = [httpBadge(res.status)];
  if (res.replayed !== null && res.replayed !== undefined) {
    bits.push(`<span class="badge badge-${res.replayed === 'true' ? 'info' : 'ok'}">Idempotent-Replayed: ${esc(res.replayed)}</span>`);
  }
  bits.push(`<span class="muted">${esc(fmtMs(res.ms))}</span>`);
  bits.push(`<span class="muted">${esc(res.method)} ${esc(res.path)}</span>`);
  const error = res.ok ? '' : `<p class="error-line">${esc(errorText(res))}</p>`;
  return `<div class="status">${bits.join(' ')}</div>${error}<pre class="out">${esc(pretty(res.body))}</pre>`;
}

function showResult(id, res) {
  const el = byId(id);
  if (!el) return;
  el.innerHTML = resultMarkup(res);
  delete el.dataset.loadError;
}

/** Errors from list loaders are marked so a later successful reload clears them without wiping action results. */
function showLoadError(id, res) {
  const el = byId(id);
  if (!el) return;
  el.innerHTML = resultMarkup(res);
  el.dataset.loadError = '1';
}

function clearLoadError(id) {
  const el = byId(id);
  if (el && el.dataset.loadError) {
    el.innerHTML = '';
    delete el.dataset.loadError;
  }
}

function orderBadge(status) {
  const tone = status === 'fulfilled' ? 'ok' : status === 'cancelled' ? 'warn' : 'info';
  return `<span class="badge badge-${tone}">${esc(status)}</span>`;
}

function fmtTime(iso) {
  if (!iso) return '—';
  const s = String(iso);
  return s.length >= 19 ? `${s.slice(0, 10)} ${s.slice(11, 19)}Z` : s;
}

function emptyRow(cols, text) {
  return `<tr><td colspan="${esc(cols)}" class="muted">${esc(text)}</td></tr>`;
}

function storeCode(id) {
  const store = state.stores.find((s) => s.id === Number(id));
  return store ? store.code : String(id);
}

function productSku(id) {
  const product = state.products.find((p) => p.id === Number(id)) || state.catalog.find((p) => p.id === Number(id));
  return product ? product.sku : String(id);
}

function compare(a, b) {
  if (a === null || a === undefined) return b === null || b === undefined ? 0 : 1;
  if (b === null || b === undefined) return -1;
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return String(a).localeCompare(String(b), 'en', { numeric: true });
}

let toastTimer = null;
function notify(text, tone = 'ok') {
  const el = byId('toast');
  el.textContent = text;
  el.className = `toast toast-${tone}`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
  }, 4500);
}

function freshKey(prefix) {
  const random = window.crypto && typeof crypto.randomUUID === 'function' ? crypto.randomUUID().slice(0, 8) : Math.random().toString(36).slice(2, 10);
  return `${prefix}-${Date.now().toString(36)}-${random}`;
}

// ----------------------------------------------------------------------------- base data (stores, products, selects)
function fillSelect(sel, items, label, blank) {
  if (!sel) return;
  const previous = sel.value;
  const options = items.map((item) => `<option value="${esc(item.id)}">${esc(label(item))}</option>`);
  if (blank !== undefined) options.unshift(`<option value="">${esc(blank)}</option>`);
  sel.innerHTML = options.join('');
  if (previous && Array.from(sel.options).some((o) => o.value === previous)) sel.value = previous;
}

function productOptions(selected) {
  return state.products
    .map((p) => `<option value="${esc(p.id)}"${String(p.id) === String(selected) ? ' selected' : ''}>${esc(p.sku)} — ${esc(money(p.price_cents))}</option>`)
    .join('');
}

async function loadBase() {
  const [storesRes, productsRes] = await Promise.all([api('stores'), api('products?limit=100')]);
  state.stores = storesRes.ok && Array.isArray(storesRes.body) ? storesRes.body : [];
  const items = productsRes.ok && productsRes.body && Array.isArray(productsRes.body.items) ? productsRes.body.items : [];
  state.products = items.filter((p) => p.active !== false);
  const code = (s) => s.code;
  for (const id of ['receipt-store', 'adjust-store', 'order-store', 'transfer-from', 'transfer-to']) fillSelect(byId(id), state.stores, code);
  for (const id of ['inv-store', 'order-filter-store', 'reorder-store']) fillSelect(byId(id), state.stores, code, 'all');
  const sku = (p) => `${p.sku} — ${money(p.price_cents)}`;
  for (const id of ['adjust-product', 'transfer-product']) fillSelect(byId(id), state.products, sku);
  const to = byId('transfer-to');
  if (to.options.length > 1 && to.value === byId('transfer-from').value) to.selectedIndex = 1;
  for (const id of ['receipt-lines', 'order-lines']) ensureLines(byId(id));
  renderStores();
  if (!storesRes.ok) showLoadError('inv-out', storesRes);
  else if (!productsRes.ok) showLoadError('inv-out', productsRes);
}

// ----------------------------------------------------------------------------- multi-line forms
function lineMarkup(kind, n) {
  return `<div class="line" data-line>
    <select aria-label="${esc(kind)} line ${esc(n)} product" data-role="product">${productOptions()}</select>
    <input type="number" min="1" max="1000" value="1" aria-label="${esc(kind)} line ${esc(n)} quantity" data-role="qty" required>
    <button type="button" class="link" data-action="remove-line">remove</button>
  </div>`;
}

function ensureLines(container) {
  if (!container) return;
  const lines = $all('[data-line]', container);
  if (!lines.length) {
    container.insertAdjacentHTML('beforeend', lineMarkup(container.dataset.lines, 1));
    return;
  }
  for (const line of lines) {
    const sel = line.querySelector('[data-role=product]');
    sel.innerHTML = productOptions(sel.value);
  }
}

function renumber(container) {
  const kind = container.dataset.lines;
  $all('[data-line]', container).forEach((line, i) => {
    line.querySelector('[data-role=product]').setAttribute('aria-label', `${kind} line ${i + 1} product`);
    line.querySelector('[data-role=qty]').setAttribute('aria-label', `${kind} line ${i + 1} quantity`);
  });
}

function addLine(container) {
  const n = $all('[data-line]', container).length;
  if (n >= MAX_LINES) {
    notify(`At most ${MAX_LINES} lines per request`, 'warn');
    return;
  }
  container.insertAdjacentHTML('beforeend', lineMarkup(container.dataset.lines, n + 1));
  const added = $all('[data-line] [data-role=product]', container).pop();
  if (added) added.focus();
}

function removeLine(button) {
  const line = button.closest('[data-line]');
  const container = line.parentElement;
  if ($all('[data-line]', container).length <= 1) {
    notify('A request needs at least one line', 'warn');
    return;
  }
  line.remove();
  renumber(container);
}

function readLines(container) {
  return $all('[data-line]', container)
    .map((line) => ({ product_id: Number(line.querySelector('[data-role=product]').value), quantity: Number(line.querySelector('[data-role=qty]').value) }))
    .filter((l) => l.product_id && l.quantity > 0);
}

// ----------------------------------------------------------------------------- inventory view
function invFilters() {
  return { store_id: byId('inv-store').value, low_stock: byId('inv-low').checked ? 'true' : '', q: byId('inv-q').value.trim() };
}

function enrichRow(row) {
  const product = state.products.find((p) => p.id === row.product_id);
  const price = row.price_cents !== undefined && row.price_cents !== null ? row.price_cents : product ? product.price_cents : null;
  const value = row.value_cents !== undefined && row.value_cents !== null ? row.value_cents : price === null ? null : price * row.on_hand;
  return { ...row, price_cents: price, value_cents: value };
}

async function loadInventory() {
  const f = invFilters();
  const res = await api(`inventory${qs({ limit: 200, store_id: f.store_id, low_stock: f.low_stock, q: f.q })}`);
  if (!res.ok || !res.body || !Array.isArray(res.body.items)) {
    state.inventory.rows = [];
    state.inventory.total = 0;
    state.inventory.ms = res.ms;
    renderInventory();
    showLoadError('inv-out', res);
    return res;
  }
  clearLoadError('inv-out');
  let rows = res.body.items;
  if (f.q) {
    // Also filter locally so the search works against servers that ignore the q parameter.
    const needle = f.q.toLowerCase();
    rows = rows.filter((r) => String(r.sku).toLowerCase().includes(needle) || String(r.name).toLowerCase().includes(needle));
  }
  state.inventory.rows = rows.map(enrichRow);
  state.inventory.total = f.q ? rows.length : res.body.total;
  state.inventory.ms = res.ms;
  renderInventory();
  return res;
}

function renderInventory() {
  const { rows, sort, total, ms } = state.inventory;
  const sorted = [...rows].sort((a, b) => compare(a[sort.key], b[sort.key]) * sort.dir);
  for (const th of $all('#inventory-table th[aria-sort]')) {
    const button = th.querySelector('[data-key]');
    th.setAttribute('aria-sort', button && button.dataset.key === sort.key ? (sort.dir > 0 ? 'ascending' : 'descending') : 'none');
  }
  tbodyOf('inventory-table').innerHTML =
    sorted
      .map(
        (r) => `<tr class="clickable" data-action="movements" data-store="${esc(r.store_id)}" data-product="${esc(r.product_id)}">
      <td>${esc(r.store_code)}</td><td>${esc(r.sku)}</td><td class="wrap">${esc(r.name)}</td>
      <td class="num${r.below_reorder ? ' low' : ''}">${esc(r.on_hand)}</td><td class="num">${esc(r.reorder_point)}</td>
      <td class="num">${esc(money(r.price_cents))}</td><td class="num">${esc(money(r.value_cents))}</td><td class="num">${esc(r.version)}</td>
      <td><button type="button" class="link" data-action="movements" data-store="${esc(r.store_id)}" data-product="${esc(r.product_id)}">ledger</button></td>
    </tr>`,
      )
      .join('') || emptyRow(9, 'no inventory rows match');
  byId('inv-meta').textContent = `(${total} rows · ${fmtMs(ms)})`;
}

function closeDrawer() {
  const drawer = byId('drawer');
  if (drawer.hidden) return;
  drawer.hidden = true;
  const opener = state.drawer.opener;
  if (opener && opener.isConnected && typeof opener.focus === 'function') opener.focus();
}

async function openDrawer(store, product, append = false, opener = null) {
  const d = state.drawer;
  if (!append) {
    const row = state.inventory.rows.find((r) => r.store_id === store && r.product_id === product);
    d.store = store;
    d.product = product;
    d.rows = [];
    d.opener = opener;
    d.label = row ? `${row.store_code} · ${row.sku} — ${row.name} (on hand ${row.on_hand}, version ${row.version})` : `store ${store} · product ${product}`;
  }
  const before = append && d.rows.length ? d.rows[d.rows.length - 1].id : null;
  const res = await api(`inventory/${store}/${product}/movements${qs({ limit: 50, before_id: before })}`);
  const drawer = byId('drawer');
  const wasHidden = drawer.hidden;
  drawer.hidden = false;
  byId('drawer-sub').textContent = d.label;
  if (res.ok && Array.isArray(res.body)) {
    const known = new Set(d.rows.map((m) => m.id));
    const fresh = res.body.filter((m) => !known.has(m.id));
    d.rows = d.rows.concat(fresh);
    byId('drawer-meta').textContent = fresh.length ? `${d.rows.length} movements · ${fmtMs(res.ms)}` : 'no older movements';
  } else {
    byId('drawer-meta').textContent = errorText(res);
  }
  renderDrawer();
  if (wasHidden) byId('drawer-close').focus();
}

function renderDrawer() {
  tbodyOf('drawer-table').innerHTML =
    state.drawer.rows
      .map(
        (m) => `<tr><td>${esc(m.id)}</td><td class="num${m.delta < 0 ? ' low' : ''}">${esc(m.delta > 0 ? `+${m.delta}` : m.delta)}</td>
      <td>${esc(m.reason)}</td><td class="wrap">${esc(m.reference === null || m.reference === undefined ? '' : m.reference)}</td>
      <td class="num">${esc(m.balance_after === null || m.balance_after === undefined ? '—' : m.balance_after)}</td><td>${esc(fmtTime(m.created_at))}</td></tr>`,
      )
      .join('') || emptyRow(6, 'no movements');
}

async function submitReceipt() {
  const store = byId('receipt-store').value;
  const reference = byId('receipt-ref').value.trim();
  const lines = readLines(byId('receipt-lines'));
  if (!lines.length) {
    notify('Add at least one line', 'warn');
    return;
  }
  const res = await api(`inventory/${store}/receipts`, { method: 'POST', body: { reference, lines } });
  showResult('receipt-out', res);
  if (res.ok) {
    notify(`Receipt ${reference} posted`);
    await loadInventory();
  }
}

async function submitAdjust() {
  const body = { delta: Number(byId('adjust-delta').value), reason: byId('adjust-reason').value };
  const version = byId('adjust-version').value;
  if (version !== '') body.expected_version = Number(version);
  const res = await api(`inventory/${byId('adjust-store').value}/${byId('adjust-product').value}/adjust`, { method: 'POST', body });
  showResult('adjust-out', res);
  if (res.ok) await loadInventory();
}

async function fillVersion() {
  const res = await api(`inventory/${byId('adjust-store').value}/${byId('adjust-product').value}`);
  if (res.ok && res.body && typeof res.body === 'object') {
    byId('adjust-version').value = res.body.version;
    notify(`Current version is ${res.body.version} (on hand ${res.body.on_hand})`);
  } else {
    showResult('adjust-out', res);
  }
}

// ----------------------------------------------------------------------------- orders view
function orderFilters() {
  return { status: byId('order-status').value, store_id: byId('order-filter-store').value };
}

async function loadOrders() {
  const f = orderFilters();
  const o = state.orders;
  const res = await api(`orders${qs({ limit: PAGE, offset: o.offset, status: f.status, store_id: f.store_id })}`);
  if (!res.ok || !res.body || !Array.isArray(res.body.items)) {
    tbodyOf('orders-table').innerHTML = '';
    showLoadError('orders-out', res);
    return res;
  }
  clearLoadError('orders-out');
  o.total = Number(res.body.total) || 0;
  const items = res.body.items;
  tbodyOf('orders-table').innerHTML =
    items
      .map((ord) => {
        const lines = (ord.lines || []).map((l) => `${l.sku}×${l.quantity} @ ${money(l.unit_price_cents)}`).join(', ');
        const actions =
          ord.status === 'placed'
            ? `<button type="button" class="link" data-action="order-cancel" data-id="${esc(ord.id)}">cancel</button>
               <button type="button" class="link" data-action="order-fulfil" data-id="${esc(ord.id)}">fulfil</button>`
            : '';
        return `<tr><td>${esc(ord.id)}</td><td>${esc(storeCode(ord.store_id))}</td><td>${orderBadge(ord.status)}</td>
        <td class="num">${esc(money(ord.total_cents))}</td><td class="wrap">${esc(lines)}</td><td class="wrap">${esc(ord.idempotency_key || '')}</td>
        <td>${esc(fmtTime(ord.created_at))}</td><td>${esc(ord.updated_at ? fmtTime(ord.updated_at) : '—')}</td><td>${actions}</td></tr>`;
      })
      .join('') || emptyRow(9, 'no orders match');
  const from = o.total ? o.offset + 1 : 0;
  const to = Math.min(o.offset + PAGE, o.total);
  byId('orders-page').textContent = `${from}–${to} of ${o.total}`;
  byId('orders-prev').disabled = o.offset <= 0;
  byId('orders-next').disabled = o.offset + PAGE >= o.total;
  byId('orders-meta').textContent = `(${o.total} orders · ${fmtMs(res.ms)})`;
  return res;
}

async function submitOrder() {
  const lines = readLines(byId('order-lines'));
  if (!lines.length) {
    notify('Add at least one line', 'warn');
    return;
  }
  const key = byId('order-key').value.trim();
  const res = await api('orders', { method: 'POST', headers: key ? { 'Idempotency-Key': key } : {}, body: { store_id: Number(byId('order-store').value), lines } });
  showResult('order-out', res);
  if (res.ok) {
    state.orders.offset = 0;
    await Promise.all([loadOrders(), loadInventory()]);
  }
}

async function orderAction(id, verb) {
  const res = await api(`orders/${id}/${verb}`, { method: 'POST' });
  showResult('orders-out', res);
  await loadOrders();
  if (res.ok) await loadInventory();
}

// ----------------------------------------------------------------------------- transfers view
async function submitTransfer() {
  const key = byId('transfer-key').value.trim();
  const body = {
    from_store_id: Number(byId('transfer-from').value),
    to_store_id: Number(byId('transfer-to').value),
    product_id: Number(byId('transfer-product').value),
    quantity: Number(byId('transfer-qty').value),
  };
  const res = await api('transfers', { method: 'POST', headers: key ? { 'Idempotency-Key': key } : {}, body });
  showResult('transfer-out', res);
  if (res.ok) await Promise.all([loadTransfers(), loadInventory()]);
}

async function loadTransfers() {
  const res = await api('transfers?limit=20');
  if (!res.ok || !res.body || !Array.isArray(res.body.items)) {
    tbodyOf('transfers-table').innerHTML = '';
    byId('transfers-meta').textContent = '';
    showLoadError('transfers-out', res);
    return res;
  }
  clearLoadError('transfers-out');
  const items = res.body.items;
  tbodyOf('transfers-table').innerHTML =
    items
      .map(
        (t) => `<tr><td>${esc(t.id)}</td><td>${esc(storeCode(t.from_store_id))} → ${esc(storeCode(t.to_store_id))}</td>
      <td>${esc(t.from && t.from.sku ? t.from.sku : productSku(t.product_id))}</td><td class="num">${esc(t.quantity)}</td>
      <td class="wrap">${esc(t.idempotency_key || '')}</td><td>${esc(fmtTime(t.created_at))}</td></tr>`,
      )
      .join('') || emptyRow(6, 'no transfers yet');
  byId('transfers-meta').textContent = `(${res.body.total !== undefined ? res.body.total : items.length} transfers · ${fmtMs(res.ms)})`;
  return res;
}

// ----------------------------------------------------------------------------- reports view
function kpi(label, value) {
  return `<div class="kpi"><b>${esc(value === null || value === undefined ? '—' : value)}</b><span>${esc(label)}</span></div>`;
}

async function loadSummary() {
  const res = await api('reports/summary');
  if (!res.ok || !res.body || typeof res.body !== 'object') {
    byId('summary-cards').innerHTML = '';
    tbodyOf('summary-table').innerHTML = '';
    byId('summary-meta').textContent = '';
    showLoadError('summary-out', res);
    return res;
  }
  clearLoadError('summary-out');
  const b = res.body;
  const totals = b.totals || {};
  const orders = b.orders || {};
  const revenue = b.revenue_cents || {};
  const products = b.products || {};
  const cents = (v) => (v === null || v === undefined ? null : money(v));
  byId('summary-cards').innerHTML = [
    kpi('SKUs stocked', totals.skus),
    kpi('Units on hand', totals.units),
    kpi('Stock value', cents(totals.value_cents)),
    kpi('Low-stock lines', totals.low_stock),
    kpi('Orders placed', orders.placed),
    kpi('Orders fulfilled', orders.fulfilled),
    kpi('Orders cancelled', orders.cancelled),
    kpi('Revenue (placed)', cents(revenue.placed)),
    kpi('Revenue (fulfilled)', cents(revenue.fulfilled)),
    kpi('Products active / inactive', products.active === undefined ? null : `${products.active} / ${products.inactive === undefined ? 0 : products.inactive}`),
  ].join('');
  // The per-store list is looked up by its likely names, then by shape (an array of objects carrying store_code).
  const isStoreList = (v) => Array.isArray(v) && v.length > 0 && v[0] !== null && typeof v[0] === 'object' && 'store_code' in v[0];
  const stores = ['stores', 'per_store', 'by_store'].map((k) => b[k]).find(Array.isArray) || Object.values(b).find(isStoreList) || [];
  tbodyOf('summary-table').innerHTML =
    stores
      .map((s) => {
        const pct = s.skus ? Math.round(100 * (1 - (Number(s.low_stock) || 0) / s.skus)) : 0;
        return `<tr><td>${esc(s.store_code)}</td><td class="wrap">${esc(s.store_name)}</td><td class="num">${esc(s.skus)}</td><td class="num">${esc(s.units)}</td>
        <td class="num">${esc(money(s.value_cents))}</td><td class="num${s.low_stock ? ' low' : ''}">${esc(s.low_stock)}</td>
        <td><div class="meter" role="img" aria-label="${esc(pct)}% of SKUs above their reorder point"><i data-pct="${esc(pct)}"></i></div></td></tr>`;
      })
      .join('') || emptyRow(7, 'no per-store rows in the summary');
  // Dynamic widths go through the CSSOM, which the CSP allows (inline style attributes would be blocked).
  for (const bar of $all('#summary-table .meter i')) bar.style.width = `${Math.max(0, Math.min(100, Number(bar.dataset.pct) || 0))}%`;
  byId('summary-meta').textContent = `(${b.generated_at ? `generated ${fmtTime(b.generated_at)} · ` : ''}${fmtMs(res.ms)})`;
  return res;
}

const dash = (v) => (v === null || v === undefined ? '—' : v);

async function loadReorder() {
  const days = byId('reorder-days').value;
  const store = byId('reorder-store').value;
  const res = await api(`reports/reorder${qs({ days, store_id: store })}`);
  if (!res.ok || !Array.isArray(res.body)) {
    tbodyOf('reorder-table').innerHTML = '';
    byId('reorder-meta').textContent = '';
    showLoadError('reorder-out', res);
    return res;
  }
  clearLoadError('reorder-out');
  const rows = res.body;
  tbodyOf('reorder-table').innerHTML =
    rows
      .map(
        (r) => `<tr><td>${esc(r.store_code)}</td><td>${esc(r.sku)}</td><td class="wrap">${esc(r.name)}</td><td class="num low">${esc(r.on_hand)}</td>
      <td class="num">${esc(r.reorder_point)}</td><td class="num">${esc(dash(r.sold_window !== undefined ? r.sold_window : r.sold_30d))}</td>
      <td class="num">${esc(dash(r.returned_window))}</td><td class="num">${esc(dash(r.net_sold))}</td><td class="num">${esc(dash(r.daily_velocity))}</td>
      <td class="num">${esc(dash(r.days_of_cover))}</td><td class="num">${esc(r.suggested_qty)}</td></tr>`,
      )
      .join('') || emptyRow(11, 'nothing is at or below its reorder point');
  byId('reorder-meta').textContent = `(${rows.length} SKUs · ${days}-day window · ${fmtMs(res.ms)})`;
  return res;
}

function renderSalesChart(series, days) {
  const W = 600;
  const H = 180;
  const top = 18;
  const bottom = 24;
  const left = 8;
  const right = 8;
  const plotH = H - top - bottom;
  const plotW = W - left - right;
  const n = Math.max(series.length, 1);
  const slot = plotW / n;
  const barW = Math.max(1, slot - 2);
  const revenues = series.map((d) => Number(d.revenue_cents) || 0);
  const peak = Math.max(0, ...revenues);
  const scale = peak || 1;
  const bars = series
    .map((d, i) => {
      const v = revenues[i];
      const h = Math.round(((v / scale) * plotH) * 100) / 100;
      const x = Math.round((left + i * slot) * 100) / 100;
      const y = Math.round((top + plotH - h) * 100) / 100;
      return `<rect class="bar" x="${esc(x)}" y="${esc(y)}" width="${esc(Math.round(barW * 100) / 100)}" height="${esc(h)}"><title>${esc(d.day)}: ${esc(money(v))} · ${esc(d.orders || 0)} orders · ${esc(d.units || 0)} units</title></rect>`;
    })
    .join('');
  const line = sparklinePath(series.map((d) => Number(d.units) || 0), plotW - barW, plotH);
  const first = series.length ? series[0].day : '';
  const last = series.length ? series[series.length - 1].day : '';
  byId('sales-chart').innerHTML = `<svg class="spark" viewBox="0 0 ${W} ${H}" role="img" aria-label="Revenue per day as bars and units sold as a line over the last ${esc(days)} days">
    <line class="grid" x1="${left}" y1="${top + plotH}" x2="${W - right}" y2="${top + plotH}"></line>
    ${bars}
    <g transform="translate(${esc(Math.round((left + barW / 2) * 100) / 100)} ${top})"><path class="trend" d="${esc(line)}"></path></g>
    <text class="axis" x="${left}" y="${top - 6}">peak day ${esc(money(peak))}</text>
    <text class="axis" x="${left}" y="${H - 6}">${esc(first)}</text>
    <text class="axis" x="${W - right}" y="${H - 6}" text-anchor="end">${esc(last)}</text>
  </svg>`;
}

async function loadSales() {
  const days = Number(byId('sales-days').value) || 30;
  const [byDay, byProduct] = await Promise.all([api(`reports/sales${qs({ days, group_by: 'day' })}`), api(`reports/sales${qs({ days, group_by: 'product' })}`)]);
  if (!byDay.ok || !Array.isArray(byDay.body)) {
    byId('sales-chart').innerHTML = '';
    byId('sales-meta').textContent = '';
    showLoadError('sales-out', byDay);
  } else {
    clearLoadError('sales-out');
    const series = fillDays(byDay.body, days);
    renderSalesChart(series, days);
    const orders = series.reduce((sum, d) => sum + (Number(d.orders) || 0), 0);
    const revenue = series.reduce((sum, d) => sum + (Number(d.revenue_cents) || 0), 0);
    byId('sales-meta').textContent = `(${orders} orders · ${money(revenue)} · last ${days} days)`;
  }
  const rows = byProduct.ok && Array.isArray(byProduct.body) ? byProduct.body : [];
  tbodyOf('sales-products-table').innerHTML =
    rows
      .map(
        (r) => `<tr><td>${esc(r.sku)}</td><td class="wrap">${esc(r.name)}</td><td class="num">${esc(r.orders)}</td><td class="num">${esc(r.units)}</td><td class="num">${esc(money(r.revenue_cents))}</td></tr>`,
      )
      .join('') || emptyRow(5, byProduct.ok ? 'no sales in this window' : errorText(byProduct));
}

function integrityMarkup(res) {
  if (!res.ok || !res.body || typeof res.body !== 'object') return resultMarkup(res);
  const b = res.body;
  const ok = b.ok === true;
  const counts = ['mismatches', 'negative', 'chain_breaks', 'order_total_mismatches']
    .filter((k) => Array.isArray(b[k]))
    .map((k) => `${k.replace(/_/g, ' ')} ${b[k].length}`)
    .join(' · ');
  const head = `<div class="status"><span class="badge badge-${ok ? 'ok' : 'danger'}">${ok ? 'ledger consistent' : 'ledger inconsistent'}</span>
    <span class="muted">checked ${esc(b.checked)}${b.schema_version !== undefined ? ` · schema v${esc(b.schema_version)}` : ''}${counts ? ` · ${esc(counts)}` : ''}</span></div>`;
  return head + resultMarkup(res);
}

async function integrity() {
  const res = await api('integrity');
  byId('integrity-out').innerHTML = integrityMarkup(res);
  return res;
}

async function rebuild() {
  const res = await api('integrity/rebuild', { method: 'POST' });
  byId('integrity-out').innerHTML = resultMarkup(res);
  if (res.ok) await loadInventory();
  return res;
}

// ----------------------------------------------------------------------------- catalog view
async function loadProducts() {
  const q = byId('catalog-q').value.trim();
  const inactive = byId('catalog-inactive').checked;
  const res = await api(`products${qs({ limit: 100, q, include_inactive: inactive ? 'true' : '' })}`);
  if (!res.ok || !res.body || !Array.isArray(res.body.items)) {
    state.catalog = [];
    renderProducts();
    showLoadError('products-out', res);
    return res;
  }
  clearLoadError('products-out');
  state.catalog = res.body.items;
  renderProducts();
  byId('products-meta').textContent = `(${res.body.total} products · ${fmtMs(res.ms)})`;
  return res;
}

function productRow(p) {
  const inactive = p.active === false;
  const toggle = inactive
    ? `<button type="button" class="link" data-action="product-activate" data-id="${esc(p.id)}">activate</button>`
    : `<button type="button" class="link" data-action="product-deactivate" data-id="${esc(p.id)}">deactivate</button>`;
  return `<tr data-id="${esc(p.id)}"><td>${esc(p.id)}</td><td>${esc(p.sku)}</td><td class="wrap">${esc(p.name)}</td><td>${esc(p.category)}</td>
    <td class="num">${esc(money(p.price_cents))}</td><td class="num">${esc(p.reorder_point)}</td>
    <td>${inactive ? '<span class="badge badge-warn">inactive</span>' : '<span class="badge badge-ok">active</span>'}</td>
    <td><button type="button" class="link" data-action="product-edit" data-id="${esc(p.id)}">edit</button> ${toggle}</td></tr>`;
}

function productEditRow(p) {
  return `<tr class="editing" data-id="${esc(p.id)}"><td>${esc(p.id)}</td><td>${esc(p.sku)}</td>
    <td><input aria-label="Name of ${esc(p.sku)}" data-field="name" maxlength="120" value="${esc(p.name)}"></td>
    <td><input aria-label="Category of ${esc(p.sku)}" data-field="category" maxlength="40" value="${esc(p.category)}"></td>
    <td><input aria-label="Price in cents of ${esc(p.sku)}" data-field="price_cents" type="number" min="0" value="${esc(p.price_cents)}"></td>
    <td><input aria-label="Reorder point of ${esc(p.sku)}" data-field="reorder_point" type="number" min="0" value="${esc(p.reorder_point)}"></td>
    <td><label><input type="checkbox" data-field="active"${p.active === false ? '' : ' checked'}> active</label></td>
    <td><button type="button" class="link" data-action="product-save" data-id="${esc(p.id)}">save</button>
        <button type="button" class="link" data-action="product-cancel" data-id="${esc(p.id)}">cancel</button></td></tr>`;
}

function renderProducts() {
  tbodyOf('products-table').innerHTML = state.catalog.map(productRow).join('') || emptyRow(8, 'no products match');
}

function rowFor(tableId, id) {
  return $all(`#${tableId} tbody tr[data-id]`).find((tr) => tr.dataset.id === String(id));
}

function readPatch(tr, current, fields) {
  const patch = {};
  for (const input of $all('[data-field]', tr)) {
    const field = input.dataset.field;
    if (!fields.includes(field)) continue;
    let value;
    if (input.type === 'checkbox') value = input.checked;
    else if (input.type === 'number') value = Number(input.value);
    else value = input.value.trim();
    const before = field === 'active' ? current.active !== false : current[field];
    if (value !== before) patch[field] = value;
  }
  return patch;
}

async function saveProduct(id) {
  const p = state.catalog.find((x) => x.id === Number(id));
  const tr = rowFor('products-table', id);
  if (!p || !tr) return;
  const patch = readPatch(tr, p, ['name', 'category', 'price_cents', 'reorder_point', 'active']);
  if (!Object.keys(patch).length) {
    notify('No changes to save', 'warn');
    return;
  }
  const res = await api(`products/${id}`, { method: 'PATCH', body: patch });
  showResult('products-out', res);
  if (res.ok) await Promise.all([loadProducts(), loadBase()]);
}

async function setProductActive(id, active) {
  const res = active ? await api(`products/${id}`, { method: 'PATCH', body: { active: true } }) : await api(`products/${id}`, { method: 'DELETE' });
  showResult('products-out', res);
  if (res.ok) {
    notify(active ? `Product ${id} activated` : `Product ${id} deactivated (soft delete, HTTP ${res.status})`);
    await Promise.all([loadProducts(), loadBase()]);
  }
}

async function submitProduct() {
  const body = {
    sku: byId('product-sku').value.trim(),
    name: byId('product-name').value.trim(),
    category: byId('product-category').value.trim(),
    price_cents: Number(byId('product-price').value),
    reorder_point: Number(byId('product-reorder').value),
  };
  const res = await api('products', { method: 'POST', body });
  showResult('product-out', res);
  if (res.ok) {
    byId('product-sku').value = '';
    byId('product-name').value = '';
    await Promise.all([loadProducts(), loadBase()]);
  }
}

function storeRow(s) {
  return `<tr data-id="${esc(s.id)}"><td>${esc(s.id)}</td><td>${esc(s.code)}</td><td class="wrap">${esc(s.name)}</td><td>${esc(s.region)}</td>
    <td><button type="button" class="link" data-action="store-edit" data-id="${esc(s.id)}">edit</button></td></tr>`;
}

function storeEditRow(s) {
  return `<tr class="editing" data-id="${esc(s.id)}"><td>${esc(s.id)}</td><td>${esc(s.code)}</td>
    <td><input aria-label="Name of store ${esc(s.code)}" data-field="name" maxlength="80" value="${esc(s.name)}"></td>
    <td><input aria-label="Region of store ${esc(s.code)}" data-field="region" maxlength="40" value="${esc(s.region)}"></td>
    <td><button type="button" class="link" data-action="store-save" data-id="${esc(s.id)}">save</button>
        <button type="button" class="link" data-action="store-cancel" data-id="${esc(s.id)}">cancel</button></td></tr>`;
}

function renderStores() {
  tbodyOf('stores-table').innerHTML = state.stores.map(storeRow).join('') || emptyRow(5, 'no stores');
}

async function saveStore(id) {
  const s = state.stores.find((x) => x.id === Number(id));
  const tr = rowFor('stores-table', id);
  if (!s || !tr) return;
  const patch = readPatch(tr, s, ['name', 'region']);
  if (!Object.keys(patch).length) {
    notify('No changes to save', 'warn');
    return;
  }
  const res = await api(`stores/${id}`, { method: 'PATCH', body: patch });
  showResult('stores-out', res);
  if (res.ok) await loadBase();
  else renderStores();
}

async function submitStore() {
  const body = { code: byId('store-code').value.trim(), name: byId('store-name').value.trim(), region: byId('store-region').value.trim() };
  const res = await api('stores', { method: 'POST', body });
  showResult('stores-out', res);
  if (res.ok) {
    byId('store-code').value = '';
    byId('store-name').value = '';
    await loadBase();
  }
}

// ----------------------------------------------------------------------------- scenarios
function expectStatus(res, statuses, code) {
  const bodyCode = res.body && typeof res.body === 'object' ? res.body.code : undefined;
  const statusOk = statuses.includes(res.status);
  const codeOk = !code || bodyCode === undefined || bodyCode === code;
  let actual = res.ok ? `HTTP ${res.status}${res.replayed !== null && res.replayed !== undefined ? ` · Idempotent-Replayed: ${res.replayed}` : ''}` : errorText(res);
  if (code && statusOk && bodyCode === undefined) actual += ' (no error code in body)';
  return { pass: statusOk && codeOk, actual };
}

async function pickStock(t, minOnHand) {
  const res = await t.step('Load inventory', '200 with stocked rows', () => api('inventory?limit=200'), (r) => {
    const rows = r.ok && r.body && Array.isArray(r.body.items) ? r.body.items : [];
    return { pass: rows.some((x) => x.on_hand >= minOnHand && x.on_hand < 1000), actual: r.ok ? `${rows.length} rows` : errorText(r) };
  });
  const rows = res.ok && res.body && Array.isArray(res.body.items) ? res.body.items : [];
  const row = rows.filter((x) => x.on_hand >= minOnHand && x.on_hand < 1000).sort((a, b) => b.on_hand - a.on_hand)[0];
  if (!row) {
    t.fail('Pick a stocked product', 'no inventory row with enough stock');
    return null;
  }
  const other = state.stores.find((s) => s.id !== row.store_id);
  const otherRow = rows.find((x) => x.store_id !== row.store_id);
  const otherStore = other ? other.id : otherRow ? otherRow.store_id : null;
  return { store: row.store_id, product: row.product_id, row, otherStore };
}

const SCENARIOS = {
  oversell: async (t) => {
    const pick = await pickStock(t, 0);
    if (!pick) return;
    const { store, product, row } = pick;
    const qty = row.on_hand + 1;
    await t.step(
      `Order ${qty} × ${row.sku} at ${row.store_code} (on hand ${row.on_hand})`,
      '409 insufficient_stock',
      () => api('orders', { method: 'POST', body: { store_id: store, lines: [{ product_id: product, quantity: qty }] } }),
      (r) => expectStatus(r, [409], 'insufficient_stock'),
    );
    await t.step(
      'Re-read the inventory row',
      `on_hand still ${row.on_hand}`,
      () => api(`inventory/${store}/${product}`),
      (r) => ({ pass: r.ok && r.body.on_hand === row.on_hand, actual: r.ok ? `on_hand ${r.body.on_hand}, version ${r.body.version}` : errorText(r) }),
    );
  },

  idempotent: async (t) => {
    const pick = await pickStock(t, 3);
    if (!pick) return;
    const { store, product, row } = pick;
    const key = freshKey('ui-replay');
    const body = { store_id: store, lines: [{ product_id: product, quantity: 1 }] };
    const first = await t.step(
      `POST /orders for 1 × ${row.sku} with Idempotency-Key ${key}`,
      '201 created · Idempotent-Replayed: false',
      () => api('orders', { method: 'POST', headers: { 'Idempotency-Key': key }, body }),
      (r) => expectStatus(r, [201]),
    );
    const id = first.ok && first.body ? first.body.id : null;
    await t.step(
      'Retry the identical request',
      `200 · Idempotent-Replayed: true · same order #${id}`,
      () => api('orders', { method: 'POST', headers: { 'Idempotency-Key': key }, body }),
      (r) => ({
        pass: r.status === 200 && r.replayed === 'true' && !!r.body && r.body.id === id,
        actual: r.ok ? `HTTP ${r.status} · Idempotent-Replayed: ${r.replayed} · order #${r.body.id}` : errorText(r),
      }),
    );
    await t.step(
      'Reuse the key with quantity 2',
      '422 idempotency_key_reuse',
      () => api('orders', { method: 'POST', headers: { 'Idempotency-Key': key }, body: { store_id: store, lines: [{ product_id: product, quantity: 2 }] } }),
      (r) => expectStatus(r, [422], 'idempotency_key_reuse'),
    );
    if (id !== null) {
      await t.step(
        `Cancel order #${id} to restock`,
        '200 · status cancelled',
        () => api(`orders/${id}/cancel`, { method: 'POST' }),
        (r) => ({ pass: r.ok && r.body.status === 'cancelled', actual: r.ok ? `HTTP ${r.status} · ${r.body.status}` : errorText(r) }),
      );
    }
  },

  stale: async (t) => {
    const pick = await pickStock(t, 1);
    if (!pick) return;
    const { store, product, row } = pick;
    const path = `inventory/${store}/${product}/adjust`;
    const current = await t.step(
      `Read ${row.store_code}/${row.sku}`,
      '200 with the current version',
      () => api(`inventory/${store}/${product}`),
      (r) => ({ pass: r.ok && Number.isInteger(r.body.version), actual: r.ok ? `version ${r.body.version}, on_hand ${r.body.on_hand}` : errorText(r) }),
    );
    if (!current.ok) return;
    const v = current.body.version;
    await t.step(
      `Adjust +1 with expected_version ${Math.max(0, v - 1)} (stale)`,
      '409 version_conflict',
      () => api(path, { method: 'POST', body: { delta: 1, reason: 'adjustment', expected_version: Math.max(0, v - 1) } }),
      (r) => expectStatus(r, [409], 'version_conflict'),
    );
    const updated = await t.step(
      `Adjust +1 with expected_version ${v}`,
      `200 · version ${v + 1}`,
      () => api(path, { method: 'POST', body: { delta: 1, reason: 'adjustment', expected_version: v } }),
      (r) => ({ pass: r.ok && r.body.version === v + 1, actual: r.ok ? `HTTP ${r.status} · version ${r.body.version}` : errorText(r) }),
    );
    if (updated.ok) {
      await t.step(
        `Undo the +1 with expected_version ${v + 1}`,
        `200 · version ${v + 2}`,
        () => api(path, { method: 'POST', body: { delta: -1, reason: 'adjustment', expected_version: v + 1 } }),
        (r) => ({ pass: r.ok && r.body.version === v + 2, actual: r.ok ? `HTTP ${r.status} · version ${r.body.version}` : errorText(r) }),
      );
    }
  },

  transfer: async (t) => {
    const pick = await pickStock(t, 1);
    if (!pick) return;
    const { store: a, product: p, row, otherStore: b } = pick;
    if (b === null || b === undefined) {
      t.fail('Pick a destination store', 'only one store exists');
      return;
    }
    const readPair = () => Promise.all([api(`inventory/${a}/${p}`), api(`inventory/${b}/${p}`)]);
    const onHand = (r) => (r.ok && r.body ? r.body.on_hand : 0);
    const before = await t.step(
      `Read ${row.sku} at ${row.store_code} and at ${storeCode(b)}`,
      '200 (a 404 on the destination means "no row yet" and counts as 0)',
      readPair,
      ([x, y]) => ({ pass: x.ok && (y.ok || y.status === 404), actual: `${onHand(x)} and ${onHand(y)}` }),
    );
    const onA = onHand(before[0]);
    const onB = onHand(before[1]);
    const body = (quantity) => ({ from_store_id: a, to_store_id: b, product_id: p, quantity });
    await t.step(
      `Transfer ${onA + 1} units (one more than on hand)`,
      '409 insufficient_stock',
      () => api('transfers', { method: 'POST', body: body(onA + 1) }),
      (r) => expectStatus(r, [409], 'insufficient_stock'),
    );
    await t.step('Both sides unchanged', `${onA} and ${onB}`, readPair, ([x, y]) => ({
      pass: onHand(x) === onA && onHand(y) === onB,
      actual: `${onHand(x)} and ${onHand(y)}`,
    }));
    const moved = await t.step(
      'Transfer 1 unit',
      '201 created (200 on the v0.1 server)',
      () => api('transfers', { method: 'POST', body: body(1) }),
      (r) => expectStatus(r, [200, 201]),
    );
    await t.step('Source −1, destination +1', `${onA - 1} and ${onB + 1}`, readPair, ([x, y]) => ({
      pass: onHand(x) === onA - 1 && onHand(y) === onB + 1,
      actual: `${onHand(x)} and ${onHand(y)}`,
    }));
    if (moved.ok) {
      await t.step(
        'Move the unit back',
        '2xx',
        () => api('transfers', { method: 'POST', body: { from_store_id: b, to_store_id: a, product_id: p, quantity: 1 } }),
        (r) => expectStatus(r, [200, 201]),
      );
    }
  },

  audit: async (t) => {
    await t.step('GET /integrity', 'ok: true', () => api('integrity'), (r) => ({
      pass: r.ok && !!r.body && r.body.ok === true,
      actual: r.ok ? `ok ${r.body.ok} · checked ${r.body.checked} · mismatches ${(r.body.mismatches || []).length}` : errorText(r),
    }));
    await t.step('GET /movements?limit=10 (global audit feed)', '200 with items', () => api('movements?limit=10'), (r) => ({
      pass: r.ok && !!r.body && Array.isArray(r.body.items),
      actual: r.ok && r.body && Array.isArray(r.body.items) ? `${r.body.items.length} movements · next_before_id ${dash(r.body.next_before_id)}` : errorText(r),
    }));
    const pick = await pickStock(t, 0);
    if (!pick) return;
    const { store, product, row } = pick;
    await t.step(`Last movements for ${row.store_code}/${row.sku}`, '200 with balance_after', () => api(`inventory/${store}/${product}/movements?limit=5`), (r) => ({
      pass: r.ok && Array.isArray(r.body) && r.body.length > 0,
      actual: r.ok && Array.isArray(r.body) ? `${r.body.length} rows · latest balance_after ${r.body[0] ? dash(r.body[0].balance_after) : '—'}` : errorText(r),
    }));
  },
};

function renderScenario(name, steps, status) {
  const el = byId(`scenario-${name}`);
  if (!el) return;
  const badge =
    status === 'running'
      ? '<span class="badge badge-info">running…</span>'
      : status === 'pass'
        ? '<span class="badge badge-ok">pass</span>'
        : '<span class="badge badge-danger">fail</span>';
  const passed = steps.filter((s) => s.pass).length;
  el.innerHTML = `<div class="status">${badge} <span class="muted">${esc(passed)}/${esc(steps.length)} steps passed</span></div>
    <ol class="steps">${steps
      .map(
        (s, i) => `<li class="step">
        <div class="status"><span class="badge badge-${s.pass ? 'ok' : 'danger'}">${s.pass ? 'pass' : 'fail'}</span> <strong>${esc(i + 1)}. ${esc(s.title)}</strong></div>
        <div class="kv"><span>expected</span><span>${esc(s.expected)}</span><span>actual</span><span>${esc(s.actual)}</span></div>
        ${
          s.responses.length
            ? `<details><summary>raw response${s.responses.length > 1 ? 's' : ''}</summary>${s.responses
                .map((r) => `<div class="muted">${esc(r.method)} ${esc(r.path)} → ${esc(r.status)} · ${esc(fmtMs(r.ms))}</div><pre class="out">${esc(pretty(r.body))}</pre>`)
                .join('')}</details>`
            : ''
        }
      </li>`,
      )
      .join('')}</ol>`;
}

async function runScenario(name) {
  const run = SCENARIOS[name];
  if (!run) return;
  const steps = [];
  let status = 'running';
  const draw = () => renderScenario(name, steps, status);
  const t = {
    step: async (title, expected, request, judge) => {
      let result;
      let responses = [];
      try {
        result = await request();
        responses = Array.isArray(result) ? result : [result];
        const verdict = judge(result);
        steps.push({ title, expected, actual: verdict.actual, pass: Boolean(verdict.pass), responses });
      } catch (err) {
        steps.push({ title, expected, actual: `error: ${(err && err.message) || err}`, pass: false, responses });
        result = Array.isArray(result) ? result : responses[0] || { ok: false, status: 0, body: null };
      }
      draw();
      return result;
    },
    fail: (title, actual) => {
      steps.push({ title, expected: '—', actual, pass: false, responses: [] });
      draw();
    },
  };
  const button = document.querySelector(`[data-action="run-scenario"][data-scenario="${name}"]`);
  if (button) button.disabled = true;
  draw();
  try {
    await run(t);
  } catch (err) {
    t.fail('Unexpected error', String((err && err.message) || err));
  }
  status = steps.length && steps.every((s) => s.pass) ? 'pass' : 'fail';
  draw();
  if (button) button.disabled = false;
  await loadInventory();
}

// ----------------------------------------------------------------------------- console view
function curlBase() {
  if (state.mode === 'pyodide') return 'http://localhost:8000';
  return new URL('.', window.location.href).href.replace(/\/$/, '');
}

function renderConsole() {
  byId('console-meta').textContent = `(${state.history.length} requests)`;
  tbodyOf('console-table').innerHTML =
    state.history
      .map((e) => {
        const open = state.expanded.has(e.id);
        const main = `<tr><td>${esc(e.ts.slice(11, 23))}</td><td>${esc(e.method)}</td><td class="wrap">${esc(e.path)}</td>
        <td><span class="badge badge-${classifyStatus(e.status)}">${esc(e.status || 'ERR')}</span></td><td class="num">${esc(fmtMs(e.ms))}</td>
        <td>${esc(e.replayed === null || e.replayed === undefined ? '' : e.replayed)}</td>
        <td><button type="button" class="link" data-action="console-toggle" data-id="${esc(e.id)}" aria-expanded="${open ? 'true' : 'false'}">${open ? 'hide' : 'details'}</button>
            <button type="button" class="link" data-action="console-curl" data-id="${esc(e.id)}">copy as curl</button></td></tr>`;
        if (!open) return main;
        return `${main}<tr class="details"><td colspan="7"><div class="kv">
          <span>request headers</span><span>${esc(JSON.stringify(e.requestHeaders))}</span>
          <span>request body</span><span><pre class="out">${esc(pretty(e.requestBody))}</pre></span>
          <span>response headers</span><span>${esc(JSON.stringify(e.headers))}</span>
          <span>${e.csv ? 'csv preview' : 'response body'}</span><span><pre class="out">${esc(pretty(e.body))}</pre></span>
          ${e.curl ? `<span>curl</span><span><pre class="out">${esc(e.curl)}</pre></span>` : ''}
        </div></td></tr>`;
      })
      .join('') || emptyRow(7, 'no requests yet');
}

function copyCurl(id) {
  const entry = state.history.find((e) => e.id === id);
  if (!entry) return;
  const cmd = toCurl({ method: entry.method, url: entry.path, headers: entry.requestHeaders, body: entry.requestBody, base: curlBase() });
  entry.curl = cmd;
  state.expanded.add(entry.id);
  const pre = byId('console-curl');
  pre.textContent = cmd;
  pre.hidden = false;
  renderConsole();
  const fallback = () => notify('Clipboard unavailable — the curl command is printed in the console', 'warn');
  if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    navigator.clipboard.writeText(cmd).then(() => notify('curl command copied to the clipboard'), fallback);
  } else {
    fallback();
  }
}

function clearConsole() {
  state.history = [];
  state.expanded.clear();
  const pre = byId('console-curl');
  pre.textContent = '';
  pre.hidden = true;
  renderConsole();
}

// ----------------------------------------------------------------------------- views & events
const loaders = {
  inventory: () => loadInventory(),
  orders: () => loadOrders(),
  transfers: () => loadTransfers(),
  reports: () => Promise.all([loadSummary(), loadReorder(), loadSales()]),
  catalog: () => {
    renderStores();
    return loadProducts();
  },
  scenarios: () => undefined,
  console: () => renderConsole(),
};

function showView(name) {
  if (!VIEWS.includes(name)) return;
  for (const tab of $all('[role=tab]')) {
    const selected = tab.dataset.view === name;
    tab.setAttribute('aria-selected', selected ? 'true' : 'false');
    tab.tabIndex = selected ? 0 : -1;
  }
  for (const panel of $all('[role=tabpanel]')) panel.hidden = panel.id !== `view-${name}`;
  state.view = name;
  if (window.location.hash !== `#${name}`) window.history.replaceState(null, '', `#${name}`);
  guard(() => loaders[name]());
}

function guard(fn) {
  return Promise.resolve()
    .then(fn)
    .catch((err) => notify(`Something went wrong: ${(err && err.message) || err}`, 'danger'));
}

const timers = new Map();
function debounce(key, fn, ms = 250) {
  clearTimeout(timers.get(key));
  timers.set(key, setTimeout(fn, ms));
}

const actions = {
  tab: (el) => showView(el.dataset.view),
  'inv-refresh': () => loadInventory(),
  'inv-sort': (el) => {
    const sort = state.inventory.sort;
    if (sort.key === el.dataset.key) sort.dir = -sort.dir;
    else {
      sort.key = el.dataset.key;
      sort.dir = 1;
    }
    renderInventory();
  },
  movements: (el) => openDrawer(Number(el.dataset.store), Number(el.dataset.product), false, el.tagName === 'BUTTON' ? el : el.querySelector('button') || el),
  'movements-older': () => (state.drawer.store === null ? undefined : openDrawer(state.drawer.store, state.drawer.product, true)),
  'drawer-close': () => closeDrawer(),
  'export-inventory': () => {
    const f = invFilters();
    return download(`inventory/export.csv${qs({ store_id: f.store_id, low_stock: f.low_stock, q: f.q })}`, 'inventory', 'inv-out');
  },
  'export-movements': () => download(`movements/export.csv${qs({ store_id: state.drawer.store, product_id: state.drawer.product })}`, 'movements', 'inv-out'),
  'add-line': (el) => addLine(byId(el.dataset.target)),
  'remove-line': (el) => removeLine(el),
  'adjust-fill-version': () => fillVersion(),
  'generate-key': (el) => {
    const input = byId(el.dataset.target);
    if (input) {
      input.value = freshKey('ui');
      input.focus();
    }
  },
  'orders-refresh': () => loadOrders(),
  'orders-prev': () => {
    state.orders.offset = Math.max(0, state.orders.offset - PAGE);
    return loadOrders();
  },
  'orders-next': () => {
    if (state.orders.offset + PAGE < state.orders.total) state.orders.offset += PAGE;
    return loadOrders();
  },
  'export-orders': () => {
    const f = orderFilters();
    return download(`orders/export.csv${qs({ store_id: f.store_id, status: f.status })}`, 'orders', 'orders-out');
  },
  'order-cancel': (el) => orderAction(el.dataset.id, 'cancel'),
  'order-fulfil': (el) => orderAction(el.dataset.id, 'fulfil'),
  'transfers-refresh': () => loadTransfers(),
  'load-summary': () => loadSummary(),
  'load-reorder': () => loadReorder(),
  'load-sales': () => loadSales(),
  'export-reorder': () => download(`reports/reorder.csv${qs({ days: byId('reorder-days').value, store_id: byId('reorder-store').value })}`, 'reorder', 'reorder-out'),
  integrity: () => integrity(),
  'integrity-rebuild': () => rebuild(),
  'products-refresh': () => loadProducts(),
  'product-edit': (el) => {
    const p = state.catalog.find((x) => x.id === Number(el.dataset.id));
    const tr = rowFor('products-table', el.dataset.id);
    if (p && tr) {
      tr.outerHTML = productEditRow(p);
      rowFor('products-table', el.dataset.id).querySelector('input').focus();
    }
  },
  'product-cancel': () => renderProducts(),
  'product-save': (el) => saveProduct(el.dataset.id),
  'product-deactivate': (el) => setProductActive(el.dataset.id, false),
  'product-activate': (el) => setProductActive(el.dataset.id, true),
  'store-edit': (el) => {
    const s = state.stores.find((x) => x.id === Number(el.dataset.id));
    const tr = rowFor('stores-table', el.dataset.id);
    if (s && tr) {
      tr.outerHTML = storeEditRow(s);
      rowFor('stores-table', el.dataset.id).querySelector('input').focus();
    }
  },
  'store-cancel': () => renderStores(),
  'store-save': (el) => saveStore(el.dataset.id),
  'run-scenario': (el) => runScenario(el.dataset.scenario),
  'run-all-scenarios': async () => {
    for (const name of Object.keys(SCENARIOS)) await runScenario(name);
  },
  'console-toggle': (el) => {
    const id = Number(el.dataset.id);
    if (state.expanded.has(id)) state.expanded.delete(id);
    else state.expanded.add(id);
    renderConsole();
  },
  'console-curl': (el) => copyCurl(Number(el.dataset.id)),
  'console-clear': () => clearConsole(),
};

const forms = { receipt: submitReceipt, adjust: submitAdjust, order: submitOrder, transfer: submitTransfer, product: submitProduct, store: submitStore };

const changes = {
  inventory: () => loadInventory(),
  orders: () => {
    state.orders.offset = 0;
    return loadOrders();
  },
  reorder: () => loadReorder(),
  sales: () => loadSales(),
  products: () => loadProducts(),
};

function onTabKey(event, tab) {
  const tabs = $all('[role=tab]');
  let i = tabs.indexOf(tab);
  if (event.key === 'ArrowRight') i = (i + 1) % tabs.length;
  else if (event.key === 'ArrowLeft') i = (i - 1 + tabs.length) % tabs.length;
  else if (event.key === 'Home') i = 0;
  else if (event.key === 'End') i = tabs.length - 1;
  else return;
  event.preventDefault();
  tabs[i].focus();
  showView(tabs[i].dataset.view);
}

function wire() {
  document.body.addEventListener('click', (event) => {
    const el = event.target.closest('[data-action]');
    if (!el) return;
    const fn = actions[el.dataset.action];
    if (!fn) return;
    if (el.tagName === 'A') event.preventDefault();
    guard(() => fn(el, event));
  });
  document.body.addEventListener('submit', (event) => {
    const form = event.target.closest('form[data-form]');
    if (!form) return;
    event.preventDefault();
    const fn = forms[form.dataset.form];
    if (fn) guard(() => fn(form));
  });
  document.body.addEventListener('change', (event) => {
    const el = event.target.closest('[data-change]');
    if (!el) return;
    const fn = changes[el.dataset.change];
    if (fn) guard(() => fn(el));
  });
  document.body.addEventListener('input', (event) => {
    const el = event.target.closest('[data-input]');
    if (!el) return;
    const fn = changes[el.dataset.input];
    if (fn) debounce(el.dataset.input, () => guard(() => fn(el)));
  });
  document.body.addEventListener('keydown', (event) => {
    const tab = event.target.closest('[role=tab]');
    if (tab) onTabKey(event, tab);
    if (event.key === 'Escape') closeDrawer();
  });
  window.addEventListener('hashchange', () => {
    const name = window.location.hash.slice(1);
    if (VIEWS.includes(name) && name !== state.view) showView(name);
  });
}

async function init() {
  wire();
  for (const id of ['receipt-lines', 'order-lines']) ensureLines(byId(id));
  renderConsole();
  await detect();
  renderMode();
  await loadBase();
  const initial = window.location.hash.slice(1);
  showView(VIEWS.includes(initial) ? initial : 'inventory');
}

init().catch((err) => {
  const message = String((err && err.message) || err);
  setModeText(`Startup failed: ${message}`);
  notify(`Startup failed: ${message}`, 'danger');
});
