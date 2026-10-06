// Unit tests for the pure helpers in public/lib.js (no DOM, no network).
// Run: node --test tests/ui/lib.test.mjs
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  classifyStatus,
  csvFilename,
  escapeHtml,
  fillDays,
  fmtMs,
  money,
  parseFilename,
  qs,
  sparklinePath,
  toCurl,
} from '../../public/lib.js';

describe('escapeHtml', () => {
  it('escapes the five HTML special characters and the backtick', () => {
    assert.equal(escapeHtml('<script>alert("x")</script>'), '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;');
    assert.equal(escapeHtml("Tom & Jerry's"), 'Tom &amp; Jerry&#39;s');
    assert.equal(escapeHtml('`${evil}`'), '&#96;${evil}&#96;');
  });
  it('leaves plain text untouched and stringifies numbers', () => {
    assert.equal(escapeHtml('Basmati Rice 5 kg'), 'Basmati Rice 5 kg');
    assert.equal(escapeHtml(42), '42');
    assert.equal(escapeHtml(0), '0');
  });
  it('maps null and undefined to an empty string', () => {
    assert.equal(escapeHtml(null), '');
    assert.equal(escapeHtml(undefined), '');
  });
  it('always escapes, even already-escaped input (no double-decoding surprises)', () => {
    assert.equal(escapeHtml('&amp;'), '&amp;amp;');
    const out = escapeHtml('<a href="x" onclick=\'y\'>`z`</a> & more');
    assert.doesNotMatch(out, /[<>"'`]/);
  });
});

describe('money', () => {
  it('formats cents as rupees with en-IN grouping and two decimals', () => {
    assert.equal(money(64900), '₹649.00');
    assert.equal(money(12345678), '₹1,23,456.78');
    assert.equal(money(5), '₹0.05');
    assert.equal(money(0), '₹0.00');
  });
  it('keeps the sign in front of the currency symbol', () => {
    assert.equal(money(-500), '-₹5.00');
  });
  it('renders missing values as an em dash', () => {
    assert.equal(money(null), '—');
    assert.equal(money(undefined), '—');
    assert.equal(money('not a number'), '—');
  });
  it('accepts numeric strings', () => {
    assert.equal(money('250'), '₹2.50');
  });
});

describe('toCurl', () => {
  it('builds a plain GET without -X and joins base and path with one slash', () => {
    assert.equal(toCurl({ method: 'GET', url: '/health', base: 'http://localhost:8000/' }), "curl 'http://localhost:8000/health'");
    assert.equal(toCurl({ url: 'inventory?limit=5', base: 'http://localhost:8000' }), "curl 'http://localhost:8000/inventory?limit=5'");
  });
  it('adds method, headers and body for a POST', () => {
    const cmd = toCurl({
      method: 'post',
      url: '/orders',
      base: 'http://localhost:8000',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': 'k-1' },
      body: '{"store_id":1,"lines":[{"product_id":2,"quantity":1}]}',
    });
    assert.equal(
      cmd,
      "curl -X POST 'http://localhost:8000/orders' -H 'Content-Type: application/json' -H 'Idempotency-Key: k-1' " +
        "--data '{\"store_id\":1,\"lines\":[{\"product_id\":2,\"quantity\":1}]}'",
    );
  });
  it('serialises object bodies as JSON and quotes single quotes safely', () => {
    const cmd = toCurl({ method: 'POST', url: '/products', body: { name: "O'Brien" } });
    assert.ok(cmd.endsWith(`--data '{"name":"O'\\''Brien"}'`), cmd);
    assert.ok(cmd.startsWith("curl -X POST '/products'"), cmd);
  });
  it('skips empty headers and bodies and works without a base', () => {
    assert.equal(toCurl({ method: 'DELETE', url: '/products/3', headers: { 'X-Empty': '' }, body: null }), "curl -X DELETE '/products/3'");
    assert.equal(toCurl({ url: "/odd'path" }), `curl '/odd'\\''path'`);
  });
});

describe('classifyStatus', () => {
  it('maps status classes to badge tones', () => {
    assert.equal(classifyStatus(200), 'ok');
    assert.equal(classifyStatus(201), 'ok');
    assert.equal(classifyStatus(204), 'ok');
    assert.equal(classifyStatus(304), 'info');
    assert.equal(classifyStatus(404), 'warn');
    assert.equal(classifyStatus(409), 'warn');
    assert.equal(classifyStatus(422), 'warn');
    assert.equal(classifyStatus(500), 'danger');
    assert.equal(classifyStatus(503), 'danger');
  });
  it('treats network failures (status 0 / missing) as danger and accepts numeric strings', () => {
    assert.equal(classifyStatus(0), 'danger');
    assert.equal(classifyStatus(undefined), 'danger');
    assert.equal(classifyStatus(null), 'danger');
    assert.equal(classifyStatus('201'), 'ok');
    assert.equal(classifyStatus(101), 'info');
  });
});

describe('fmtMs', () => {
  it('formats numbers and numeric strings with one decimal and a unit', () => {
    assert.equal(fmtMs(12.34), '12.3 ms');
    assert.equal(fmtMs(12.36), '12.4 ms');
    assert.equal(fmtMs('7'), '7.0 ms');
    assert.equal(fmtMs(0), '0.0 ms');
    assert.equal(fmtMs(1234.56), '1234.6 ms');
  });
  it('renders missing or invalid values as an em dash', () => {
    assert.equal(fmtMs(null), '—');
    assert.equal(fmtMs(undefined), '—');
    assert.equal(fmtMs(''), '—');
    assert.equal(fmtMs('abc'), '—');
  });
});

describe('sparklinePath', () => {
  it('returns an empty string for no data', () => {
    assert.equal(sparklinePath([], 100, 20), '');
    assert.equal(sparklinePath(null, 100, 20), '');
  });
  it('normalises values into the box (max at the top, min at the bottom)', () => {
    assert.equal(sparklinePath([0, 10], 100, 20), 'M 0 20 L 100 0');
    assert.equal(sparklinePath([0, 10, 5], 100, 20), 'M 0 20 L 50 0 L 100 10');
  });
  it('draws a flat mid-line when all values are equal or there is one point', () => {
    assert.equal(sparklinePath([5, 5, 5], 100, 20), 'M 0 10 L 50 10 L 100 10');
    assert.equal(sparklinePath([3], 100, 20), 'M 0 10 L 100 10');
  });
  it('keeps every coordinate inside the box and emits one segment per gap', () => {
    const values = [12, 0, 7, 99, 3, 50, 50, 1];
    const d = sparklinePath(values, 300, 60);
    assert.match(d, /^M /);
    const segments = d.split(' L ');
    assert.equal(segments.length, values.length);
    for (const seg of segments) {
      const [x, y] = seg.replace(/^M /, '').split(' ').map(Number);
      assert.ok(x >= 0 && x <= 300, `x out of range: ${x}`);
      assert.ok(y >= 0 && y <= 60, `y out of range: ${y}`);
    }
    assert.doesNotMatch(d, /NaN|Infinity/);
  });
  it('treats non-numeric entries as zero', () => {
    assert.equal(sparklinePath([null, 10], 100, 20), 'M 0 20 L 100 0');
  });
});

describe('fillDays', () => {
  const now = new Date('2026-10-05T12:00:00Z');
  it('returns one entry per UTC day ending today, zero-filling missing days', () => {
    const rows = [{ day: '2026-10-03', orders: 2, units: 3, revenue_cents: 500 }];
    assert.deepEqual(fillDays(rows, 3, 'day', now), [
      { day: '2026-10-03', orders: 2, units: 3, revenue_cents: 500 },
      { day: '2026-10-04', orders: 0, units: 0, revenue_cents: 0 },
      { day: '2026-10-05', orders: 0, units: 0, revenue_cents: 0 },
    ]);
  });
  it('drops rows outside the window and keeps rows for today', () => {
    const rows = [
      { day: '2026-09-01', orders: 9, units: 9, revenue_cents: 9 },
      { day: '2026-10-05', orders: 1, units: 1, revenue_cents: 100 },
    ];
    const out = fillDays(rows, 2, 'day', now);
    assert.deepEqual(out.map((r) => r.day), ['2026-10-04', '2026-10-05']);
    assert.equal(out[1].revenue_cents, 100);
    assert.equal(out[0].orders, 0);
  });
  it('handles empty input, a custom key and crosses month boundaries', () => {
    const out = fillDays([], 2, 'date', new Date('2026-10-01T00:30:00Z'));
    assert.deepEqual(out, [{ date: '2026-09-30' }, { date: '2026-10-01' }]);
    assert.equal(fillDays(null, 1, 'day', now).length, 1);
    assert.deepEqual(fillDays([], 0, 'day', now), []);
  });
  it('uses the UTC calendar date, not the local one', () => {
    const out = fillDays([], 1, 'day', new Date('2026-10-05T23:59:59Z'));
    assert.deepEqual(out, [{ day: '2026-10-05' }]);
  });
});

describe('parseFilename', () => {
  it('reads quoted and unquoted filenames from Content-Disposition', () => {
    assert.equal(parseFilename('attachment; filename="inventory-20261005.csv"', 'x.csv'), 'inventory-20261005.csv');
    assert.equal(parseFilename('attachment; filename=orders.csv', 'x.csv'), 'orders.csv');
    assert.equal(parseFilename('attachment; filename="reorder.csv"; size=120', 'x.csv'), 'reorder.csv');
  });
  it('decodes RFC 5987 extended filenames', () => {
    assert.equal(parseFilename("attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.csv", 'x.csv'), 'résumé.csv');
  });
  it('falls back when the header is missing, inline-only or unsafe', () => {
    assert.equal(parseFilename(null, 'fallback.csv'), 'fallback.csv');
    assert.equal(parseFilename(undefined, 'fallback.csv'), 'fallback.csv');
    assert.equal(parseFilename('inline', 'fallback.csv'), 'fallback.csv');
    assert.equal(parseFilename('attachment; filename=""', 'fallback.csv'), 'fallback.csv');
  });
  it('strips directory components', () => {
    assert.equal(parseFilename('attachment; filename="../../etc/passwd.csv"', 'x.csv'), 'passwd.csv');
    assert.equal(parseFilename('attachment; filename="C:\\temp\\a.csv"', 'x.csv'), 'a.csv');
  });
});

describe('csvFilename', () => {
  it('appends the UTC date as YYYYMMDD', () => {
    assert.equal(csvFilename('inventory', new Date('2026-10-05T23:59:59Z')), 'inventory-20261005.csv');
    assert.equal(csvFilename('orders', new Date('2026-01-09T00:00:00Z')), 'orders-20260109.csv');
  });
  it('defaults to the current date', () => {
    assert.match(csvFilename('reorder'), /^reorder-\d{8}\.csv$/);
  });
});

describe('qs', () => {
  it('builds a query string, skipping empty, null, undefined and false values', () => {
    assert.equal(qs({ store_id: 1, low_stock: true, q: '', status: null, offset: undefined, inactive: false }), '?store_id=1&low_stock=true');
    assert.equal(qs({ q: 'a&b c', offset: 0 }), '?q=a%26b+c&offset=0');
  });
  it('returns an empty string when nothing remains', () => {
    assert.equal(qs({}), '');
    assert.equal(qs({ a: null }), '');
    assert.equal(qs(), '');
  });
});
