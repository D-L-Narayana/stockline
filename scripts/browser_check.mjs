#!/usr/bin/env node
/*
 * StockLine browser workflow check: the production Content-Security-Policy and security headers,
 * the real dashboard workflows and real CSV downloads, all in one headless Chromium session.
 *
 * What it does
 *   1. headers    — plain HTTP (no browser): exact Content-Security-Policy and security-header values on the
 *                   index page, the compiled assets (/app.js, /styles.css, /lib.js), JSON, the CSV exports,
 *                   /docs and /redoc, a 404 and a 422.  Expected values are parsed from app/security.py at run
 *                   time, so this script cannot drift from the policy the server is built to send.
 *   2. context    — one Chromium context with the CSP enforced exactly as served: a `securitypolicyviolation`
 *                   listener is installed on every page; every console message, page error, failed request and
 *                   request URL is recorded; any request to an origin other than the server fails the run.
 *   3. workflows  — the dashboard end to end through its UI: inventory, deliveries, optimistic-locking
 *                   adjustments, idempotent orders (201 → 200 replay → 422 key reuse → 409 oversell),
 *                   cancel/fulfil, transfers with Idempotency-Key replay, reports, catalog lifecycle (patch,
 *                   soft delete, reactivate), the guided scenarios, the request console, CSV exports as real
 *                   downloads (response headers AND saved file content), keyboard navigation and dark mode.
 *   4. axe        — optional and separately labelled: runs only when axe-core is resolvable, in its own
 *                   bypassCSP context that is never used for application-flow steps; otherwise "not run".
 *   5. responsive — server mode under the enforced CSP, a fresh context per viewport (390×900 and 1280×900): every
 *                   view is opened and must not widen the document (scrollWidth − innerWidth ≤ 1 px); the seven tabs must
 *                   stay reachable and `.table-wrap` must keep scrolling horizontally when its table is wider than the
 *                   wrapper.  A failing view lists every box that crosses the right edge and the ancestor that clips it.
 *   6. pages-demo — separately labelled: builds the static GitHub Pages site with scripts/build_site.py, serves
 *                   it from a header-less static server (Content-Type only, like GitHub Pages), asserts the
 *                   injected CSP meta tag equals build_site.PAGES_CSP, boots the Pyodide runtime from the CDN
 *                   and runs a reduced workflow set, including the 390×900 overflow measurement on every view.  Only the
 *                   CDN origins named in PAGES_CSP may be contacted; an unreachable CDN is a failed stage, never a skipped pass.
 *
 * Usage
 *   node scripts/browser_check.mjs [--out DIR] [--base URL | --python PATH] [--browsers-path DIR] [--headed]
 *                                  [--timeout MS] [--pages-timeout MS] [--no-pages-demo]
 *
 *   --out DIR            report.json, console.log, requests.log, csp-violations.json, headers.json, session.har,
 *                        server.log, downloads/ and numbered screenshots (default: a fresh folder in the OS temp dir)
 *   --base URL           check a running server instead of starting one (it must be seeded with the demo data)
 *   --python PATH        interpreter for the self-started server and the site build
 *                        (default ./.venv/bin/python when present, else python3)
 *   --browsers-path DIR  sets PLAYWRIGHT_BROWSERS_PATH before Playwright is loaded
 *   --headed             run with a visible browser window
 *   --timeout MS         per-step wait budget (default 15000)
 *   --pages-timeout MS   budget for the Pyodide runtime to boot from the CDN (default 240000)
 *   --no-pages-demo      skip stage 5 (recorded as "not run" in the report; use on hosts without internet access)
 *
 * Playwright is resolved at run time ($PLAYWRIGHT_MODULE, ./node_modules/playwright, ./node_modules/@playwright/test,
 * the ancestor node_modules chain, then `npm root -g`).  Requires Node 18+ (global fetch).
 *
 * Exit codes: 0 every step passed · 1 at least one step failed · 2 Playwright or Chromium unavailable.
 */
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import { createRequire } from 'node:module';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '..');
const require = createRequire(import.meta.url);

const EXIT_OK = 0;
const EXIT_FAILED = 1;
const EXIT_UNAVAILABLE = 2;

// Documented CSV contract (module constants INVENTORY_CSV_COLUMNS, MOVEMENT_CSV_COLUMNS, ORDER_CSV_COLUMNS and
// reports.REORDER_COLUMNS). A header row that differs is a contract break, not noise.
const CSV_HEADERS = {
  inventory: 'store_id,store_code,product_id,sku,name,on_hand,reorder_point,version,below_reorder,price_cents,value_cents',
  movements: 'id,store_id,product_id,delta,reason,reference,balance_after,created_at',
  orders: 'order_id,store_id,status,created_at,updated_at,idempotency_key,product_id,sku,quantity,unit_price_cents,line_total_cents,order_total_cents',
  reorder: 'store_id,store_code,product_id,sku,name,on_hand,reorder_point,days,sold_window,returned_window,net_sold,daily_velocity,days_of_cover,suggested_qty',
};
const SEEDED_STORES = 3;
const SEEDED_PRODUCTS = 12;
const SEEDED_ROWS = SEEDED_STORES * SEEDED_PRODUCTS;
const PAGE_SIZE = 10; // orders per page in the dashboard
const SCENARIO_STEPS = { oversell: 3, idempotent: 5, stale: 5, transfer: 7, audit: 4 };
const HOSTILE_NAME = '<img src=x onerror=window.__pwned=1>"\'`&';
const DARK_BG = 'rgb(11, 18, 32)'; // --bg of the dark theme (#0b1220)
const FAILED_RESOURCE_RE = /^Failed to load resource: the server responded with a status of (\d{3})/;
// Chromium wording for CSP refusals on the console: "Refused to …" (older) / "… violates the following Content Security Policy directive …" (newer).
const CSP_CONSOLE_RE = /Refused to|Content Security Policy/i;
const DASH = '[–-]'; // the pager uses an en dash
// Responsive stage: viewports measured, the dashboard's views, and what must be rendered before a view is measured.
const RESPONSIVE_WIDTHS = [390, 1280];
const VIEWPORT_HEIGHT = 900;
const MAX_DOCUMENT_OVERFLOW = 1; // px: document.documentElement.scrollWidth - window.innerWidth
const VIEWS = ['inventory', 'orders', 'transfers', 'reports', 'catalog', 'scenarios', 'console'];
const VIEW_READY = {
  inventory: ['#inventory-table tbody tr'],
  orders: ['#orders-table tbody tr'],
  transfers: ['#transfers-table tbody tr'],
  reports: ['#summary-table tbody tr', '#reorder-table tbody tr', '#sales-chart svg.spark'],
  catalog: ['#products-table tbody tr', '#stores-table tbody tr'],
  scenarios: ['#view-scenarios article.card'],
  console: ['#console-table tbody tr'],
};
const STATIC_MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.py': 'text/plain; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.wasm': 'application/wasm',
};

// --------------------------------------------------------------------------- small helpers
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const stamp = () => new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z');
const slug = (text) => String(text).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 72);
const json = (value) => JSON.stringify(value);
const escapeRe = (text) => String(text).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const relative = (file) => {
  const rel = path.relative(REPO, file);
  return rel.startsWith('..') ? file : rel;
};

// --------------------------------------------------------------------------- page-side probes (serialised into the browser; no outer-scope references)
/** Document overflow and every rendered box whose border edge crosses the right edge of the viewport, with the ancestor that clips it. */
function measureOverflow() {
  const width = window.innerWidth;
  const describe = (el) => {
    const role = el.getAttribute('role');
    return `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}${el.classList.length ? `.${Array.from(el.classList).join('.')}` : ''}${role ? `[role=${role}]` : ''}`;
  };
  const clipping = ['hidden', 'auto', 'scroll', 'clip'];
  const clips = (el) => {
    const cs = getComputedStyle(el);
    return clipping.includes(cs.overflowX) || clipping.includes(cs.overflow);
  };
  // An ancestor's overflow only clips boxes whose containing block is that ancestor or one of its descendants: a static
  // or relative box is contained by its parent, an absolute box by its nearest positioned ancestor (none → the initial
  // containing block, which nothing clips), a fixed box by the viewport.
  const containingBlock = (el) => {
    const position = getComputedStyle(el).position;
    if (position === 'fixed') return null;
    let node = el.parentElement;
    if (position === 'absolute') while (node && node !== document.documentElement && getComputedStyle(node).position === 'static') node = node.parentElement;
    return node === document.documentElement ? null : node;
  };
  const clipper = (el) => {
    let node = containingBlock(el);
    while (node && node !== document.documentElement) {
      if (clips(node)) return node;
      node = node.parentElement;
    }
    return null;
  };
  const offenders = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest('[hidden]')) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const rect = el.getBoundingClientRect();
    if ((rect.width === 0 && rect.height === 0) || rect.right <= width + 0.5) continue;
    const block = containingBlock(el);
    const by = clipper(el);
    offenders.push({
      element: describe(el),
      left: Math.round(rect.left * 100) / 100,
      right: Math.round(rect.right * 100) / 100,
      position: cs.position,
      containingBlock: cs.position === 'absolute' || cs.position === 'fixed' ? (block ? describe(block) : 'initial containing block') : 'parent',
      clippedBy: by ? describe(by) : null,
    });
  }
  return {
    innerWidth: width,
    scrollWidth: document.documentElement.scrollWidth,
    overflow: document.documentElement.scrollWidth - width,
    offenders,
    unclipped: offenders.filter((o) => !o.clippedBy),
  };
}

/** Tab-bar reachability and the horizontal scrolling of every visible `.table-wrap` (within-component scrolling must survive any fix). */
function inspectLayout() {
  const tabs = Array.from(document.querySelectorAll('[role=tab]'));
  const tablist = document.querySelector('[role=tablist]');
  const selected = document.querySelector('[role=tab][aria-selected="true"]');
  const wraps = Array.from(document.querySelectorAll('.table-wrap'))
    .filter((w) => !w.closest('[hidden]') && w.getBoundingClientRect().width > 0)
    .map((w) => {
      const table = w.querySelector('table');
      return {
        table: table ? table.id || 'table' : '(no table)',
        clientWidth: w.clientWidth,
        scrollWidth: w.scrollWidth,
        tableWidth: table ? Math.round(table.getBoundingClientRect().width) : 0,
        overflowX: getComputedStyle(w).overflowX,
      };
    });
  return {
    tabCount: tabs.length,
    tabsVisible: tabs.every((t) => {
      const r = t.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    }),
    tablistScrolls: Boolean(tablist && tablist.scrollWidth > tablist.clientWidth),
    tablistOverflowX: tablist ? getComputedStyle(tablist).overflowX : null,
    selected: selected ? selected.id : null,
    wraps,
  };
}

function usage() {
  const lines = fs.readFileSync(fileURLToPath(import.meta.url), 'utf8').split('\n');
  const start = lines.findIndex((l) => l.includes(' * Usage'));
  const end = lines.findIndex((l) => l.includes(' * Exit codes'));
  process.stdout.write(`${lines.slice(start, end + 1).map((l) => l.replace(/^ \* ?/, '')).join('\n')}\n`);
}

function parseArgs(argv) {
  const opts = { out: null, base: null, python: null, browsersPath: null, headed: false, timeout: 15000, pagesTimeout: 240000, pagesDemo: true };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const value = () => {
      i += 1;
      if (i >= argv.length) throw new Error(`${arg} needs a value`);
      return argv[i];
    };
    if (arg === '--out') opts.out = path.resolve(value());
    else if (arg === '--base') opts.base = value().replace(/\/+$/, '');
    else if (arg === '--python') opts.python = value();
    else if (arg === '--browsers-path') opts.browsersPath = path.resolve(value());
    else if (arg === '--headed') opts.headed = true;
    else if (arg === '--timeout') opts.timeout = Math.max(1000, Number(value()) || 15000);
    else if (arg === '--pages-timeout') opts.pagesTimeout = Math.max(10000, Number(value()) || 240000);
    else if (arg === '--no-pages-demo') opts.pagesDemo = false;
    else if (arg === '-h' || arg === '--help') {
      usage();
      process.exit(EXIT_OK);
    } else throw new Error(`unknown option ${arg} (try --help)`);
  }
  if (!opts.out) opts.out = path.join(os.tmpdir(), `stockline-browser-check-${stamp()}`);
  return opts;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function portIsClosed(port) {
  return new Promise((resolve) => {
    const socket = net.connect({ port, host: '127.0.0.1' });
    socket.setTimeout(2000);
    socket.once('connect', () => {
      socket.destroy();
      resolve(false);
    });
    socket.once('error', () => resolve(true));
    socket.once('timeout', () => {
      socket.destroy();
      resolve(true);
    });
  });
}

// --------------------------------------------------------------------------- expected policies, parsed from the sources
function pyStrings(text) {
  const out = [];
  const re = /"((?:[^"\\]|\\.)*)"/g;
  let m = re.exec(text);
  while (m) {
    out.push(m[1].replace(/\\(.)/g, '$1'));
    m = re.exec(text);
  }
  return out;
}

function pyBlock(src, name, open, close, file) {
  const re = new RegExp(`^${name}\\s*=\\s*\\${open}([\\s\\S]*?)\\n\\${close}`, 'm');
  const m = re.exec(src);
  if (!m) throw new Error(`cannot find ${name} in ${file}`);
  return m[1];
}

function readPolicy() {
  const securityFile = path.join(REPO, 'app', 'security.py');
  const src = fs.readFileSync(securityFile, 'utf8');
  const defaultCsp = pyStrings(pyBlock(src, 'DEFAULT_CSP', '(', ')', 'app/security.py')).join('');
  const docsCsp = pyStrings(pyBlock(src, 'DOCS_CSP', '(', ')', 'app/security.py')).join('');
  const securityHeaders = {};
  const pairs = /"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"/g;
  const body = pyBlock(src, 'SECURITY_HEADERS', '{', '}', 'app/security.py');
  let m = pairs.exec(body);
  while (m) {
    securityHeaders[m[1]] = m[2];
    m = pairs.exec(body);
  }
  if (!defaultCsp.startsWith('default-src') || !defaultCsp.includes("script-src 'self'")) throw new Error(`unexpected DEFAULT_CSP parsed: ${defaultCsp}`);
  if (!docsCsp.startsWith('default-src') || !docsCsp.includes('cdn.jsdelivr.net')) throw new Error(`unexpected DOCS_CSP parsed: ${docsCsp}`);
  if (Object.keys(securityHeaders).length < 6 || !securityHeaders['X-Content-Type-Options']) throw new Error('unexpected SECURITY_HEADERS parsed');
  const buildFile = path.join(REPO, 'scripts', 'build_site.py');
  const pagesCsp = pyStrings(pyBlock(fs.readFileSync(buildFile, 'utf8'), 'PAGES_CSP', '(', ')', 'scripts/build_site.py')).join('');
  if (!pagesCsp.startsWith('default-src') || !pagesCsp.includes('cdn.jsdelivr.net')) throw new Error(`unexpected PAGES_CSP parsed: ${pagesCsp}`);
  const pagesOrigins = [...new Set((pagesCsp.match(/https?:\/\/[^\s;]+/g) || []).map((u) => new URL(u).origin))];
  const versionMatch = /__version__\s*=\s*"([^"]+)"/.exec(fs.readFileSync(path.join(REPO, 'app', '__init__.py'), 'utf8'));
  return {
    defaultCsp,
    docsCsp,
    securityHeaders,
    pagesCsp,
    pagesOrigins,
    version: versionMatch ? versionMatch[1] : null,
    sources: { server: relative(securityFile), pages: relative(buildFile) },
  };
}

