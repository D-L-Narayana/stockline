// Pure helpers for the StockLine dashboard.
// No DOM, no network and no globals are touched at import time, so `node --test` can exercise this module directly.

const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;' };

/** Escape a value for safe interpolation into HTML text or attribute content (null/undefined -> ''). */
export function escapeHtml(value) {
  if (value === null || value === undefined) return '';
  return String(value).replace(/[&<>"'`]/g, (c) => HTML_ESCAPES[c]);
}

/** Format integer cents as rupees with en-IN digit grouping and two decimals; missing/invalid -> em dash. */
export function money(cents) {
  if (cents === null || cents === undefined || cents === '') return '—';
  const n = Number(cents);
  if (!Number.isFinite(n)) return '—';
  const amount = Math.abs(n) / 100;
  const text = amount.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${n < 0 ? '-' : ''}₹${text}`;
}

/** Single-quote a string for a POSIX shell. */
function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

/** Build a curl command for a recorded request. `body` may be a string or a JSON-able object. */
export function toCurl({ method = 'GET', url = '', headers = {}, body = null, base = '' } = {}) {
  const verb = String(method || 'GET').toUpperCase();
  const target = base ? `${String(base).replace(/\/+$/, '')}/${String(url).replace(/^\/+/, '')}` : String(url);
  const parts = ['curl'];
  if (verb !== 'GET') parts.push('-X', verb);
  parts.push(shellQuote(target));
  for (const [name, value] of Object.entries(headers || {})) {
    if (value === null || value === undefined || value === '') continue;
    parts.push('-H', shellQuote(`${name}: ${value}`));
  }
  if (body !== null && body !== undefined && body !== '') {
    const text = typeof body === 'string' ? body : JSON.stringify(body);
    parts.push('--data', shellQuote(text));
  }
  return parts.join(' ');
}

/** Map an HTTP status to a badge tone: 2xx ok, 1xx/3xx info, 4xx warn, 5xx (and network failures) danger. */
export function classifyStatus(status) {
  const n = Number(status);
  if (!Number.isFinite(n) || n <= 0) return 'danger';
  if (n < 200) return 'info';
  if (n < 300) return 'ok';
  if (n < 400) return 'info';
  if (n < 500) return 'warn';
  return 'danger';
}

/** Format a duration in milliseconds (number or numeric string) as '12.3 ms'; missing/invalid -> em dash. */
export function fmtMs(ms) {
  if (ms === null || ms === undefined || ms === '') return '—';
  const n = Number(ms);
  if (!Number.isFinite(n)) return '—';
  return `${n.toFixed(1)} ms`;
}

const round2 = (n) => String(Math.round(n * 100) / 100);

/** SVG path ('M x y L x y …') for a series normalised into a width×height box; max at the top. Empty input -> ''. */
export function sparklinePath(values, width, height) {
  if (!Array.isArray(values) || values.length === 0) return '';
  const w = Number(width) || 0;
  const h = Number(height) || 0;
  const nums = values.map((v) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  });
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const range = max - min;
  const y = (v) => (range === 0 ? h / 2 : h - ((v - min) / range) * h);
  const points = nums.length === 1 ? [[0, y(nums[0])], [w, y(nums[0])]] : nums.map((v, i) => [(i * w) / (nums.length - 1), y(v)]);
  return points.map(([px, py], i) => `${i === 0 ? 'M' : 'L'} ${round2(px)} ${round2(py)}`).join(' ');
}

/** Return one entry per UTC calendar day for the last `days` days (ending today), zero-filling days without a row. */
export function fillDays(rows, days, key = 'day', now = new Date()) {
  const list = Array.isArray(rows) ? rows : [];
  const count = Math.max(0, Math.floor(Number(days) || 0));
  const numericKeys = new Set();
  const byDay = new Map();
  for (const row of list) {
    if (!row || typeof row !== 'object') continue;
    byDay.set(String(row[key]).slice(0, 10), row);
    for (const [k, v] of Object.entries(row)) {
      if (k !== key && typeof v === 'number') numericKeys.add(k);
    }
  }
  const out = [];
  for (let i = count - 1; i >= 0; i -= 1) {
    const day = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - i)).toISOString().slice(0, 10);
    const row = byDay.get(day);
    if (row) {
      out.push({ ...row, [key]: day });
    } else {
      const blank = { [key]: day };
      for (const k of numericKeys) blank[k] = 0;
      out.push(blank);
    }
  }
  return out;
}

/** Extract the filename from a Content-Disposition header (quoted, bare or RFC 5987); directories are stripped. */
export function parseFilename(contentDisposition, fallback) {
  const header = contentDisposition === null || contentDisposition === undefined ? '' : String(contentDisposition);
  let name = '';
  const extended = /filename\*\s*=\s*[^']*'[^']*'([^;]+)/i.exec(header);
  if (extended) {
    const raw = extended[1].trim();
    try {
      name = decodeURIComponent(raw);
    } catch {
      name = raw;
    }
  }
  if (!name) {
    const quoted = /filename\s*=\s*"([^"]*)"/i.exec(header);
    if (quoted) {
      name = quoted[1];
    } else {
      const bare = /filename\s*=\s*([^;]+)/i.exec(header);
      if (bare) name = bare[1].trim();
    }
  }
  name = name.split(/[\\/]/).pop().replace(/[\u0000-\u001f"]/g, '').trim();
  return name || fallback;
}

/** `${stem}-YYYYMMDD.csv` using the UTC date. */
export function csvFilename(stem, date = new Date()) {
  return `${stem}-${date.toISOString().slice(0, 10).replace(/-/g, '')}.csv`;
}

/** Build a query string ('?a=1&b=2' or '') skipping null, undefined, empty-string and false values. */
export function qs(params = {}) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === null || value === undefined || value === '' || value === false) continue;
    search.append(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}