// --------------------------------------------------------------------------- Playwright / axe resolution
function npmRootGlobal() {
  try {
    const res = spawnSync('npm', ['root', '-g'], { encoding: 'utf8', timeout: 20000 });
    const root = (res.stdout || '').trim();
    return res.status === 0 && root ? root : null;
  } catch {
    return null;
  }
}

function tryResolve(candidate, test, tried) {
  try {
    const resolved = require.resolve(candidate);
    const mod = test(resolved);
    if (mod) return { resolved, candidate, module: mod };
    tried.push(`${candidate} (resolved, but not usable)`);
  } catch (err) {
    tried.push(`${candidate} (${err.code || err.message})`);
  }
  return null;
}

function resolveModule(names, test) {
  const tried = [];
  const envNames = { playwright: 'PLAYWRIGHT_MODULE', '@playwright/test': 'PLAYWRIGHT_MODULE', 'axe-core': 'AXE_CORE_PATH' };
  const candidates = [];
  for (const name of names) if (process.env[envNames[name]] && !candidates.includes(process.env[envNames[name]])) candidates.push(process.env[envNames[name]]);
  for (const name of names) candidates.push(path.join(REPO, 'node_modules', name));
  for (const name of names) candidates.push(name); // ancestor node_modules chain, starting from this file
  for (const candidate of candidates) {
    const result = tryResolve(candidate, test, tried);
    if (result) return result;
  }
  const globalRoot = npmRootGlobal();
  if (!globalRoot) tried.push('npm root -g (unavailable)');
  else {
    for (const name of names) {
      const result = tryResolve(path.join(globalRoot, name), test, tried);
      if (result) return result;
    }
  }
  return { tried };
}

function loadPlaywright() {
  const found = resolveModule(['playwright', '@playwright/test'], (resolved) => {
    const mod = require(resolved);
    return mod && mod.chromium ? mod : null;
  });
  if (!found.module) return found;
  let version = null;
  try {
    version = require(path.join(path.dirname(found.resolved), 'package.json')).version;
  } catch {
    version = null;
  }
  return { ...found, version };
}

function loadAxeSource() {
  return resolveModule(['axe-core'], (resolved) => {
    const dir = path.dirname(resolved);
    const file = [path.join(dir, 'axe.min.js'), path.join(dir, 'axe.js'), resolved].find((f) => fs.existsSync(f) && /\.js$/.test(f));
    return file ? fs.readFileSync(file, 'utf8') : null;
  });
}

// --------------------------------------------------------------------------- the server under test
function pickPython(opts) {
  const venv = path.join(REPO, '.venv', 'bin', 'python');
  return opts.python || (fs.existsSync(venv) ? venv : 'python3');
}

async function startServer(python, out) {
  const port = await freePort();
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'stockline-browser-check-'));
  const env = { ...process.env, STOCKLINE_DB: path.join(tmp, 'stockline.db'), STOCKLINE_SEED: '1' };
  for (const name of ['STOCKLINE_API_KEY', 'STOCKLINE_CSP', 'STOCKLINE_HSTS', 'STOCKLINE_CORS_ORIGINS']) delete env[name]; // production defaults
  const logFd = fs.openSync(path.join(out, 'server.log'), 'a');
  const args = ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)];
  const proc = spawn(python, args, { cwd: REPO, env, stdio: ['ignore', logFd, logFd] });
  const server = { proc, python, args, port, base: `http://127.0.0.1:${port}`, tmp, logFd, exited: false, exitCode: null, signal: null, spawnError: null };
  proc.on('exit', (code, signal) => {
    server.exited = true;
    server.exitCode = code;
    server.signal = signal;
  });
  proc.on('error', (err) => {
    server.spawnError = err;
    server.exited = true;
  });
  const deadline = Date.now() + 40000;
  while (Date.now() < deadline) {
    if (server.exited) break;
    try {
      const r = await fetch(`${server.base}/health`);
      if (r.status === 200) return server;
    } catch {
      // not listening yet
    }
    await sleep(250);
  }
  await stopServer(server);
  const logPath = path.join(out, 'server.log');
  const tail = fs.existsSync(logPath) ? fs.readFileSync(logPath, 'utf8').split('\n').slice(-20).join('\n') : '';
  throw new Error(`server did not answer GET /health within 40 s (${python} ${args.join(' ')})${server.spawnError ? `: ${server.spawnError.message}` : ''}\n${tail}`);
}

async function stopServer(server) {
  if (!server) return null;
  const { proc } = server;
  if (proc && !server.exited) {
    try {
      proc.kill('SIGTERM');
    } catch {
      // already gone
    }
    const deadline = Date.now() + 8000;
    while (!server.exited && Date.now() < deadline) await sleep(100);
    if (!server.exited) {
      try {
        proc.kill('SIGKILL');
      } catch {
        // already gone
      }
      const hard = Date.now() + 3000;
      while (!server.exited && Date.now() < hard) await sleep(100);
    }
  }
  if (server.logFd !== null && server.logFd !== undefined) {
    try {
      fs.closeSync(server.logFd);
    } catch {
      // closed already
    }
    server.logFd = null;
  }
  if (server.tmp) {
    fs.rmSync(server.tmp, { recursive: true, force: true });
    server.tmp = null;
  }
  return { stopped: server.exited, exitCode: server.exitCode, signal: server.signal, portClosed: await portIsClosed(server.port) };
}

// --------------------------------------------------------------------------- header-less static server (GitHub Pages shape)
function startStaticServer(root) {
  const stats = { served: 0, notFound: 0 };
  const server = http.createServer((req, res) => {
    let pathname;
    try {
      pathname = decodeURIComponent(new URL(req.url, 'http://static.invalid').pathname);
    } catch {
      pathname = '/';
    }
    if (pathname.endsWith('/')) pathname += 'index.html';
    const file = path.normalize(path.join(root, pathname));
    let ok = file.startsWith(root);
    try {
      ok = ok && fs.statSync(file).isFile();
    } catch {
      ok = false;
    }
    if (!ok) {
      // GitHub Pages answers unknown paths (the dashboard's `health` probe among them) with an HTML 404 page.
      stats.notFound += 1;
      res.writeHead(404, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end('<!doctype html><title>404</title><h1>404 Not Found</h1>');
      return;
    }
    stats.served += 1;
    res.writeHead(200, { 'Content-Type': STATIC_MIME[path.extname(file).toLowerCase()] || 'application/octet-stream' });
    fs.createReadStream(file).pipe(res);
  });
  return new Promise((resolve, reject) => {
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      resolve({
        server,
        stats,
        port,
        base: `http://127.0.0.1:${port}`,
        close: () =>
          new Promise((done) => {
            if (typeof server.closeAllConnections === 'function') server.closeAllConnections();
            server.close(() => done());
          }),
      });
    });
  });
}

// --------------------------------------------------------------------------- per-context monitors
function newMonitor(name) {
  return {
    name,
    requests: [],
    cspViolations: [],
    consoleErrors: [],
    consoleWarnings: [],
    expectedHttpErrorLogs: [],
    httpErrorResponses: [],
    pageErrors: [],
    requestFailed: [],
    foreignRequests: [],
    allowedForeignRequests: [],
    downloads: [],
    resourceErrors: [],
  };
}

function monitorFields(monitor) {
  const { name, requests, resourceErrors, ...rest } = monitor;
  return rest;
}

// --------------------------------------------------------------------------- the check
class Check {
  constructor(opts, policy) {
    this.opts = opts;
    this.policy = policy;
    this.out = opts.out;
    this.timeout = opts.timeout;
    this.shots = 0;
    this.current = null;
    this.server = newMonitor('server-mode');
    this.pages = newMonitor('pages-demo');
    this.violationSink = this.server.cspViolations;
    this.consoleLines = [];
    this.pagesInfo = null;
    this.overflowByView = {}; // "<context>:<width>x<height>" -> view -> document overflow in px
    this.responsiveDetails = [];
    this.responsiveContexts = {};
    this.report = {
      tool: 'scripts/browser_check.mjs',
      startedAt: new Date().toISOString(),
      base: null,
      mode: opts.base ? 'external server (--base)' : 'self-started uvicorn app.main:app (temp database, STOCKLINE_SEED=1, production defaults)',
      policySources: policy.sources,
      policy: { defaultCsp: policy.defaultCsp, docsCsp: policy.docsCsp, securityHeaders: policy.securityHeaders, pagesCsp: policy.pagesCsp, pagesOrigins: policy.pagesOrigins },
      environment: { node: process.version, platform: `${os.platform()} ${os.release()}` },
      steps: [],
      headers: {},
      cspPositiveControl: null,
      axe: 'unavailable',
      responsive: null,
      summary: null,
    };
    fs.mkdirSync(path.join(this.out, 'downloads'), { recursive: true });
  }

  log(line) {
    process.stdout.write(`${line}\n`);
  }

  async step(name, fn, { stage = 'workflows', page = null, screenshot = true, shot = null, kind = null } = {}) {
    const entry = { step: name, stage, status: 'pass', details: '', ms: 0 };
    if (kind) entry.kind = kind;
    this.current = entry;
    const t0 = Date.now();
    try {
      const result = await fn();
      entry.details = result === undefined || result === null ? '' : String(result);
    } catch (err) {
      entry.status = 'fail';
      const text = err && err.stack ? err.stack : String(err);
      entry.details = text.split('\n').slice(0, 6).join('\n');
    }
    entry.ms = Date.now() - t0;
    if (page) {
      const violations = await this.drainViolations(page);
      if (violations.length) {
        entry.status = 'fail';
        entry.cspViolations = violations.length;
        entry.details += `${entry.details ? '\n' : ''}${violations.length} CSP violation(s): ${json(violations.slice(0, 3))}`;
      }
      if (screenshot) entry.screenshot = await this.shoot(page, shot || name);
    }
    delete entry.positiveControl;
    this.report.steps.push(entry);
    const first = entry.details.split('\n')[0];
    this.log(`${entry.status === 'pass' ? 'PASS' : 'FAIL'}  [${stage}] ${name}${first ? ` — ${first}` : ''}`);
    this.current = null;
    return entry;
  }

  failed(name, reason, stage, kind = null) {
    const entry = { step: name, stage, status: 'fail', details: reason, ms: 0 };
    if (kind) entry.kind = kind;
    this.report.steps.push(entry);
    this.log(`FAIL  [${stage}] ${name} — ${reason}`);
    return entry;
  }

  notRun(name, reason, stage) {
    const entry = { step: name, stage, status: 'not_run', details: reason, ms: 0 };
    this.report.steps.push(entry);
    this.log(`NOT RUN  [${stage}] ${name} — ${reason}`);
    return entry;
  }

  async shoot(page, name) {
    this.shots += 1;
    const file = `${String(this.shots).padStart(2, '0')}-${slug(name)}.png`;
    try {
      await page.screenshot({ path: path.join(this.out, file), fullPage: false });
      return file;
    } catch {
      return null;
    }
  }

  async drainViolations(page) {
    try {
      const found = await page.evaluate(() => (window.__cspViolations || []).splice(0));
      const tagged = found.map((v) => ({ ...v, step: this.current ? this.current.step : null }));
      if (this.current && this.current.positiveControl) {
        this.report.cspPositiveControl = { ...(this.report.cspPositiveControl || {}), drainedViolations: tagged };
        return [];
      }
      this.violationSink.push(...tagged);
      return tagged;
    } catch {
      return [];
    }
  }

  /** Install the CSP-violation listener on every page of a context. */
  async installViolationListener(context) {
    await context.addInitScript(() => {
      window.__cspViolations = [];
      document.addEventListener('securitypolicyviolation', (e) => {
        window.__cspViolations.push({
          blockedURI: e.blockedURI,
          violatedDirective: e.violatedDirective,
          effectiveDirective: e.effectiveDirective,
          sourceFile: e.sourceFile,
          lineNumber: e.lineNumber,
          columnNumber: e.columnNumber,
          sample: e.sample,
          disposition: e.disposition,
          documentURI: e.documentURI,
        });
      });
    });
  }

  /** Record console, page errors, every request (and its origin), responses, failed requests and downloads into `monitor`. */
  attachMonitor(page, origin, monitor, allowedOrigins = []) {
    const stepName = () => (this.current ? this.current.step : null);
    const inControl = () => Boolean(this.current && this.current.positiveControl);
    page.on('console', (msg) => {
      const type = msg.type();
      const text = msg.text();
      const location = msg.location() || {};
      this.consoleLines.push(`${new Date().toISOString()} [${monitor.name}] [${type}] ${text}${location.url ? ` (${location.url}:${location.lineNumber})` : ''} <${stepName() || '-'}>`);
      const record = { type, text, url: location.url || '', step: stepName() };
      if (CSP_CONSOLE_RE.test(text)) {
        if (inControl()) {
          const control = (this.report.cspPositiveControl = this.report.cspPositiveControl || {});
          control.consoleRefusals = [...(control.consoleRefusals || []), text];
        } else {
          monitor.consoleErrors.push({ ...record, reason: 'CSP refusal reported on the console' });
        }
        return;
      }
      if (type === 'error') {
        const m = FAILED_RESOURCE_RE.exec(text);
        if (m) {
          // Chromium logs every non-2xx fetch at error level; the workflows provoke 4xx answers on purpose.
          // Classified by classifyResourceErrors(): accepted only for a same-origin 4xx.
          monitor.resourceErrors.push({ status: Number(m[1]), url: location.url || '', step: stepName(), text });
          return;
        }
        monitor.consoleErrors.push(record);
      } else if (type === 'warning') {
        monitor.consoleWarnings.push(record);
      }
    });
    page.on('pageerror', (err) => monitor.pageErrors.push({ message: String(err && err.message ? err.message : err), step: stepName() }));
    page.on('request', (req) => {
      const entry = { method: req.method(), url: req.url(), type: req.resourceType(), status: null, contentType: '', step: stepName() };
      monitor.requests.push(entry);
      req.__entry = entry;
      let url;
      try {
        url = new URL(req.url());
      } catch {
        url = null;
      }
      const harmless = url && ['data:', 'about:', 'blob:'].includes(url.protocol);
      if (harmless || (url && url.origin === origin)) return;
      if (url && allowedOrigins.includes(url.origin)) monitor.allowedForeignRequests.push({ ...entry });
      else monitor.foreignRequests.push({ ...entry });
    });
    page.on('response', (res) => {
      const entry = res.request().__entry;
      if (entry) {
        entry.status = res.status();
        entry.contentType = res.headers()['content-type'] || '';
      }
      if (res.status() >= 400) monitor.httpErrorResponses.push({ url: res.url(), status: res.status(), step: stepName() });
    });
    page.on('requestfailed', (req) => {
      const failure = req.failure();
      monitor.requestFailed.push({ method: req.method(), url: req.url(), error: failure ? failure.errorText : 'unknown', step: stepName() });
    });
    page.on('download', (d) => monitor.downloads.push({ suggestedFilename: d.suggestedFilename(), step: stepName() }));
  }

  /** Sort the "Failed to load resource" console lines: a same-origin 4xx is expected noise, anything else is an error. */
  classifyResourceErrors(monitor, origin) {
    for (const e of monitor.resourceErrors) {
      const fourXx = e.status >= 400 && e.status < 500;
      let url = e.url;
      if (!url) {
        // The browser omitted the URL: match a response of that status observed during the same step.
        const seen = monitor.httpErrorResponses.filter((h) => h.status === e.status && h.step === e.step);
        url = seen.length ? seen[seen.length - 1].url : '';
      }
      if (fourXx && url && url.startsWith(origin)) {
        monitor.expectedHttpErrorLogs.push({ status: e.status, url, step: e.step });
      } else {
        const reason = fourXx ? 'no same-origin 4xx response matches this console line' : 'resource failure that is not a deliberate 4xx';
        monitor.consoleErrors.push({ type: 'error', text: e.text, url: e.url, step: e.step, reason });
      }
    }
    monitor.resourceErrors = [];
  }

  /** Everything that must be empty for a context to be clean. */
  contextProblems(monitor, origin) {
    this.classifyResourceErrors(monitor, origin);
    const problems = [];
    if (monitor.cspViolations.length) problems.push(`${monitor.cspViolations.length} CSP violation(s): ${json(monitor.cspViolations.slice(0, 3))}`);
    if (monitor.consoleErrors.length) problems.push(`${monitor.consoleErrors.length} console error(s): ${monitor.consoleErrors.map((c) => c.text).slice(0, 3).join(' | ')}`);
    if (monitor.pageErrors.length) problems.push(`${monitor.pageErrors.length} page error(s): ${monitor.pageErrors.map((p) => p.message).slice(0, 3).join(' | ')}`);
    if (monitor.requestFailed.length) problems.push(`${monitor.requestFailed.length} failed request(s): ${monitor.requestFailed.map((f) => `${f.url} ${f.error}`).slice(0, 3).join(' | ')}`);
    if (monitor.foreignRequests.length) problems.push(`${monitor.foreignRequests.length} foreign request(s): ${monitor.foreignRequests.map((f) => f.url).slice(0, 3).join(' | ')}`);
    const fiveXx = monitor.httpErrorResponses.filter((h) => h.status >= 500);
    if (fiveXx.length) problems.push(`${fiveXx.length} 5xx response(s): ${fiveXx.map((h) => `${h.status} ${h.url}`).slice(0, 3).join(' | ')}`);
    return problems;
  }

  // ------------------------------------------------------------------- stage 1: headers over plain HTTP
  async headerStage(base) {
    const { defaultCsp, docsCsp, securityHeaders } = this.policy;
    const specs = [
      { path: '/', type: /^text\/html/, csp: defaultCsp, noStore: false, status: 200 },
      { path: '/app.js', type: /^(text|application)\/javascript/, csp: defaultCsp, noStore: false, status: 200 },
      { path: '/lib.js', type: /^(text|application)\/javascript/, csp: defaultCsp, noStore: false, status: 200 },
      { path: '/styles.css', type: /^text\/css/, csp: defaultCsp, noStore: false, status: 200 },
      { path: '/health', type: /^application\/json/, csp: defaultCsp, noStore: true, status: 200 },
      { path: '/openapi.json', type: /^application\/json/, csp: defaultCsp, noStore: true, status: 200 },
      { path: '/inventory/export.csv', type: /^text\/csv/, csp: defaultCsp, noStore: true, status: 200, csv: 'inventory' },
      { path: '/orders/export.csv', type: /^text\/csv/, csp: defaultCsp, noStore: true, status: 200, csv: 'orders' },
      { path: '/docs', type: /^text\/html/, csp: docsCsp, noStore: false, status: 200 },
      { path: '/redoc', type: /^text\/html/, csp: docsCsp, noStore: false, status: 200 },
      { path: '/nope', type: /^application\/json/, csp: defaultCsp, noStore: true, status: 404 },
      { path: '/orders', method: 'POST', body: '{"store_id": "x"}', type: /^application\/json/, csp: defaultCsp, noStore: true, status: 422 },
    ];
    for (const spec of specs) {
      const method = spec.method || 'GET';
      await this.step(
        `headers ${method} ${spec.path}`,
        async () => {
          const r = await fetch(base + spec.path, {
            method,
            headers: spec.body ? { 'content-type': 'application/json' } : {},
            body: spec.body,
            redirect: 'manual',
          });
          const text = await r.text();
          const actual = {};
          r.headers.forEach((value, name) => {
            actual[name] = value;
          });
          this.report.headers[`${method} ${spec.path}`] = { status: r.status, headers: actual };
          const problems = [];
          const got = (name) => r.headers.get(name);
          if (r.status !== spec.status) problems.push(`status ${r.status}, expected ${spec.status}`);
          if (got('content-security-policy') !== spec.csp) {
            problems.push(`Content-Security-Policy ${json(got('content-security-policy'))} != expected ${json(spec.csp)}`);
          }
          for (const [name, value] of Object.entries(securityHeaders)) {
            if (got(name) !== value) problems.push(`${name} ${json(got(name))} != expected ${json(value)}`);
          }
          const contentType = got('content-type') || '';
          if (!spec.type.test(contentType)) problems.push(`Content-Type ${json(contentType)} does not match ${spec.type}`);
          const cacheControl = got('cache-control');
          if (spec.noStore && cacheControl !== 'no-store') problems.push(`Cache-Control ${json(cacheControl)}, expected no-store`);
          if (!spec.noStore && cacheControl === 'no-store') problems.push('Cache-Control: no-store on a cacheable asset');
          if (!got('x-request-id')) problems.push('X-Request-ID missing');
          if (!got('x-response-time-ms')) problems.push('X-Response-Time-ms missing');
          let extra = '';
          if (spec.csv) {
            const lines = text.split('\n').filter((l) => l !== '');
            const disposition = got('content-disposition') || '';
            const rowCount = Number(got('x-row-count'));
            if (!new RegExp(`^attachment; filename="${spec.csv}-\\d{8}\\.csv"$`).test(disposition)) problems.push(`Content-Disposition ${json(disposition)}`);
            if (lines[0] !== CSV_HEADERS[spec.csv]) problems.push(`CSV header row ${json(lines[0])} != expected ${json(CSV_HEADERS[spec.csv])}`);
            if (!Number.isInteger(rowCount) || rowCount !== lines.length - 1) problems.push(`X-Row-Count ${json(got('x-row-count'))} != ${lines.length - 1} data rows`);
            if (lines.length - 1 < 1) problems.push('CSV has no data rows');
            extra = ` · ${lines.length - 1} data rows · ${disposition}`;
          }
          if (problems.length) throw new Error(problems.join('; '));
          return `${r.status} ${contentType} · CSP ok · ${Object.keys(securityHeaders).length} security headers ok · Cache-Control ${cacheControl || '(none)'}${extra}`;
        },
        { stage: 'headers' },
      );
    }
  }

  // ------------------------------------------------------------------- stage 2: the enforced-CSP application context
  async openContext(browser, origin) {
    const context = await browser.newContext({
      acceptDownloads: true,
      viewport: { width: 1280, height: 900 },
      recordHar: { path: path.join(this.out, 'session.har'), content: 'omit' },
    });
    await this.installViolationListener(context);
    const page = await context.newPage();
    this.violationSink = this.server.cspViolations;
    this.attachMonitor(page, origin, this.server, []);
    return { context, page };
  }

  // ------------------------------------------------------------------- shared UI helpers
  uiHelpers(page) {
    const T = this.timeout;
    const text = async (sel) => (await page.locator(sel).first().innerText()).trim();
    const count = (sel) => page.locator(sel).count();
    const waitText = (sel, has, timeout = T) => page.locator(sel, { hasText: has }).first().waitFor({ timeout });
    const waitCount = async (sel, op, a, b = 0, timeout = T) => {
      await page.waitForFunction(
        (arg) => {
          const n = document.querySelectorAll(arg.sel).length;
          if (arg.op === 'eq') return n === arg.a;
          if (arg.op === 'gte') return n >= arg.a;
          if (arg.op === 'gt') return n > arg.a;
          if (arg.op === 'lt') return n < arg.a;
          if (arg.op === 'between') return n >= arg.a && n <= arg.b;
          return false;
        },
        { sel, op, a, b },
        { timeout },
      );
      return count(sel);
    };
    const jsonOut = async (sel) => JSON.parse(await text(`${sel} pre.out`));
    const expectEq = (label, actual, expected) => {
      if (actual !== expected) throw new Error(`${label}: got ${json(actual)}, expected ${json(expected)}`);
    };
    const saveDownload = async (dl, stem, saveAs) => {
      const name = dl.suggestedFilename();
      const target = path.join(this.out, 'downloads', saveAs || name);
      await dl.saveAs(target);
      const lines = fs.readFileSync(target, 'utf8').split('\n').filter((l) => l !== '');
      const problems = [];
      if (!new RegExp(`^${stem}-\\d{8}\\.csv$`).test(name)) problems.push(`suggested filename ${json(name)}`);
      if (lines[0] !== CSV_HEADERS[stem]) problems.push(`file header row ${json(lines[0])} != expected ${json(CSV_HEADERS[stem])}`);
      const dataRows = lines.length - 1;
      if (dataRows < 1) problems.push('file has no data rows');
      return { name, target, lines, dataRows, problems };
    };
    return { T, text, count, waitText, waitCount, jsonOut, expectEq, saveDownload };
  }

  // ------------------------------------------------------------------- stage 3: workflows through the UI (server mode)
  async workflowStage(page, base) {
    const { T, text, count, waitText, waitCount, jsonOut, expectEq, saveDownload } = this.uiHelpers(page);
    const TS = Math.max(T * 3, 45000);
    const origin = new URL(base).origin;
    const api = async (method, p, body, headers = {}) => {
      const r = await fetch(origin + p, {
        method,
        headers: { ...(body !== undefined ? { 'content-type': 'application/json' } : {}), ...headers },
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
      const raw = await r.text();
      let parsed = null;
      try {
        parsed = JSON.parse(raw);
      } catch {
        parsed = raw;
      }
      return { status: r.status, headers: r.headers, body: parsed };
    };
    const onHand = async (store, product) => {
      const r = await api('GET', `/inventory/${store}/${product}`);
      return r.status === 200 ? r.body.on_hand : 0;
    };
    const download = async (trigger, { urlPart, stem, saveAs }) => {
      const waitingDownload = page.waitForEvent('download', { timeout: T });
      const waitingResponse = page.waitForResponse((r) => r.url().includes(urlPart), { timeout: T });
      await trigger();
      const [dl, response] = await Promise.all([waitingDownload, waitingResponse]);
      const saved = await saveDownload(dl, stem, saveAs);
      const { name, dataRows, problems } = saved;
      const headers = response.headers();
      if (!/^text\/csv/.test(headers['content-type'] || '')) problems.push(`response Content-Type ${json(headers['content-type'])}`);
      if ((headers['content-disposition'] || '') !== `attachment; filename="${name}"`) problems.push(`response Content-Disposition ${json(headers['content-disposition'])} vs filename ${name}`);
      if (Number(headers['x-row-count']) !== dataRows) problems.push(`X-Row-Count ${json(headers['x-row-count'])} != ${dataRows} data rows in the file`);
      if ((headers['content-security-policy'] || '') !== this.policy.defaultCsp) problems.push('export response is missing the default CSP');
      if ((headers['cache-control'] || '') !== 'no-store') problems.push(`export response Cache-Control ${json(headers['cache-control'])}`);
      await waitText('#toast', new RegExp(`^Downloaded ${escapeRe(name)} · ${dataRows} rows$`));
      if (problems.length) throw new Error(problems.join('; '));
      return { name, dataRows, target: saved.target, headers, status: response.status(), url: response.url() };
    };

    const S = {}; // state shared between steps

    await this.step(
      'load the dashboard in server mode',
      async () => {
        await page.goto(`${origin}/`, { waitUntil: 'load' });
        await waitText('#mode', /Server mode/);
        const banner = await text('#mode');
        const re = /^Server mode — connected to the FastAPI API v(\d+\.\d+\.\d+) · schema v(\d+)/;
        const m = re.exec(banner);
        if (!m) throw new Error(`banner ${json(banner)} does not match ${re}`);
        if (this.policy.version && m[1] !== this.policy.version) throw new Error(`banner version ${m[1]} != app.__version__ ${this.policy.version}`);
        if (m[2] !== '2') throw new Error(`schema version ${m[2]} != 2`);
        return banner;
      },
      { page },
    );

    await this.step(
      `inventory renders the ${SEEDED_ROWS} seeded rows with valuation columns`,
      async () => {
        await waitCount('#inventory-table tbody tr', 'eq', SEEDED_ROWS);
        const first = (await text('#inventory-table tbody tr:first-child')).replace(/\s+/g, ' ');
        if ((first.match(/₹/g) || []).length < 2) throw new Error(`price/value columns missing in first row: ${first}`);
        const meta = await text('#inv-meta');
        if (!meta.startsWith(`(${SEEDED_ROWS} rows`)) throw new Error(`inv-meta ${json(meta)}`);
        return `${SEEDED_ROWS} rows · ${meta} · first: ${first}`;
      },
      { page },
    );

    await this.step(
      'low-stock filter narrows the table and marks every row',
      async () => {
        await page.check('#inv-low');
        await page.waitForFunction(
          (total) => {
            const rows = document.querySelectorAll('#inventory-table tbody tr');
            return rows.length > 0 && rows.length < total && Array.from(rows).every((r) => r.querySelector('td.low'));
          },
          SEEDED_ROWS,
          { timeout: T },
        );
        S.lowStockRows = await count('#inventory-table tbody tr');
        await page.uncheck('#inv-low');
        await waitCount('#inventory-table tbody tr', 'eq', SEEDED_ROWS);
        return `${S.lowStockRows} low-stock rows, all marked; unfiltered again ${SEEDED_ROWS}`;
      },
      { page },
    );

    await this.step(
      'search filters by SKU or name',
      async () => {
        await page.fill('#inv-q', 'rice');
        await waitCount('#inventory-table tbody tr', 'eq', SEEDED_STORES);
        const skus = await page.locator('#inventory-table tbody tr td:nth-child(2)').allInnerTexts();
        if (!skus.every((s) => /RICE/.test(s))) throw new Error(`unexpected rows for "rice": ${skus.join(',')}`);
        await page.fill('#inv-q', '');
        await waitCount('#inventory-table tbody tr', 'eq', SEEDED_ROWS);
        return `"rice" → ${SEEDED_STORES} rows (${skus.join(', ')}); cleared → ${SEEDED_ROWS}`;
      },
      { page },
    );

    await this.step(
      'column sort toggles aria-sort and orders the rows',
      async () => {
        const th = page.locator('#inventory-table th:has(button[data-key="on_hand"])');
        await page.click('#inventory-table button[data-key="on_hand"]');
        expectEq('aria-sort after one click', await th.getAttribute('aria-sort'), 'ascending');
        const nums = (await page.locator('#inventory-table tbody tr td:nth-child(4)').allInnerTexts()).map(Number);
        for (let i = 1; i < nums.length; i += 1) if (nums[i] < nums[i - 1]) throw new Error(`not ascending: ${nums.join(',')}`);
        await page.click('#inventory-table button[data-key="on_hand"]');
        expectEq('aria-sort after two clicks', await th.getAttribute('aria-sort'), 'descending');
        await page.click('#inventory-table button[data-key="store_code"]');
        expectEq('store column sorted again', await page.locator('#inventory-table th:has(button[data-key="store_code"])').getAttribute('aria-sort'), 'ascending');
        return `on_hand ascending ${nums[0]}…${nums[nums.length - 1]}, then descending; store order restored`;
      },
      { page },
    );

    await this.step(
      'receive a delivery with two lines → 201 and both balances rise exactly once',
      async () => {
        S.store = await page.locator('#receipt-store').inputValue();
        S.p1 = await page.locator('#receipt-lines [data-line]:nth-child(1) [data-role=product]').inputValue();
        await page.fill('#receipt-ref', 'PO-BC-1');
        await page.fill('#receipt-lines [data-line]:nth-child(1) [data-role=qty]', '7');
        await page.click('[data-action="add-line"][data-target="receipt-lines"]');
        await waitCount('#receipt-lines [data-line]', 'eq', 2);
        S.p2 = await page.locator('#receipt-lines [data-line]:nth-child(2) [data-role=product] option').nth(1).getAttribute('value');
        await page.selectOption('#receipt-lines [data-line]:nth-child(2) [data-role=product]', S.p2);
        await page.fill('#receipt-lines [data-line]:nth-child(2) [data-role=qty]', '3');
        const before1 = await onHand(S.store, S.p1);
        const before2 = await onHand(S.store, S.p2);
        await page.click('#receipt-form button[type=submit]');
        await waitText('#receipt-out', 'HTTP 201');
        await waitText('#toast', 'Receipt PO-BC-1 posted');
        const body = await jsonOut('#receipt-out');
        expectEq('receipt reference', body.reference, 'PO-BC-1');
        expectEq('receipt lines', body.lines.length, 2);
        expectEq('line 1 on_hand', body.lines[0].on_hand, before1 + 7);
        expectEq('line 2 on_hand', body.lines[1].on_hand, before2 + 3);
        expectEq('API on_hand line 1', await onHand(S.store, S.p1), before1 + 7);
        expectEq('API on_hand line 2', await onHand(S.store, S.p2), before2 + 3);
        S.afterReceipt1 = before1 + 7;
        return `store ${S.store}: product ${S.p1} ${before1}→${before1 + 7}, product ${S.p2} ${before2}→${before2 + 3}`;
      },
      { page },
    );

    await this.step(
      'movements drawer shows the receipt with balance_after; Older pages by keyset; Escape closes and restores focus',
      async () => {
        await page.click(`#inventory-table tbody tr[data-store="${S.store}"][data-product="${S.p1}"] [data-action="movements"]`);
        await page.locator('#drawer:not([hidden])').waitFor({ timeout: T });
        await waitCount('#drawer-table tbody tr', 'gte', 2);
        const head = await text('#drawer-table thead');
        if (!/Balance after/.test(head)) throw new Error('balance_after column missing');
        const cells = await page.locator('#drawer-table tbody tr:first-child td').allInnerTexts();
        expectEq('latest reference', cells[3].trim(), 'receipt:PO-BC-1');
        expectEq('latest delta', cells[1].trim(), '+7');
        expectEq('latest balance_after', Number(cells[4]), S.afterReceipt1);
        const balances = await page.locator('#drawer-table tbody tr td:nth-child(5)').allInnerTexts();
        if (balances.some((b) => !/^-?\d+$/.test(b.trim()))) throw new Error(`a balance_after cell is not numeric: ${balances.join(',')}`);
        const rows = await count('#drawer-table tbody tr');
        const older = page.waitForResponse((r) => /\/movements\?.*before_id=\d+/.test(r.url()), { timeout: T });
        await page.click('#drawer-older');
        const olderResponse = await older;
        expectEq('older page status', olderResponse.status(), 200);
        await waitText('#drawer-meta', /movements|older/);
        const meta = await text('#drawer-meta');
        await page.keyboard.press('Escape');
        await page.waitForFunction(() => document.getElementById('drawer').hidden, null, { timeout: T });
        const focused = await page.evaluate(() => `${document.activeElement.tagName}[data-action=${document.activeElement.dataset.action}]`);
        if (!/^BUTTON\[data-action=movements\]/.test(focused)) throw new Error(`focus after Escape is on ${focused}`);
        return `${rows} rows · latest ${cells[3].trim()} ${cells[1].trim()} → balance ${cells[4].trim()} · Older → ${new URL(olderResponse.url()).search} → ${meta} · focus back on ${focused}`;
      },
      { page },
    );

    await this.step(
      'adjust with a stale expected_version → 409 version_conflict, then with the current version → 200',
      async () => {
        const store = await page.locator('#adjust-store').inputValue();
        const product = await page.locator('#adjust-product').inputValue();
        await page.fill('#adjust-delta', '1');
        await page.fill('#adjust-version', '0');
        await page.click('#adjust-form button[type=submit]');
        await waitText('#adjust-out', 'HTTP 409');
        const stale = await text('#adjust-out .error-line');
        if (!/version_conflict/.test(stale)) throw new Error(`409 without version_conflict code: ${stale}`);
        const row = await api('GET', `/inventory/${store}/${product}`);
        await page.click('[data-action="adjust-fill-version"]');
        await page.waitForFunction((v) => document.querySelector('#adjust-version').value === String(v), row.body.version, { timeout: T });
        await page.click('#adjust-form button[type=submit]');
        await waitText('#adjust-out', 'HTTP 200');
        const body = await jsonOut('#adjust-out');
        expectEq('version after adjust', body.version, row.body.version + 1);
        expectEq('on_hand after adjust', body.on_hand, row.body.on_hand + 1);
        return `${stale} · then version ${row.body.version}→${body.version}, on_hand ${row.body.on_hand}→${body.on_hand}`;
      },
      { page },
    );

    await this.step(
      'orders view lists the seeded orders with status badges and pages',
      async () => {
        await page.click('#tab-orders');
        await page.locator('#view-orders:not([hidden])').waitFor({ timeout: T });
        await waitCount('#orders-table tbody tr', 'eq', PAGE_SIZE);
        const pager = await text('#orders-page');
        const m = new RegExp(`^1${DASH}${PAGE_SIZE} of (\\d+)$`).exec(pager);
        if (!m || Number(m[1]) < 20) throw new Error(`pager ${json(pager)}`);
        const badges = await page.locator('#orders-table tbody tr td:nth-child(3) .badge').allInnerTexts();
        if (badges.length !== PAGE_SIZE || !badges.every((b) => /^(placed|cancelled|fulfilled)$/.test(b))) throw new Error(`badges ${json(badges)}`);
        await page.click('#orders-next');
        await waitText('#orders-page', new RegExp(`^${PAGE_SIZE + 1}${DASH}${2 * PAGE_SIZE} of`));
        await page.click('#orders-prev');
        await waitText('#orders-page', new RegExp(`^1${DASH}${PAGE_SIZE} of`));
        return `${pager} · badges ${[...new Set(badges)].join('/')} · Next → ${PAGE_SIZE + 1}–${2 * PAGE_SIZE} · Prev → 1–${PAGE_SIZE}`;
      },
      { page },
    );

    await this.step(
      'place an order with an Idempotency-Key → 201; replay → 200 + Idempotent-Replayed: true; stock decremented once',
      async () => {
        S.orderStore = await page.locator('#order-store').inputValue();
        S.orderProduct = await page.locator('#order-lines [data-line]:nth-child(1) [data-role=product]').inputValue();
        await page.fill('#order-lines [data-line]:nth-child(1) [data-role=qty]', '1');
        await page.click('[data-action="generate-key"][data-target="order-key"]');
        S.orderKey = await page.locator('#order-key').inputValue();
        if (!S.orderKey) throw new Error('generate-key left the key empty');
        S.beforeOrder = await onHand(S.orderStore, S.orderProduct);
        await page.click('#order-form button[type=submit]');
        await waitText('#order-out', 'HTTP 201');
        await waitText('#order-out', 'Idempotent-Replayed: false');
        const first = await jsonOut('#order-out');
        expectEq('stored key', first.idempotency_key, S.orderKey);
        S.orderId = first.id;
        expectEq('on_hand after order', await onHand(S.orderStore, S.orderProduct), S.beforeOrder - 1);
        await page.click('#order-form button[type=submit]');
        await waitText('#order-out', 'Idempotent-Replayed: true');
        const out = await text('#order-out .status');
        if (!/HTTP 200/.test(out)) throw new Error(`replay status line: ${out}`);
        const replay = await jsonOut('#order-out');
        expectEq('replayed id', replay.id, S.orderId);
        expectEq('on_hand after replay', await onHand(S.orderStore, S.orderProduct), S.beforeOrder - 1);
        return `order #${S.orderId} key ${S.orderKey}: 201 then 200 + Idempotent-Replayed: true · on_hand ${S.beforeOrder}→${S.beforeOrder - 1} once`;
      },
      { page },
    );

    await this.step(
      'same key with a different body → 422 idempotency_key_reuse, nothing written',
      async () => {
        await page.fill('#order-lines [data-line]:nth-child(1) [data-role=qty]', '2');
        await page.click('#order-form button[type=submit]');
        await waitText('#order-out', 'HTTP 422');
        const line = await text('#order-out .error-line');
        if (!/idempotency_key_reuse/.test(line)) throw new Error(`422 without idempotency_key_reuse: ${line}`);
        expectEq('on_hand unchanged', await onHand(S.orderStore, S.orderProduct), S.beforeOrder - 1);
        return line;
      },
      { page },
    );

    await this.step(
      'oversell → 409 insufficient_stock with nothing decremented',
      async () => {
        await page.fill('#order-key', '');
        await page.fill('#order-lines [data-line]:nth-child(1) [data-role=qty]', '1000');
        await page.click('#order-form button[type=submit]');
        await waitText('#order-out', 'HTTP 409');
        const line = await text('#order-out .error-line');
        if (!/insufficient_stock/.test(line)) throw new Error(`409 without insufficient_stock: ${line}`);
        expectEq('on_hand unchanged', await onHand(S.orderStore, S.orderProduct), S.beforeOrder - 1);
        await page.fill('#order-lines [data-line]:nth-child(1) [data-role=qty]', '1');
        return line;
      },
      { page },
    );

    await this.step(
      'cancel restocks the order; the status filter shows placed orders; fulfil one sets updated_at',
      async () => {
        await page.click('[data-action="orders-refresh"]');
        const row = page.locator(`#orders-table tbody tr:has(td:first-child:text-is("${S.orderId}"))`);
        await row.waitFor({ timeout: T });
        await row.locator('[data-action="order-cancel"]').click();
        await waitText('#orders-out', 'HTTP 200');
        const cancelled = await jsonOut('#orders-out');
        expectEq('cancelled id', cancelled.id, S.orderId);
        expectEq('cancelled status', cancelled.status, 'cancelled');
        await page.waitForFunction(
          (id) => {
            const r = Array.from(document.querySelectorAll('#orders-table tbody tr')).find((tr) => tr.children[0].textContent === String(id));
            return r && /cancelled/.test(r.children[2].textContent);
          },
          S.orderId,
          { timeout: T },
        );
        expectEq('on_hand restored', await onHand(S.orderStore, S.orderProduct), S.beforeOrder);
        await page.selectOption('#order-status', 'placed');
        await page.waitForFunction(
          () => {
            const rows = Array.from(document.querySelectorAll('#orders-table tbody tr'));
            return rows.length > 0 && rows.every((tr) => /placed/.test(tr.children[2].textContent) && tr.querySelector('[data-action="order-fulfil"]'));
          },
          null,
          { timeout: T },
        );
        const fulfil = page.locator('#orders-table tbody [data-action="order-fulfil"]').first();
        const fulfilId = Number(await fulfil.getAttribute('data-id'));
        await fulfil.click();
        await waitText('#orders-out', 'fulfilled');
        const fulfilled = await jsonOut('#orders-out');
        expectEq('fulfilled id', fulfilled.id, fulfilId);
        expectEq('fulfilled status', fulfilled.status, 'fulfilled');
        if (!fulfilled.updated_at || fulfilled.updated_at <= fulfilled.created_at) throw new Error(`updated_at ${fulfilled.updated_at} not after created_at ${fulfilled.created_at}`);
        expectEq('API status', (await api('GET', `/orders/${fulfilId}`)).body.status, 'fulfilled');
        await page.selectOption('#order-status', '');
        await waitText('#orders-page', new RegExp(`^1${DASH}${PAGE_SIZE} of`));
        return `order #${S.orderId} cancelled (on_hand back to ${S.beforeOrder}); status=placed filter; order #${fulfilId} fulfilled, updated_at ${fulfilled.updated_at}`;
      },
      { page },
    );

    await this.step(
      'orders CSV export is a real download (response headers and file content verified)',
      async () => {
        const result = await download(() => page.click('[data-action="export-orders"]'), { urlPart: '/orders/export.csv', stem: 'orders' });
        return `${result.name} · ${result.dataRows} data rows · ${result.headers['content-type']} · X-Row-Count ${result.headers['x-row-count']}`;
      },
      { page },
    );

    await this.step(
      'transfer with an Idempotency-Key → 201, replay → 200 + Idempotent-Replayed: true, stock moved once, listed',
      async () => {
        await page.click('#tab-transfers');
        await page.locator('#view-transfers:not([hidden])').waitFor({ timeout: T });
        const from = await page.locator('#transfer-from').inputValue();
        const to = await page.locator('#transfer-to').inputValue();
        if (from === to) throw new Error('from and to default to the same store');
        const product = await page.locator('#transfer-product').inputValue();
        await page.fill('#transfer-qty', '3');
        await page.click('[data-action="generate-key"][data-target="transfer-key"]');
        const key = await page.locator('#transfer-key').inputValue();
        const beforeFrom = await onHand(from, product);
        const beforeTo = await onHand(to, product);
        await page.click('#transfer-form button[type=submit]');
        await waitText('#transfer-out', 'HTTP 201');
        await waitText('#transfer-out', 'Idempotent-Replayed: false');
        const created = await jsonOut('#transfer-out');
        expectEq('transfer key', created.idempotency_key, key);
        expectEq('transfer quantity', created.quantity, 3);
        await page.click('#transfer-form button[type=submit]');
        await waitText('#transfer-out', 'Idempotent-Replayed: true');
        if (!/HTTP 200/.test(await text('#transfer-out .status'))) throw new Error('replay did not answer 200');
        const replay = await jsonOut('#transfer-out');
        expectEq('replayed transfer id', replay.id, created.id);
        expectEq('source on_hand', await onHand(from, product), beforeFrom - 3);
        expectEq('destination on_hand', await onHand(to, product), beforeTo + 3);
        const listed = page.locator('#transfers-table tbody tr', { hasText: key }).first();
        await listed.waitFor({ timeout: T });
        const listedText = (await listed.innerText()).replace(/\s+/g, ' ');
        if (!listedText.includes('→') || !/\b3\b/.test(listedText)) throw new Error(`listed row ${json(listedText)}`);
        const viaApi = await api('GET', `/transfers/${created.id}`);
        expectEq('GET /transfers/{id}', viaApi.status, 200);
        expectEq('GET /transfers/{id} from.on_hand', viaApi.body.from.on_hand, beforeFrom - 3);
        return `transfer #${created.id} store ${from}→${to} product ${product} ×3 (key ${key}): 201 then 200 replay · ${beforeFrom}→${beforeFrom - 3} / ${beforeTo}→${beforeTo + 3} · listed: ${listedText}`;
      },
      { page },
    );

    await this.step(
      'reports: summary cards and per-store table, reorder table with days selector, sales chart and top products',
      async () => {
        await page.click('#tab-reports');
        await page.locator('#view-reports:not([hidden])').waitFor({ timeout: T });
        await waitCount('#summary-cards .kpi', 'eq', 10);
        await waitCount('#summary-table tbody tr', 'eq', SEEDED_STORES);
        const storeCodes = await page.locator('#summary-table tbody tr td:nth-child(1)').allInnerTexts();
        const kpis = (await page.locator('#summary-cards .kpi').allInnerTexts()).map((k) => k.replace(/\s+/g, ' '));
        if (kpis.some((k) => /—/.test(k))) throw new Error(`a summary card is empty: ${json(kpis)}`);
        await waitText('#reorder-meta', '30-day window');
        const reorderRows = await count('#reorder-table tbody tr');
        if (reorderRows < 1 || /nothing is at or below/.test(await text('#reorder-table tbody'))) throw new Error('reorder table is empty');
        const sold = (await text('#reorder-table tbody tr:first-child td:nth-child(6)')).trim();
        if (!/^\d+$/.test(sold)) throw new Error(`sold_window cell ${json(sold)} is not numeric`);
        await page.selectOption('#reorder-days', '7');
        await waitText('#reorder-meta', '7-day window');
        const reorderMeta = await text('#reorder-meta');
        await page.locator('#sales-chart svg.spark').waitFor({ timeout: T });
        await waitCount('#sales-chart svg.spark rect.bar', 'eq', 30);
        await page.locator('#sales-chart svg.spark path.trend').waitFor({ timeout: T });
        const topRows = await waitCount('#sales-products-table tbody tr', 'gte', 1);
        if (/no sales|HTTP/.test(await text('#sales-products-table tbody'))) throw new Error('top products table shows an error or no sales');
        await page.selectOption('#sales-days', '7');
        await waitCount('#sales-chart svg.spark rect.bar', 'eq', 7);
        const salesMeta = await text('#sales-meta');
        if (!/^\(\d+ orders · ₹[\d,.]+ · last 7 days\)$/.test(salesMeta)) throw new Error(`sales-meta ${json(salesMeta)}`);
        return `${kpis.length} KPIs (${kpis.slice(0, 3).join(' | ')}) · stores ${storeCodes.join(',')} · reorder ${reorderRows} rows, first sold_window ${sold} → ${reorderMeta} · top products ${topRows} · sales bars 30→7, ${salesMeta}`;
      },
      { page },
    );

    await this.step(
      'integrity audit reports a consistent ledger and the rebuild answers 200 ok',
      async () => {
        await page.click('[data-action="integrity"]');
        await waitText('#integrity-out', 'ledger consistent');
        const audit = await jsonOut('#integrity-out');
        expectEq('integrity ok', audit.ok, true);
        if (audit.checked < SEEDED_ROWS) throw new Error(`checked ${audit.checked} < ${SEEDED_ROWS}`);
        const rebuildResponse = page.waitForResponse((r) => /\/integrity\/rebuild$/.test(r.url()), { timeout: T });
        await page.click('[data-action="integrity-rebuild"]');
        expectEq('rebuild status', (await rebuildResponse).status(), 200);
        await waitText('#integrity-out pre.out', '"backfilled"');
        const rebuilt = await jsonOut('#integrity-out');
        expectEq('rebuild ok', rebuilt.ok, true);
        expectEq('rebuild fixed nothing', Array.isArray(rebuilt.fixed) ? rebuilt.fixed.length : -1, 0);
        return `integrity ok (checked ${audit.checked}, schema v${audit.schema_version}) · rebuild ok (fixed 0, backfilled ${rebuilt.backfilled})`;
      },
      { page },
    );

    await this.step(
      'reorder CSV export is a real download (response headers and file content verified)',
      async () => {
        const result = await download(() => page.click('[data-action="export-reorder"]'), { urlPart: '/reports/reorder.csv', stem: 'reorder' });
        if (!/days=7/.test(result.url)) throw new Error(`export did not carry the selected window: ${result.url}`);
        return `${result.name} · ${result.dataRows} data rows · ${result.headers['content-disposition']} · ${new URL(result.url).search}`;
      },
      { page },
    );

    await this.step(
      `catalog renders the ${SEEDED_PRODUCTS} products and ${SEEDED_STORES} stores`,
      async () => {
        await page.click('#tab-catalog');
        await page.locator('#view-catalog:not([hidden])').waitFor({ timeout: T });
        await waitCount('#products-table tbody tr[data-id]', 'eq', SEEDED_PRODUCTS);
        await waitCount('#stores-table tbody tr[data-id]', 'eq', SEEDED_STORES);
        const active = await page.locator('#products-table tbody .badge-ok', { hasText: 'active' }).count();
        expectEq('active badges', active, SEEDED_PRODUCTS);
        return `${SEEDED_PRODUCTS} products (all active) · ${SEEDED_STORES} stores`;
      },
      { page },
    );

    await this.step(
      'patch a product price inline → 200; an existing order keeps its snapshot price',
      async () => {
        const orders = await api('GET', '/orders?limit=100');
        const withLines = orders.body.items.find((o) => o.lines && o.lines.length);
        if (!withLines) throw new Error('no order with lines');
        const line = withLines.lines[0];
        const product = await api('GET', `/products/${line.product_id}`);
        const newPrice = product.body.price_cents + 1000;
        await page.click(`#products-table tbody tr[data-id="${line.product_id}"] [data-action="product-edit"]`);
        await page.fill(`#products-table tr.editing[data-id="${line.product_id}"] [data-field="price_cents"]`, String(newPrice));
        await page.click(`#products-table [data-action="product-save"][data-id="${line.product_id}"]`);
        await waitText('#products-out', 'HTTP 200');
        const patched = await jsonOut('#products-out');
        expectEq('patched price', patched.price_cents, newPrice);
        if (!patched.updated_at) throw new Error('updated_at not set by the PATCH');
        const after = await api('GET', `/orders/${withLines.id}`);
        const sameLine = after.body.lines.find((l) => l.product_id === line.product_id);
        expectEq('order line unit_price_cents unchanged', sameLine.unit_price_cents, line.unit_price_cents);
        expectEq('product price via API', (await api('GET', `/products/${line.product_id}`)).body.price_cents, newPrice);
        S.patchedProduct = line.product_id;
        return `product ${line.product_id} (${product.body.sku}) ${product.body.price_cents}→${newPrice}; order #${withLines.id} line still ${line.unit_price_cents}`;
      },
      { page },
    );

    await this.step(
      'soft delete a product → 204, ordering it → 409 product_inactive, include-inactive shows it, reactivate → 200',
      async () => {
        const ids = await page.locator('#products-table tbody tr[data-id]').evaluateAll((rows) => rows.map((r) => r.dataset.id));
        const target = ids.filter((id) => Number(id) !== S.patchedProduct).pop();
        await page.click(`#products-table tbody tr[data-id="${target}"] [data-action="product-deactivate"]`);
        await waitText('#products-out', 'HTTP 204');
        await waitCount('#products-table tbody tr[data-id]', 'eq', SEEDED_PRODUCTS - 1);
        if (await count(`#products-table tbody tr[data-id="${target}"]`)) throw new Error('inactive product still listed by default');
        const attempt = await api('POST', '/orders', { store_id: Number(S.store), lines: [{ product_id: Number(target), quantity: 1 }] });
        expectEq('order of inactive product status', attempt.status, 409);
        expectEq('order of inactive product code', attempt.body.code, 'product_inactive');
        await page.check('#catalog-inactive');
        await waitCount('#products-table tbody tr[data-id]', 'eq', SEEDED_PRODUCTS);
        const row = page.locator(`#products-table tbody tr[data-id="${target}"]`);
        expectEq('badge of the soft-deleted product', (await row.locator('.badge').innerText()).trim(), 'inactive');
        await row.locator('[data-action="product-activate"]').click();
        await waitText('#products-out', 'HTTP 200');
        const reactivated = await jsonOut('#products-out');
        expectEq('reactivated active flag', reactivated.active, true);
        await page.uncheck('#catalog-inactive');
        await waitCount('#products-table tbody tr[data-id]', 'eq', SEEDED_PRODUCTS);
        expectEq('API active flag', (await api('GET', `/products/${target}`)).body.active, true);
        return `product ${target}: DELETE → 204, POST /orders → 409 product_inactive, listed with include_inactive, PATCH active → 200`;
      },
      { page },
    );

    await this.step(
      'patch a store region inline → 200',
      async () => {
        const ids = await page.locator('#stores-table tbody tr[data-id]').evaluateAll((rows) => rows.map((r) => r.dataset.id));
        const target = ids[ids.length - 1];
        await page.click(`#stores-table tbody tr[data-id="${target}"] [data-action="store-edit"]`);
        await page.fill(`#stores-table tr.editing[data-id="${target}"] [data-field="region"]`, 'North');
        await page.click(`#stores-table [data-action="store-save"][data-id="${target}"]`);
        await waitText('#stores-out', 'HTTP 200');
        const patched = await jsonOut('#stores-out');
        expectEq('patched region', patched.region, 'North');
        await waitText(`#stores-table tbody tr[data-id="${target}"]`, 'North');
        expectEq('API region', (await api('GET', `/stores/${target}`)).body.region, 'North');
        return `store ${target} (${patched.code}) region → North`;
      },
      { page },
    );

    await this.step(
      'XSS probe: a hostile product name is rendered as text, never as markup',
      async () => {
        await page.fill('#product-sku', 'XSS-PROBE-1');
        await page.fill('#product-name', HOSTILE_NAME);
        await page.click('#product-form button[type=submit]');
        await waitText('#product-out', 'HTTP 201');
        await waitCount('#products-table tbody tr[data-id]', 'eq', SEEDED_PRODUCTS + 1);
        const pwned = await page.evaluate(() => window.__pwned === 1);
        const imgs = await count('#products-table img');
        if (pwned || imgs) throw new Error('hostile name executed or rendered as markup');
        const shown = await page.locator('#products-table tbody tr[data-id]').last().locator('td:nth-child(3)').innerText();
        expectEq('name rendered verbatim', shown, HOSTILE_NAME);
        return 'rendered verbatim as text: no <img>, no script execution';
      },
      { page },
    );

    await this.step(
      'scenarios: every button passes with the expected number of steps',
      async () => {
        await page.click('#tab-scenarios');
        await page.locator('#view-scenarios:not([hidden])').waitFor({ timeout: T });
        const verdicts = [];
        const problems = [];
        for (const [name, expectedSteps] of Object.entries(SCENARIO_STEPS)) {
          await page.click(`[data-action="run-scenario"][data-scenario="${name}"]`);
          await page.locator(`#scenario-${name} > .status > .badge:is(.badge-ok, .badge-danger)`).waitFor({ timeout: TS });
          const badge = await text(`#scenario-${name} > .status > .badge`);
          const stepBadges = await page.locator(`#scenario-${name} li.step .status .badge`).allInnerTexts();
          verdicts.push(`${name}=${badge} (${stepBadges.length} steps)`);
          if (badge !== 'pass') problems.push(`${name} badge ${badge}`);
          if (stepBadges.length !== expectedSteps) problems.push(`${name} has ${stepBadges.length} steps, expected ${expectedSteps}`);
          if (stepBadges.some((b) => b !== 'pass')) {
            const steps = await page.locator(`#scenario-${name} li.step`).allInnerTexts();
            problems.push(...steps.filter((s) => /^fail/.test(s)).map((s) => `${name}: ${s.split('\n').slice(0, 3).join(' | ')}`));
          }
        }
        if (problems.length) throw new Error(problems.join('; '));
        return verdicts.join(' · ');
      },
      { page },
    );

    await this.step(
      'scenarios: Run all re-runs the five scenarios and every one passes again',
      async () => {
        const audit = page.waitForResponse((r) => /\/movements\?limit=10/.test(r.url()), { timeout: TS });
        await page.click('[data-action="run-all-scenarios"]');
        await audit;
        await page.locator('#scenario-audit > .status > .badge:is(.badge-ok, .badge-danger)').waitFor({ timeout: TS });
        const badges = [];
        for (const name of Object.keys(SCENARIO_STEPS)) badges.push(`${name}=${await text(`#scenario-${name} > .status > .badge`)}`);
        if (badges.some((b) => !/=pass$/.test(b))) throw new Error(badges.join(', '));
        return badges.join(' · ');
      },
      { page },
    );

    await this.step(
      'console: request history, details toggle, copy as curl renders a curl command, clear',
      async () => {
        await page.click('#tab-console');
        await page.locator('#view-console:not([hidden])').waitFor({ timeout: T });
        const rows = await waitCount('#console-table tbody tr', 'gt', 10);
        await page.locator('#console-table [data-action="console-toggle"]').first().click();
        await page.locator('#console-table tr.details').waitFor({ timeout: T });
        await page.locator('#console-table [data-action="console-curl"]').first().click();
        await page.locator('#console-curl:not([hidden])').waitFor({ timeout: T });
        const curl = await text('#console-curl');
        if (!curl.startsWith('curl ') || !curl.includes(origin)) throw new Error(`curl text ${json(curl.slice(0, 120))}`);
        await page.click('[data-action="console-clear"]');
        await waitText('#console-table tbody', 'no requests yet');
        return `${rows} entries · ${curl.slice(0, 100)}`;
      },
      { page },
    );

    await this.step(
      'inventory CSV exports (all rows, then the low-stock filter) are real downloads with matching headers and content',
      async () => {
        await page.click('#tab-inventory');
        await page.locator('#view-inventory:not([hidden])').waitFor({ timeout: T });
        await waitCount('#inventory-table tbody tr', 'eq', SEEDED_ROWS);
        const full = await download(() => page.click('[data-action="export-inventory"]'), { urlPart: '/inventory/export.csv', stem: 'inventory' });
        expectEq('full export rows', full.dataRows, SEEDED_ROWS);
        await page.check('#inv-low');
        await waitCount('#inventory-table tbody tr', 'lt', SEEDED_ROWS);
        const tableRows = await count('#inventory-table tbody tr');
        const low = await download(() => page.click('[data-action="export-inventory"]'), {
          urlPart: '/inventory/export.csv?',
          stem: 'inventory',
          saveAs: full.name.replace(/\.csv$/, '.low-stock.csv'),
        });
        if (!/low_stock=true/.test(low.url)) throw new Error(`low-stock export URL ${low.url} lacks low_stock=true`);
        expectEq('low-stock export rows == table rows', low.dataRows, tableRows);
        await page.uncheck('#inv-low');
        await waitCount('#inventory-table tbody tr', 'eq', SEEDED_ROWS);
        return `${full.name}: ${full.dataRows} rows · low-stock export (${new URL(low.url).search}): ${low.dataRows} rows = table ${tableRows}`;
      },
      { page },
    );

    await this.step(
      'movements CSV export from the drawer is a real download with matching headers and content',
      async () => {
        await page.click('#inventory-table tbody tr:first-child [data-action="movements"]');
        await page.locator('#drawer:not([hidden])').waitFor({ timeout: T });
        await waitCount('#drawer-table tbody tr', 'gte', 1);
        const result = await download(() => page.click('[data-action="export-movements"]'), { urlPart: '/movements/export.csv', stem: 'movements' });
        const drawerRows = await count('#drawer-table tbody tr');
        if (result.dataRows < drawerRows) throw new Error(`export has ${result.dataRows} rows but the drawer shows ${drawerRows}`);
        if (!/store_id=\d+&product_id=\d+/.test(result.url)) throw new Error(`movements export URL ${result.url} lacks the pair filter`);
        await page.click('#drawer-close');
        await page.waitForFunction(() => document.getElementById('drawer').hidden, null, { timeout: T });
        return `${result.name} · ${result.dataRows} data rows (drawer shows ${drawerRows}) · ${new URL(result.url).search}`;
      },
      { page },
    );

    await this.step(
      'keyboard: Tab reaches the tab bar, arrows switch views, Tab reaches Refresh, Enter activates it, focus ring visible',
      async () => {
        await page.click('h1');
        for (let i = 0; i < 3; i += 1) await page.keyboard.press('Tab');
        expectEq('focus after three Tabs', await page.evaluate(() => document.activeElement.id), 'tab-inventory');
        await page.keyboard.press('ArrowRight');
        expectEq('selected tab after ArrowRight', await page.evaluate(() => document.querySelector('[role=tab][aria-selected=true]').id), 'tab-orders');
        expectEq('focused tab after ArrowRight', await page.evaluate(() => document.activeElement.id), 'tab-orders');
        await page.locator('#view-orders:not([hidden])').waitFor({ timeout: T });
        for (let i = 0; i < 3; i += 1) await page.keyboard.press('Tab');
        const focused = await page.evaluate(() => ({
          action: document.activeElement.dataset.action,
          focusVisible: document.activeElement.matches(':focus-visible'),
          outline: getComputedStyle(document.activeElement).outlineStyle,
          width: getComputedStyle(document.activeElement).outlineWidth,
        }));
        expectEq('focused control', focused.action, 'orders-refresh');
        if (!focused.focusVisible || focused.outline === 'none' || focused.width === '0px') throw new Error(`no visible focus ring: ${json(focused)}`);
        this.shots += 1;
        const ring = `${String(this.shots).padStart(2, '0')}-focus-ring.png`;
        await page.screenshot({ path: path.join(this.out, ring) });
        const refreshed = page.waitForResponse((r) => /\/orders\?/.test(r.url()), { timeout: T });
        await page.keyboard.press('Enter');
        const response = await refreshed;
        return `3×Tab → #tab-inventory · ArrowRight → #tab-orders · 3×Tab → [data-action=orders-refresh] (outline ${focused.outline} ${focused.width}, screenshot ${ring}) · Enter → GET /orders ${response.status()}`;
      },
      { page },
    );

    await this.step(
      'dark mode (prefers-color-scheme: dark) applies the dark tokens',
      async () => {
        await page.emulateMedia({ colorScheme: 'dark' });
        await page.waitForFunction((bg) => getComputedStyle(document.body).backgroundColor === bg, DARK_BG, { timeout: T });
        const colors = await page.evaluate(() => ({ bg: getComputedStyle(document.body).backgroundColor, ink: getComputedStyle(document.body).color }));
        this.shots += 1;
        const file = `${String(this.shots).padStart(2, '0')}-dark-mode.png`;
        await page.screenshot({ path: path.join(this.out, file) });
        await page.emulateMedia({ colorScheme: 'light' });
        await page.waitForFunction((bg) => getComputedStyle(document.body).backgroundColor !== bg, DARK_BG, { timeout: T });
        return `body background ${colors.bg}, text ${colors.ink} (screenshot ${file}); light restored`;
      },
      { page, screenshot: false },
    );

    await this.step(
      'built-in accessibility checks (enforced-CSP context)',
      async () => {
        const result = await page.evaluate(() => {
          const problems = [];
          if (document.querySelectorAll('h1').length !== 1) problems.push(`h1 count ${document.querySelectorAll('h1').length}`);
          if (!document.documentElement.lang) problems.push('html[lang] missing');
          if (!document.querySelector('[aria-live]')) problems.push('no aria-live region');
          const ids = Array.from(document.querySelectorAll('[id]')).map((e) => e.id);
          if (new Set(ids).size !== ids.length) problems.push('duplicate ids');
          const controls = document.querySelectorAll('input, select, textarea');
          for (const c of controls) {
            if (c.type === 'hidden') continue;
            const labelled = c.closest('label') || (c.id && document.querySelector(`label[for="${c.id}"]`)) || c.getAttribute('aria-label') || c.getAttribute('aria-labelledby');
            if (!labelled) problems.push(`unlabelled control ${c.id || c.outerHTML.slice(0, 60)}`);
          }
          const buttons = document.querySelectorAll('button, a');
          for (const b of buttons) if (!(b.textContent.trim() || b.getAttribute('aria-label'))) problems.push(`unnamed ${b.tagName}`);
          for (const tab of document.querySelectorAll('[role=tab]')) {
            if (!tab.getAttribute('aria-controls') || !document.getElementById(tab.getAttribute('aria-controls'))) problems.push(`tab ${tab.id} without a panel`);
          }
          for (const panel of document.querySelectorAll('[role=tabpanel]')) {
            if (!panel.getAttribute('aria-labelledby')) problems.push(`panel ${panel.id} without aria-labelledby`);
          }
          for (const img of document.querySelectorAll('img')) if (!img.hasAttribute('alt')) problems.push('img without alt');
          return { problems, ids: ids.length, controls: controls.length, buttons: buttons.length };
        });
        if (result.problems.length) throw new Error(result.problems.join('; '));
        return `${result.ids} ids unique · ${result.controls} form controls labelled · ${result.buttons} buttons/links named · one h1 · html[lang] · aria-live present · tabs/panels wired`;
      },
      { page, screenshot: false },
    );

    await this.step(
      'CSP positive control: an injected inline script is blocked by the served policy and reported to the listener',
      async () => {
        this.current.positiveControl = true;
        await page.evaluate(() => {
          const s = document.createElement('script');
          s.textContent = 'window.__inlineRan = 1';
          document.head.appendChild(s);
          s.remove();
        });
        await page.waitForFunction(() => (window.__cspViolations || []).length > 0, null, { timeout: T });
        const ran = await page.evaluate(() => window.__inlineRan === 1);
        const violations = await page.evaluate(() => (window.__cspViolations || []).slice());
        const inline = violations.filter((v) => v.blockedURI === 'inline' && /^script-src/.test(v.effectiveDirective));
        this.report.cspPositiveControl = { ...(this.report.cspPositiveControl || {}), blockedInlineScriptExecuted: ran, violations };
        await sleep(300); // let the console refusal for the blocked script arrive while this step is still current
        if (ran) throw new Error('the inline script executed: the CSP is not enforced on the page');
        if (!inline.length) throw new Error(`no script-src violation captured: ${json(violations)}`);
        const refusals = (this.report.cspPositiveControl.consoleRefusals || []).length;
        return `inline script blocked; ${inline.length} ${inline[0].effectiveDirective} violation(s) captured by the listener, ${refusals} console refusal line(s) (recorded under cspPositiveControl, not counted against the run)`;
      },
      { page, screenshot: false },
    );
  }

  // ------------------------------------------------------------------- responsive measurements (shared by the server-mode stage and pages-demo)
  /** Open `tab`, wait for its content, then measure document overflow and the layout facts the responsive rule asserts. */
  async measureView(page, tab) {
    const T = this.timeout;
    await page.click(`#tab-${tab}`);
    await page.locator(`#view-${tab}:not([hidden])`).waitFor({ timeout: T });
    await page.waitForFunction((selectors) => selectors.every((sel) => document.querySelectorAll(sel).length > 0), VIEW_READY[tab], { timeout: T });
    await sleep(250); // let the last render settle before measuring
    const overflow = await page.evaluate(measureOverflow);
    const layout = await page.evaluate(inspectLayout);
    return { ...overflow, ...layout };
  }

  /** Verdict for one (viewport, view) measurement: problems (empty = pass) and a one-line summary. */
  judgeView(m, tab, width) {
    const problems = [];
    if (m.innerWidth !== width) problems.push(`innerWidth ${m.innerWidth} != viewport ${width}`);
    if (m.overflow > MAX_DOCUMENT_OVERFLOW) {
      const culprits = m.unclipped.map((o) => `${o.element} left ${o.left} right ${o.right} position ${o.position}, containing block ${o.containingBlock}, clipped by nothing`);
      problems.push(
        `document overflow ${m.overflow} px (scrollWidth ${m.scrollWidth}, innerWidth ${m.innerWidth}) > ${MAX_DOCUMENT_OVERFLOW} px; unclipped boxes past the right edge: ${culprits.join('; ') || '(none found — overflow comes from a box this probe does not see)'}`,
      );
    }
    if (m.selected !== `tab-${tab}`) problems.push(`selected tab is ${m.selected}, expected tab-${tab}`);
    if (m.tabCount !== VIEWS.length || !m.tabsVisible) problems.push(`${m.tabCount} tab buttons rendered (visible: ${m.tabsVisible}), expected ${VIEWS.length}`);
    const wide = m.wraps.filter((w) => w.tableWidth > w.clientWidth + 1);
    for (const w of wide) {
      if (!(w.scrollWidth > w.clientWidth) || !['auto', 'scroll'].includes(w.overflowX)) {
        problems.push(`.table-wrap of #${w.table} does not scroll (table ${w.tableWidth} px, wrapper ${w.clientWidth} px, scrollWidth ${w.scrollWidth}, overflow-x ${w.overflowX})`);
      }
    }
    if (width === 390 && tab === 'inventory') {
      const inv = m.wraps.find((w) => w.table === 'inventory-table');
      if (!inv || !(inv.scrollWidth > inv.clientWidth)) problems.push(`the inventory .table-wrap must scroll horizontally at ${width} px: ${json(inv || null)}`);
    }
    const clippers = [...new Set(m.offenders.filter((o) => o.clippedBy).map((o) => o.clippedBy))];
    const summary =
      `overflow ${m.overflow} px (scrollWidth ${m.scrollWidth}, innerWidth ${m.innerWidth}) · ${m.offenders.length} boxes cross the right edge: ` +
      `${m.offenders.length - m.unclipped.length} clipped by ${clippers.join(', ') || '—'}, ${m.unclipped.length} unclipped · tabs ${m.tabCount}/${VIEWS.length} reachable` +
      `${m.tablistScrolls ? ' (the tab bar scrolls inside its tablist)' : ''}` +
      `${wide.length ? ` · table-wrap scrolling: ${wide.map((w) => `#${w.table} ${w.tableWidth} px in ${w.clientWidth} px (scrollWidth ${w.scrollWidth})`).join(', ')}` : ' · no table wider than its wrapper'}`;
    return { problems, summary };
  }

  /** Record one measurement under report.responsive; returns the step details or throws when the view fails the rule. */
  recordView(label, tab, width, m) {
    const { problems, summary } = this.judgeView(m, tab, width);
    const byView = (this.overflowByView[label] = this.overflowByView[label] || {});
    byView[tab] = m.overflow;
    this.responsiveDetails.push({
      context: label,
      view: tab,
      width,
      status: problems.length ? 'fail' : 'pass',
      overflow: m.overflow,
      scrollWidth: m.scrollWidth,
      innerWidth: m.innerWidth,
      boxesPastRightEdge: m.offenders.length,
      unclipped: m.unclipped,
      clippedBy: [...new Set(m.offenders.filter((o) => o.clippedBy).map((o) => o.clippedBy))],
      tabs: { count: m.tabCount, visible: m.tabsVisible, tablistScrolls: m.tablistScrolls, tablistOverflowX: m.tablistOverflowX, selected: m.selected },
      tableWraps: m.wraps,
    });
    if (problems.length) throw new Error(`${problems.join('; ')} · ${summary}`);
    return summary;
  }

  // ------------------------------------------------------------------- stage 5: responsive (server mode, enforced CSP, fresh context per viewport)
  async responsiveStage(browser, base) {
    const stage = 'responsive';
    const origin = new URL(base).origin;
    const T = this.timeout;
    for (const width of RESPONSIVE_WIDTHS) {
      const label = `server:${width}x${VIEWPORT_HEIGHT}`;
      const monitor = newMonitor(label);
      this.responsiveContexts[label] = monitor;
      const context = await browser.newContext({ viewport: { width, height: VIEWPORT_HEIGHT } });
      await this.installViolationListener(context);
      const page = await context.newPage();
      this.violationSink = monitor.cspViolations;
      this.attachMonitor(page, origin, monitor, []);
      try {
        const loaded = await this.step(
          `responsive ${width}×${VIEWPORT_HEIGHT}: load the dashboard in server mode`,
          async () => {
            await page.goto(`${origin}/`, { waitUntil: 'load' });
            await page.locator('#mode', { hasText: /Server mode/ }).first().waitFor({ timeout: T });
            await page.waitForFunction((n) => document.querySelectorAll('#inventory-table tbody tr').length === n, SEEDED_ROWS, { timeout: T });
            const inner = await page.evaluate(() => window.innerWidth);
            if (inner !== width) throw new Error(`innerWidth ${inner} != ${width}`);
            return `viewport ${width}×${VIEWPORT_HEIGHT} · server mode · ${SEEDED_ROWS} inventory rows`;
          },
          { stage, page, shot: `responsive-${width}-load` },
        );
        for (const tab of VIEWS) {
          await this.step(
            `responsive ${width}×${VIEWPORT_HEIGHT} ${tab}: document overflow ≤ ${MAX_DOCUMENT_OVERFLOW} px, tabs reachable, table-wrap scrolls`,
            async () => {
              if (loaded.status !== 'pass') throw new Error('the dashboard did not load at this viewport');
              return this.recordView(label, tab, width, await this.measureView(page, tab));
            },
            { stage, page, shot: `responsive-${width}-${tab}`, kind: 'responsive' },
          );
        }
        await this.step(
          `responsive ${width}×${VIEWPORT_HEIGHT}: context — zero CSP violations, console errors, page errors, failed or foreign requests`,
          async () => {
            const problems = this.contextProblems(monitor, origin);
            if (problems.length) throw new Error(problems.join('; '));
            return `${monitor.requests.length} requests, all to ${origin} · ${monitor.expectedHttpErrorLogs.length} expected same-origin 4xx console lines · ${monitor.consoleWarnings.length} warnings`;
          },
          { stage },
        );
      } finally {
        await context.close();
        this.violationSink = this.server.cspViolations;
      }
    }
  }

  // ------------------------------------------------------------------- stage 4: axe (optional, separately labelled)
  async axeStage(browser, base) {
    const label = 'axe-instrumentation (bypassCSP; not an application-flow context)';
    const axe = loadAxeSource();
    if (!axe.module) {
      this.report.axe = 'unavailable';
      this.notRun(label, `not run: axe-core unavailable (tried ${axe.tried.join(', ')})`, 'axe');
      return;
    }
    await this.step(
      label,
      async () => {
        const context = await browser.newContext({ bypassCSP: true, viewport: { width: 1280, height: 900 } });
        try {
          const page = await context.newPage();
          await page.goto(`${base}/`, { waitUntil: 'load' });
          await page.locator('#mode', { hasText: /Server mode/ }).waitFor({ timeout: this.timeout });
          await page.addScriptTag({ content: axe.module });
          const results = await page.evaluate(async () => window.axe.run(document, { resultTypes: ['violations'] }));
          const serious = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
          this.report.axe = { source: axe.candidate, violations: results.violations.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.length })) };
          fs.writeFileSync(path.join(this.out, 'axe.json'), JSON.stringify(results, null, 2));
          if (serious.length) throw new Error(`${serious.length} serious/critical axe violation(s): ${serious.map((v) => v.id).join(', ')}`);
          return `${results.violations.length} violation(s), none serious/critical (details in axe.json)`;
        } finally {
          await context.close();
        }
      },
      { stage: 'axe' },
    );
  }

  // ------------------------------------------------------------------- stage 5: the static GitHub Pages demo under its meta CSP
  async pagesStage(browser, python) {
    const stage = 'pages-demo';
    const P = this.pages;
    const policy = this.policy;
    const info = { status: 'failed', pagesCsp: policy.pagesCsp, allowedOrigins: policy.pagesOrigins, build: null, staticServer: null, bootMs: null, cdnRequests: 0, cdnHosts: [] };
    this.pagesInfo = info;
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'stockline-site-'));
    const siteDir = path.join(tmp, 'site');
    const firstStep = this.report.steps.length;
    let served = null;
    let context = null;
    let page = null;
    const plan = [];
    const planned = (name, fn, opts = {}) => plan.push({ name, fn, opts });

    planned(
      `${stage}: build the static site with scripts/build_site.py`,
      async () => {
        const res = spawnSync(python, ['scripts/build_site.py', '--out', siteDir], { cwd: REPO, encoding: 'utf8', timeout: 120000 });
        if (res.status !== 0) throw new Error(`build_site.py exited ${res.status}: ${(res.stderr || res.stdout || '').trim().split('\n').slice(0, 3).join(' | ')}`);
        const version = JSON.parse(fs.readFileSync(path.join(siteDir, 'version.json'), 'utf8'));
        const built = fs.readFileSync(path.join(siteDir, 'index.html'), 'utf8');
        const tag = `<meta http-equiv="Content-Security-Policy" content="${policy.pagesCsp}">`;
        if (!built.includes(tag)) throw new Error('the built index.html does not carry the PAGES_CSP meta tag');
        if (/http-equiv\s*=\s*["']?content-security-policy/i.test(fs.readFileSync(path.join(REPO, 'public', 'index.html'), 'utf8'))) {
          throw new Error('the repository public/index.html carries a CSP meta tag (the Pages policy must be injected at build time only)');
        }
        const modules = fs.readdirSync(path.join(siteDir, 'app')).sort();
        info.build = { out: siteDir, version: version.version, modules, stdout: res.stdout.trim() };
        if (policy.version && version.version !== policy.version) throw new Error(`version.json says ${version.version}, app.__version__ is ${policy.version}`);
        return `${res.stdout.trim().split('\n')[0]} · meta CSP injected · ${modules.length} browser modules`;
      },
      { critical: true },
    );

    planned(
      `${stage}: serve the built site over plain HTTP (Content-Type only — no CSP or security headers, like GitHub Pages)`,
      async () => {
        served = await startStaticServer(siteDir);
        const probe = await fetch(`${served.base}/index.html`);
        const names = [...probe.headers.keys()].filter((h) => h !== 'content-type' && h !== 'content-length' && h !== 'date' && h !== 'connection' && h !== 'keep-alive' && h !== 'transfer-encoding');
        if (probe.status !== 200 || names.length) throw new Error(`static server answered ${probe.status} with unexpected headers ${json(names)}`);
        const miss = await fetch(`${served.base}/health`);
        if (miss.status !== 404 || !/^text\/html/.test(miss.headers.get('content-type') || '')) throw new Error(`/health must be an HTML 404 on static hosting, got ${miss.status} ${miss.headers.get('content-type')}`);
        info.staticServer = { base: served.base, stopped: false };
        context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1280, height: 900 } });
        await this.installViolationListener(context);
        page = await context.newPage();
        this.violationSink = P.cspViolations;
        this.attachMonitor(page, served.base, P, policy.pagesOrigins);
        return `${served.base} · index.html 200 text/html with Content-Type only · /health → 404 text/html · foreign origins allowed: ${policy.pagesOrigins.join(', ')}`;
      },
      { critical: true },
    );

    planned(
      `${stage}: index.html carries the Pages CSP meta tag equal to build_site.PAGES_CSP and no CSP header`,
      async () => {
        const response = await page.goto(`${served.base}/`, { waitUntil: 'load' });
        const headers = response.headers();
        if (headers['content-security-policy']) throw new Error('the static server sent a Content-Security-Policy header');
        if (!/^text\/html/.test(headers['content-type'] || '')) throw new Error(`document Content-Type ${json(headers['content-type'])}`);
        const metas = await page.locator('meta[http-equiv="Content-Security-Policy"]').evaluateAll((els) => els.map((e) => e.getAttribute('content')));
        if (metas.length !== 1) throw new Error(`${metas.length} CSP meta tags, expected exactly one`);
        if (metas[0] !== policy.pagesCsp) throw new Error(`meta CSP ${json(metas[0])} != PAGES_CSP ${json(policy.pagesCsp)}`);
        return `one meta CSP, byte-equal to PAGES_CSP (${policy.pagesCsp.length} chars) · no CSP header`;
      },
      { critical: true, page: true },
    );

    planned(
      `${stage}: the Pyodide runtime boots from the CDN and the dashboard switches to browser mode`,
      async () => {
        const t0 = Date.now();
        await page.locator('#mode', { hasText: /Browser mode — no server found, so the same Python service layer|Startup failed/ }).first().waitFor({ timeout: this.opts.pagesTimeout });
        info.bootMs = Date.now() - t0;
        const banner = (await page.locator('#mode').innerText()).trim();
        const cdn = P.allowedForeignRequests.map((r) => r.url);
        info.cdnRequests = cdn.length;
        info.cdnHosts = [...new Set(cdn.map((u) => new URL(u).host))];
        if (/Startup failed/.test(banner)) {
          const failures = P.requestFailed.map((f) => `${f.url} ${f.error}`).slice(0, 3).join(' | ');
          throw new Error(`${banner} · ${cdn.length} CDN request(s) · failed requests: ${failures || 'none recorded'}`);
        }
        const m = /^Browser mode — no server found, so the same Python service layer \(v(\d+\.\d+\.\d+)\) runs in this tab via Pyodide/.exec(banner);
        if (!m) throw new Error(`banner ${json(banner)}`);
        if (policy.version && m[1] !== policy.version) throw new Error(`banner version ${m[1]} != app.__version__ ${policy.version}`);
        return `${banner.slice(0, 90)}… · booted in ${(info.bootMs / 1000).toFixed(1)} s · ${cdn.length} CDN request(s) to ${info.cdnHosts.join(', ')}`;
      },
      { critical: true, page: true },
    );

    const S = {};
    planned(
      `${stage}: inventory renders the ${SEEDED_ROWS} seeded rows in the browser runtime`,
      async () => {
        const { text, waitCount } = this.uiHelpers(page);
        await waitCount('#inventory-table tbody tr', 'eq', SEEDED_ROWS, 0, this.timeout * 2);
        const meta = await text('#inv-meta');
        if (!meta.startsWith(`(${SEEDED_ROWS} rows`)) throw new Error(`inv-meta ${json(meta)}`);
        return `${SEEDED_ROWS} rows · ${meta}`;
      },
      { page: true },
    );

    const pagesLabel = `pages-demo:390x${VIEWPORT_HEIGHT}`;
    planned(
      `${stage}: responsive — switch the viewport to 390×${VIEWPORT_HEIGHT}`,
      async () => {
        await page.setViewportSize({ width: 390, height: VIEWPORT_HEIGHT });
        await page.waitForFunction(() => window.innerWidth === 390, null, { timeout: this.timeout });
        return 'innerWidth 390';
      },
      { page: true, shot: 'pages-demo-responsive-390-viewport' },
    );
    for (const tab of VIEWS) {
      planned(
        `${stage}: responsive 390×${VIEWPORT_HEIGHT} ${tab}: document overflow ≤ ${MAX_DOCUMENT_OVERFLOW} px, tabs reachable, table-wrap scrolls`,
        async () => this.recordView(pagesLabel, tab, 390, await this.measureView(page, tab)),
        { page: true, shot: `pages-demo-responsive-390-${tab}`, kind: 'responsive' },
      );
    }
    planned(
      `${stage}: responsive — restore the 1280×${VIEWPORT_HEIGHT} viewport`,
      async () => {
        await page.setViewportSize({ width: 1280, height: VIEWPORT_HEIGHT });
        await page.waitForFunction(() => window.innerWidth === 1280, null, { timeout: this.timeout });
        await page.click('#tab-inventory');
        await page.locator('#view-inventory:not([hidden])').waitFor({ timeout: this.timeout });
        return 'innerWidth 1280 · inventory view';
      },
      { critical: true, page: true, screenshot: false },
    );

    planned(
      `${stage}: order with an Idempotency-Key → 201, replay → 200 + Idempotent-Replayed: true, stock decremented once`,
      async () => {
        const { text, waitText, jsonOut, expectEq, T } = this.uiHelpers(page);
        await page.click('#tab-orders');
        await page.locator('#view-orders:not([hidden])').waitFor({ timeout: T });
        S.store = await page.locator('#order-store').inputValue();
        S.product = await page.locator('#order-lines [data-line]:nth-child(1) [data-role=product]').inputValue();
        const cell = `#inventory-table tbody tr[data-store="${S.store}"][data-product="${S.product}"] td:nth-child(4)`;
        const before = Number(await page.locator(cell).innerText());
        await page.fill('#order-lines [data-line]:nth-child(1) [data-role=qty]', '1');
        await page.click('[data-action="generate-key"][data-target="order-key"]');
        const key = await page.locator('#order-key').inputValue();
        await page.click('#order-form button[type=submit]');
        await waitText('#order-out', 'HTTP 201');
        await waitText('#order-out', 'Idempotent-Replayed: false');
        const first = await jsonOut('#order-out');
        expectEq('stored key', first.idempotency_key, key);
        await page.waitForFunction((arg) => document.querySelector(arg.sel) && document.querySelector(arg.sel).textContent.trim() === String(arg.v), { sel: cell, v: before - 1 }, { timeout: T });
        await page.click('#order-form button[type=submit]');
        await waitText('#order-out', 'Idempotent-Replayed: true');
        if (!/HTTP 200/.test(await text('#order-out .status'))) throw new Error('replay did not answer 200');
        const replay = await jsonOut('#order-out');
        expectEq('replayed id', replay.id, first.id);
        await sleep(200);
        expectEq('on_hand after replay', Number(await page.locator(cell).innerText()), before - 1);
        S.orderId = first.id;
        return `order #${first.id} key ${key}: 201 then 200 + Idempotent-Replayed: true · on_hand ${before}→${before - 1} once`;
      },
      { page: true },
    );

    planned(
      `${stage}: oversell → 409 insufficient_stock`,
      async () => {
        const { text, waitText } = this.uiHelpers(page);
        await page.fill('#order-key', '');
        await page.fill('#order-lines [data-line]:nth-child(1) [data-role=qty]', '1000');
        await page.click('#order-form button[type=submit]');
        await waitText('#order-out', 'HTTP 409');
        const line = await text('#order-out .error-line');
        if (!/insufficient_stock/.test(line)) throw new Error(`409 without insufficient_stock: ${line}`);
        await page.fill('#order-lines [data-line]:nth-child(1) [data-role=qty]', '1');
        return line;
      },
      { page: true },
    );

    planned(
      `${stage}: inventory CSV export is a real download (bridge headers shown in the console, file content verified)`,
      async () => {
        const { waitText, waitCount, saveDownload, T } = this.uiHelpers(page);
        await page.click('#tab-inventory');
        await page.locator('#view-inventory:not([hidden])').waitFor({ timeout: T });
        await waitCount('#inventory-table tbody tr', 'eq', SEEDED_ROWS);
        const waiting = page.waitForEvent('download', { timeout: T });
        await page.click('[data-action="export-inventory"]');
        const dl = await waiting;
        const saved = await saveDownload(dl, 'inventory', 'pages-demo-inventory.csv');
        if (saved.dataRows !== SEEDED_ROWS) saved.problems.push(`file has ${saved.dataRows} data rows, expected ${SEEDED_ROWS}`);
        await waitText('#toast', new RegExp(`^Downloaded ${escapeRe(saved.name)} · ${SEEDED_ROWS} rows$`));
        await page.click('#tab-console');
        await page.locator('#view-console:not([hidden])').waitFor({ timeout: T });
        const row = page.locator('#console-table tbody tr', { hasText: 'export.csv' }).first();
        await row.waitFor({ timeout: T });
        await row.locator('[data-action="console-toggle"]').click();
        const details = (await page.locator('#console-table tr.details').first().innerText()).replace(/\s+/g, ' ');
        for (const needle of ['text/csv', 'content-disposition', saved.name, 'x-row-count', `"${SEEDED_ROWS}"`]) {
          if (!details.includes(needle)) saved.problems.push(`console details lack ${json(needle)}`);
        }
        if (saved.problems.length) throw new Error(saved.problems.join('; '));
        return `${saved.name} · ${saved.dataRows} data rows · bridge headers: text/csv, content-disposition ${saved.name}, x-row-count ${SEEDED_ROWS}`;
      },
      { page: true },
    );

    planned(
      `${stage}: context — only the CDN origins named in PAGES_CSP were contacted; zero CSP violations, console errors, page errors and failed requests`,
      async () => {
        const problems = this.contextProblems(P, served.base);
        const cdn = P.allowedForeignRequests.map((r) => r.url);
        info.cdnRequests = cdn.length;
        info.cdnHosts = [...new Set(cdn.map((u) => new URL(u).host))];
        if (!cdn.length) problems.push('no CDN request was made, yet the runtime claims to have booted');
        if (problems.length) throw new Error(problems.join('; '));
        return `${P.requests.length} requests: ${P.requests.length - cdn.length} to ${served.base}, ${cdn.length} to ${info.cdnHosts.join(', ')} · ${P.expectedHttpErrorLogs.length} expected same-origin 4xx console line(s) · ${P.consoleWarnings.length} warnings · ${P.downloads.length} download(s)`;
      },
      { page: true, screenshot: false },
    );

    try {
      let blocked = null;
      for (const item of plan) {
        if (blocked) {
          this.failed(item.name, `not run: prerequisite failed (${blocked})`, stage, item.opts.kind || null);
          continue;
        }
        const entry = await this.step(item.name, item.fn, {
          stage,
          page: item.opts.page ? page : null,
          screenshot: item.opts.screenshot !== false,
          shot: item.opts.shot || null,
          kind: item.opts.kind || null,
        });
        if (entry.status !== 'pass' && item.opts.critical) blocked = item.name;
      }
    } finally {
      if (context) {
        try {
          await context.close();
        } catch {
          // already closed
        }
      }
      if (served) {
        await served.close();
        info.staticServer = { ...(info.staticServer || {}), stopped: true, portClosed: await portIsClosed(served.port), served: served.stats.served, notFound: served.stats.notFound };
      }
      fs.rmSync(tmp, { recursive: true, force: true });
      this.violationSink = this.server.cspViolations;
      const mine = this.report.steps.slice(firstStep);
      info.status = mine.length && mine.every((s) => s.status === 'pass') ? 'passed' : 'failed';
    }
  }

  // ------------------------------------------------------------------- outputs
  finish() {
    const r = this.report;
    const origin = r.base ? new URL(r.base).origin : '';
    this.classifyResourceErrors(this.server, origin);
    Object.assign(r, monitorFields(this.server));
    const responsiveContexts = {};
    for (const [label, monitor] of Object.entries(this.responsiveContexts)) {
      this.classifyResourceErrors(monitor, origin);
      responsiveContexts[label] = monitorFields(monitor);
    }
    const responsiveSteps = r.steps.filter((s) => s.kind === 'responsive');
    r.responsive = {
      rule: `document.documentElement.scrollWidth - window.innerWidth <= ${MAX_DOCUMENT_OVERFLOW} px on every view; tabs reachable; .table-wrap keeps horizontal scrolling`,
      widths: RESPONSIVE_WIDTHS,
      height: VIEWPORT_HEIGHT,
      views: VIEWS,
      overflowByView: this.overflowByView,
      measurements: this.responsiveDetails,
      contexts: responsiveContexts,
    };
    const pagesSteps = r.steps.filter((s) => s.stage === 'pages-demo');
    r.pagesDemo = this.pagesInfo
      ? { ...this.pagesInfo, ...monitorFields(this.pages), steps: pagesSteps.length, failed: pagesSteps.filter((s) => s.status === 'fail').length }
      : { status: this.opts.pagesDemo ? 'not reached' : 'not run (--no-pages-demo)' };
    const failed = r.steps.filter((s) => s.status === 'fail');
    r.finishedAt = new Date().toISOString();
    r.summary = {
      steps: r.steps.length,
      passed: r.steps.filter((s) => s.status === 'pass').length,
      failed: failed.length,
      notRun: r.steps.filter((s) => s.status === 'not_run').length,
      cspViolations: r.cspViolations.length,
      consoleErrors: r.consoleErrors.length,
      consoleWarnings: r.consoleWarnings.length,
      expectedHttpErrorLogs: r.expectedHttpErrorLogs.length,
      httpErrorResponses: r.httpErrorResponses.length,
      pageErrors: r.pageErrors.length,
      requestFailed: r.requestFailed.length,
      foreignRequests: r.foreignRequests.length,
      requests: this.server.requests.length,
      downloads: r.downloads.length,
      axe: typeof r.axe === 'string' ? r.axe : 'ran',
      pagesDemo: r.pagesDemo.status,
      responsive: {
        passed: responsiveSteps.filter((s) => s.status === 'pass').length,
        failed: responsiveSteps.filter((s) => s.status !== 'pass').length,
        overflowByView: this.overflowByView,
      },
      server: r.server ? r.server.stopped : undefined,
    };
    fs.writeFileSync(path.join(this.out, 'report.json'), JSON.stringify(r, null, 2));
    fs.writeFileSync(path.join(this.out, 'console.log'), `${this.consoleLines.join('\n')}\n`);
    const requestLine = (m) => (q) => `[${m.name}] ${q.method} ${q.url} -> ${q.status === null ? 'no response' : q.status} ${q.contentType} [${q.type}] <${q.step || '-'}>`;
    fs.writeFileSync(path.join(this.out, 'requests.log'), `${[...this.server.requests.map(requestLine(this.server)), ...this.pages.requests.map(requestLine(this.pages))].join('\n')}\n`);
    fs.writeFileSync(
      path.join(this.out, 'csp-violations.json'),
      JSON.stringify({ serverMode: this.server.cspViolations, pagesDemo: this.pages.cspViolations, positiveControl: r.cspPositiveControl }, null, 2),
    );
    fs.writeFileSync(path.join(this.out, 'headers.json'), JSON.stringify(r.headers, null, 2));
    this.log(`\nsummary ${json(r.summary)}`);
    if (failed.length) this.log(`failed steps:\n${failed.map((s) => `  - ${s.step}: ${s.details.split('\n')[0]}`).join('\n')}`);
    if (r.foreignRequests.length) this.log(`foreign requests (server mode): ${r.foreignRequests.map((f) => f.url).join(', ')}`);
    if (r.consoleErrors.length) this.log(`console errors (server mode): ${r.consoleErrors.map((c) => c.text).join(' | ')}`);
    if (r.pageErrors.length) this.log(`page errors (server mode): ${r.pageErrors.map((p) => p.message).join(' | ')}`);
    this.log(`responsive overflow by view (px): ${json(this.overflowByView)}`);
    this.log(`report: ${path.join(this.out, 'report.json')}`);
    return failed.length ? EXIT_FAILED : EXIT_OK;
  }
}

// --------------------------------------------------------------------------- main
async function main() {
  const opts = parseArgs(process.argv.slice(2));
  fs.mkdirSync(opts.out, { recursive: true });
  if (opts.browsersPath) process.env.PLAYWRIGHT_BROWSERS_PATH = opts.browsersPath;
  const policy = readPolicy();
  const check = new Check(opts, policy);
  check.log(`browser check → ${opts.out}`);
  check.log(
    `policies: ${policy.sources.server} (DEFAULT_CSP ${policy.defaultCsp.length} chars, DOCS_CSP ${policy.docsCsp.length} chars, ${Object.keys(policy.securityHeaders).length} security headers) · ${policy.sources.pages} (PAGES_CSP ${policy.pagesCsp.length} chars, CDN origins ${policy.pagesOrigins.join(', ')})`,
  );

  const pw = loadPlaywright();
  if (!pw.module) {
    check.log(`Playwright is unavailable — install it (npm i -D playwright && npx playwright install chromium) or point PLAYWRIGHT_MODULE at it.\n  tried: ${pw.tried.join('\n         ')}`);
    return EXIT_UNAVAILABLE;
  }
  check.report.environment.playwright = { version: pw.version, via: pw.candidate, resolved: path.relative(REPO, pw.resolved).startsWith('..') ? '(outside the repository)' : path.relative(REPO, pw.resolved) };
  check.log(`playwright ${pw.version || '?'} via ${pw.candidate}${opts.browsersPath ? ` · browsers ${opts.browsersPath}` : ''}`);

  const python = pickPython(opts);
  let server = null;
  let browser = null;
  let context = null;
  const stopApi = async () => {
    if (!server) return;
    const stopped = await stopServer(server);
    check.report.server = { ...(check.report.server || {}), ...stopped };
    check.log(`server stopped: ${json(stopped)}`);
    server = null;
  };
  const cleanup = async () => {
    if (context) {
      try {
        await context.close();
      } catch {
        // already closed
      }
      context = null;
    }
    if (browser) {
      try {
        await browser.close();
      } catch {
        // already closed
      }
      browser = null;
    }
    await stopApi();
  };
  const onSignal = (signal) => {
    check.log(`\nreceived ${signal}, stopping the server and the browser`);
    cleanup().finally(() => process.exit(130));
  };
  process.once('SIGINT', onSignal);
  process.once('SIGTERM', onSignal);
  process.on('exit', () => {
    if (server && server.proc && !server.exited) {
      try {
        server.proc.kill('SIGKILL');
      } catch {
        // gone
      }
    }
  });

  try {
    let base = opts.base;
    if (!base) {
      server = await startServer(python, opts.out);
      base = server.base;
      check.report.server = { python: relative(server.python), args: server.args, port: server.port, pid: server.proc.pid };
      check.log(`server: ${server.python} ${server.args.join(' ')} → ${base} (pid ${server.proc.pid})`);
    }
    check.report.base = base;

    await check.headerStage(base);

    try {
      browser = await pw.module.chromium.launch({ headless: !opts.headed });
    } catch (err) {
      check.log(`Chromium could not be launched (${String(err.message).split('\n')[0]}) — run \`npx playwright install chromium\` or pass --browsers-path.`);
      await cleanup();
      check.finish();
      return EXIT_UNAVAILABLE;
    }
    check.report.environment.browser = browser.version();
    const origin = new URL(base).origin;
    const opened = await check.openContext(browser, origin);
    context = opened.context;
    await check.workflowStage(opened.page, base);

    await check.step(
      'application context: zero CSP violations, console errors, page errors, failed requests and foreign requests',
      async () => {
        const problems = check.contextProblems(check.server, origin);
        if (problems.length) throw new Error(problems.join('; '));
        const m = check.server;
        const statuses = [...new Set(m.expectedHttpErrorLogs.map((e) => e.status))].sort().join('/');
        return `${m.requests.length} requests, all to ${origin} · ${m.httpErrorResponses.length} deliberate 4xx responses · ${m.expectedHttpErrorLogs.length} matching same-origin console lines (${statuses}) · ${m.consoleWarnings.length} warnings · ${m.downloads.length} downloads`;
      },
      { stage: 'context' },
    );

    await context.close(); // flushes session.har
    context = null;
    await check.responsiveStage(browser, base);
    await check.axeStage(browser, base);
    await stopApi(); // one server at a time: the API is down before the static site comes up

    if (opts.pagesDemo) await check.pagesStage(browser, python);
    else check.notRun('pages-demo (static site, meta CSP, Pyodide runtime)', 'not run: disabled with --no-pages-demo', 'pages-demo');
  } catch (err) {
    check.report.steps.push({ step: 'run', stage: 'run', status: 'fail', details: String(err && err.stack ? err.stack : err).split('\n').slice(0, 8).join('\n'), ms: 0 });
    check.log(`FAIL  [run] ${String(err && err.message ? err.message : err).split('\n')[0]}`);
  } finally {
    await cleanup();
  }
  return check.finish();
}

main().then(
  (code) => {
    process.exitCode = code;
  },
  (err) => {
    process.stderr.write(`${err && err.stack ? err.stack : err}\n`);
    process.exitCode = EXIT_FAILED;
  },
);
