"""Parity between the FastAPI application and the browser bridge, and the browser-module boundary.

(a) Route tables: every API operation of ``app.main.app`` (minus the server-only allowlist) has exactly one bridge
    route with the same method matching its sample path (``{x_id}`` → ``1``), and vice versa.
(b) ``import app.bridge`` in a fresh interpreter loads no web framework (``fastapi``, ``starlette``, ``uvicorn``) and
    none of the server-only modules — the Pyodide demo must work without them.
(c) The ``PY_FILES`` literal the demo loads (``public/app.js``) equals ``common.BROWSER_MODULES`` in the same order,
    and the ``BROWSER_MODULES`` literal itself keeps the regex-friendly shape the site builder parses.
(d) Every module under ``app/`` is classified exactly once as browser-shipped or server-only.
(e) Golden parity: one scripted sequence covering every endpoint the dashboard calls — run against two identically
    seeded databases, once through ``bridge.handle_json`` (what the browser demo executes) and once through a
    ``TestClient`` — answers the same status codes and JSON key sets.

Comparison rule for (e), spelled out because the two runtimes legitimately differ in details:

* status codes must be identical, and must equal the status the scenario expects;
* JSON bodies are compared as key structures — recursively for nested objects, the first element describing a list;
  values are never compared, so ``created_at``/``updated_at``/``uptime_s``/``runtime`` may differ;
* ``request_id`` is dropped before comparing: the server mints one per request, the bridge has none;
* ``detail`` of an error body (status >= 400) is compared as a leaf: it is the human-readable message (a string) or
  pydantic's error list. For query-parameter bounds the server returns pydantic's list while the bridge returns the
  message built by ``BridgeCall`` — both carry the same machine-readable ``code``, which *is* compared;
* a 204 has no body on either side (``b""`` on the wire, ``null`` in the bridge envelope);
* CSV exports must agree on the header row, ``Content-Type``, the ``Content-Disposition`` filename stem and
  ``X-Row-Count`` (both databases hold the same rows, so the counts are deterministic);
* ``Idempotent-Replayed`` must be identical when present;
* on a 405 both sides must send ``Allow``, and both values must equal the set of methods the FastAPI table declares
  for that path (the bridge lists the union of its routes for the path; the server derives the same set from its
  route table before a request can fall through to the static demo mount).

Known, documented divergence (pinned by ``test_literal_segment_under_a_parameterised_sibling_is_the_one_known_divergence``):
``GET /inventory/{store_id}/receipts`` is a 422 on the server — FastAPI's ``{product_id}`` parameter matches the literal
segment ``receipts`` and then fails integer validation — and a 405 on the bridge, whose patterns accept digits only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount

from app import bridge, common, db, main

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
APP_JS = ROOT / "public" / "app.js"
COMMON_PY = APP_DIR / "common.py"

SERVER_ONLY_PATHS = frozenset({"/metrics", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})  # plus the static mount
FORBIDDEN_PACKAGES = frozenset({"fastapi", "starlette", "uvicorn"})
SERVER_ONLY_APP_MODULES = frozenset({"app.deps", "app.observability", "app.security", "app.main"})
ENV_RESET = ("STOCKLINE_API_KEY", "STOCKLINE_CORS_ORIGINS", "STOCKLINE_CSP", "STOCKLINE_HSTS", "STOCKLINE_POOL_SIZE", "STOCKLINE_POOL_TIMEOUT")
IGNORED_KEYS = frozenset({"request_id"})

PY_FILES_RE = re.compile(r"const PY_FILES = \[([^\]]*)\];")
BROWSER_MODULES_RE = re.compile(r"^BROWSER_MODULES\s*:\s*tuple\[str,\s*\.\.\.\]\s*=\s*\(([^)]*)\)", re.MULTILINE)
QUOTED_RE = re.compile(r'"([^"]+)"')
PARAM_RE = re.compile(r"\{(\w+)\}")


# --------------------------------------------------------------------------- helpers
def iter_api_routes(routes: list) -> Iterator[APIRoute]:
    """Walk the application's route tree.

    Recent FastAPI versions keep each ``include_router`` call as a wrapper object (``original_router`` holds the
    included ``APIRouter``) instead of copying the operations to the top level; older versions place ``APIRoute``
    objects directly in ``app.routes``. Both layouts are handled, so the parity test does not depend on router internals.
    Mounts are skipped: the static demo mount is server-only, and the method guard wrapped around it exposes the
    declared route list through ``Mount.routes``, which would otherwise count every operation twice.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        if isinstance(route, Mount):
            continue
        for attribute in ("original_router", "router"):
            inner = getattr(route, attribute, None)
            if inner is not None and getattr(inner, "routes", None):
                yield from iter_api_routes(list(inner.routes))
                break
        else:
            nested = getattr(route, "routes", None)
            if nested:
                yield from iter_api_routes(list(nested))


def api_routes() -> list[tuple[str, str]]:
    """``(method, path_format)`` for every FastAPI operation the bridge must mirror (server-only paths removed)."""
    pairs: list[tuple[str, str]] = []
    for route in iter_api_routes(list(main.app.routes)):
        if route.path_format not in SERVER_ONLY_PATHS:
            pairs.extend((method, route.path_format) for method in sorted(route.methods or ()))
    return pairs


def sample(path_format: str) -> str:
    """Concrete path for a FastAPI path template: every ``{param}`` becomes ``1``."""
    return PARAM_RE.sub("1", path_format)


def declared_methods(path: str) -> set[str]:
    """Methods the FastAPI table declares for a concrete path (used to check the bridge's ``Allow`` header)."""
    return {method for method, template in api_routes() if sample(template) == path}


def shape(value: Any, *, error: bool) -> Any:
    """Key structure of a JSON value (see the module docstring for the exact rule)."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, inner in value.items():
            if key in IGNORED_KEYS:
                continue
            out[key] = "detail" if error and key == "detail" else shape(inner, error=error)
        return out
    if isinstance(value, list):
        return [shape(value[0], error=error)] if value else []
    return "value"


# --------------------------------------------------------------------------- (a) route tables
def test_every_api_route_has_exactly_one_bridge_route_and_vice_versa():
    pairs = api_routes()
    assert len(pairs) == len(bridge.ROUTES) == 34, (len(pairs), len(bridge.ROUTES))
    for method, template in pairs:
        concrete = sample(template)
        matches = [pattern for m, pattern, _handler in bridge.ROUTES if m == method and pattern.fullmatch(concrete)]
        assert len(matches) == 1, f"{method} {template}: bridge routes matching {concrete!r}: {[p.pattern for p in matches]}"
        assert set(matches[0].groupindex) == set(PARAM_RE.findall(template)), f"{method} {template}: path parameter names differ"
    for method, pattern, _handler in bridge.ROUTES:
        matches = [template for m, template in pairs if m == method and pattern.fullmatch(sample(template))]
        assert len(matches) == 1, f"bridge route {method} {pattern.pattern} matches FastAPI routes {matches}"
    for path in SERVER_ONLY_PATHS:
        assert not any(pattern.fullmatch(path) for _m, pattern, _h in bridge.ROUTES), f"{path} is server-only"


def test_bridge_dispatch_order_mirrors_the_routers():
    """Literal paths win over parameterised ones on both sides: the first matching bridge route is the right one."""
    for method, template in api_routes():
        concrete = sample(template)
        first = next((pattern for m, pattern, _h in bridge.ROUTES if m == method and pattern.match(concrete)), None)
        assert first is not None and first.fullmatch(concrete), (method, template)
    literal_before_param = [
        ("GET", r"^/orders/export\.csv$", r"^/orders/(?P<order_id>\d+)$"),
        ("GET", r"^/inventory/export\.csv$", r"^/inventory/(?P<store_id>\d+)/(?P<product_id>\d+)$"),
        ("POST", r"^/inventory/(?P<store_id>\d+)/receipts$", r"^/inventory/(?P<store_id>\d+)/(?P<product_id>\d+)/adjust$"),
    ]
    patterns = [(m, p.pattern) for m, p, _h in bridge.ROUTES]
    for method, literal, parameterised in literal_before_param:
        assert (method, literal) in patterns and (method, parameterised) in patterns, (method, literal, parameterised)
        assert patterns.index((method, literal)) < patterns.index((method, parameterised)), literal


# --------------------------------------------------------------------------- (b) framework-free import
def test_bridge_imports_no_web_framework_in_a_fresh_interpreter(tmp_path):
    code = textwrap.dedent(
        """
        import json, sys
        from app import bridge
        health = json.loads(bridge.handle_json("GET", "/health"))
        watched = ("app", "fastapi", "starlette", "uvicorn")
        modules = sorted(name for name in sys.modules if name.split(".")[0] in watched)
        print(json.dumps({"modules": modules, "routes": len(bridge.ROUTES), "health": health}))
        """
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("STOCKLINE_")}
    env["STOCKLINE_DB"] = str(tmp_path / "subprocess.db")
    stray = ROOT / "stockline.db"
    stray_existed = stray.exists()
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    loaded = set(report["modules"])
    assert "app.bridge" in loaded and "app.seed" in loaded, sorted(loaded)
    frameworks = {name for name in loaded if name.split(".")[0] in FORBIDDEN_PACKAGES}
    assert not frameworks, f"importing app.bridge loaded a web framework: {sorted(frameworks)}"
    assert not loaded & SERVER_ONLY_APP_MODULES, f"importing app.bridge loaded server-only modules: {sorted(loaded & SERVER_ONLY_APP_MODULES)}"
    assert report["routes"] == len(bridge.ROUTES)
    assert report["health"]["status"] == 200
    health = report["health"]["body"]
    assert health.get("runtime") == "pyodide" and health.get("schema_version") == 2, health
    assert (tmp_path / "subprocess.db").exists(), "the subprocess must use the database STOCKLINE_DB points at"
    assert stray.exists() == stray_existed, "the subprocess must never create stockline.db in the repository root"


# --------------------------------------------------------------------------- (c) + (d) module lists
def test_py_files_literal_equals_browser_modules():
    found = PY_FILES_RE.findall(APP_JS.read_text(encoding="utf-8"))
    assert len(found) == 1, "public/app.js must contain exactly one `const PY_FILES = [...];` statement"
    assert re.fullmatch(r'\s*(?:"[^"]+"\s*,\s*)*"[^"]+"\s*,?\s*', found[0]), f"PY_FILES must hold double-quoted strings only: {found[0]!r}"
    assert QUOTED_RE.findall(found[0]) == list(common.BROWSER_MODULES)
    literal = BROWSER_MODULES_RE.search(COMMON_PY.read_text(encoding="utf-8"))
    assert literal is not None, "BROWSER_MODULES must keep its regex-friendly literal form (parsed by scripts/build_site.py)"
    assert QUOTED_RE.findall(literal.group(1)) == list(common.BROWSER_MODULES)


def test_every_app_module_is_classified_exactly_once():
    files = {path.name for path in APP_DIR.glob("*.py")}
    browser, server = set(common.BROWSER_MODULES), set(common.SERVER_ONLY_MODULES)
    assert files == browser | server, {"unclassified": sorted(files - browser - server), "missing": sorted((browser | server) - files)}
    assert not browser & server, sorted(browser & server)
    assert len(common.BROWSER_MODULES) == len(browser), "duplicate entries in BROWSER_MODULES"
    assert "bridge.py" in browser and {"main.py", "deps.py", "observability.py", "security.py"} <= server


# --------------------------------------------------------------------------- (e) golden parity
@dataclass(frozen=True)
class Step:
    """One request of the golden sequence, sent identically to both runtimes."""

    method: str
    url: str
    expect: int
    body: Any = None  # dict → JSON-encoded; str → sent verbatim (malformed-JSON probe)
    headers: tuple[tuple[str, str], ...] = ()
    capture: str | None = None  # remember body["id"] under this name; later URLs may use "{name}"


@dataclass
class Observed:
    status: int
    body: Any
    headers: dict[str, str]


@dataclass
class Side:
    """One runtime (server or bridge) with the ids it captured along the way."""

    name: str
    send: Any
    captured: dict[str, Any] = field(default_factory=dict)

    def run(self, step: Step) -> Observed:
        url = step.url.format_map(self.captured)
        raw = step.body if step.body is None or isinstance(step.body, str) else json.dumps(step.body)
        headers = dict(step.headers)
        if raw is not None:
            headers.setdefault("Content-Type", "application/json")
        observed = self.send(step.method, url, raw, headers)
        if step.capture and isinstance(observed.body, dict):
            self.captured[step.capture] = observed.body["id"]
        return observed


def _via_server(client: TestClient):
    def send(method: str, url: str, raw: str | None, headers: dict[str, str]) -> Observed:
        r = client.request(method, url, content=raw, headers=headers)
        content_type = r.headers.get("content-type", "")
        if r.status_code == 204 or not r.content:
            body: Any = None
        elif content_type.startswith("application/json"):
            body = r.json()
        else:
            body = r.text
        return Observed(r.status_code, body, {k.lower(): v for k, v in r.headers.items()})

    return send


def _via_bridge(br):
    def send(method: str, url: str, raw: str | None, headers: dict[str, str]) -> Observed:
        out = json.loads(br.handle_json(method, url, raw, json.dumps(headers)))  # exactly what the browser receives
        return Observed(out["status"], out["body"], {k.lower(): v for k, v in out["headers"].items()})

    return send


@pytest.fixture()
def sides(tmp_path, monkeypatch):
    """Two identically seeded databases: the bridge owns ``bridge.db``, the server's pool owns ``server.db``."""
    for var in ENV_RESET:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("STOCKLINE_SEED", "1")
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "bridge.db"))
    bridge._conn = None
    bridge.get_conn()  # opens + migrates + seeds now, before DB_PATH moves on
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "server.db"))
    monkeypatch.setenv("STOCKLINE_DB", str(tmp_path / "server.db"))
    main.reset_state(seed=True)  # the pool opens lazily on the new path and seeds on first use
    with TestClient(main.app) as client:
        yield Side("server", _via_server(client)), Side("bridge", _via_bridge(bridge))
    main.reset_state(seed=False)
    conn, bridge._conn = bridge._conn, None
    if conn is not None:
        conn.close()


ORDER = {"store_id": 1, "lines": [{"product_id": 2, "quantity": 1}]}
TRANSFER = {"from_store_id": 1, "to_store_id": 2, "product_id": 3, "quantity": 1}
RECEIPT = {"reference": "PO-PARITY", "lines": [{"product_id": 1, "quantity": 2}, {"product_id": 2, "quantity": 3}]}
NEW_STORE = {"code": "PAR-01", "name": "Parity store", "region": "North"}
NEW_PRODUCT = {"sku": "PAR-1", "name": "Parity widget", "category": "Misc", "price_cents": 1000, "reorder_point": 2}

GOLDEN_SEQUENCE: tuple[Step, ...] = (
    # system
    Step("GET", "/health", 200),
    Step("GET", "/integrity", 200),
    # catalog
    Step("GET", "/stores", 200),
    Step("GET", "/stores/1", 200),
    Step("GET", "/stores/9999", 404),
    Step("PATCH", "/stores/1", 200, {"name": "Renamed"}),
    Step("PATCH", "/stores/1", 422, {}),
    Step("POST", "/stores", 409, {"code": "BLR-01", "name": "Dup", "region": "South"}),
    Step("POST", "/stores", 201, NEW_STORE),
    Step("GET", "/products?limit=100&q=&include_inactive=true", 200),
    Step("GET", "/products?q=rice&limit=20", 200),
    Step("GET", "/products?limit=0", 422),
    Step("GET", "/products/1", 200),
    Step("GET", "/products/9999", 404),
    Step("POST", "/products", 201, NEW_PRODUCT, capture="product_id"),
    Step("POST", "/products", 409, NEW_PRODUCT),
    Step("PATCH", "/products/{product_id}", 200, {"price_cents": 1250, "active": True}),
    Step("PATCH", "/products/1", 422, {"sku": "X-1"}),
    Step("GET", "/products/1/inventory", 200),
    # inventory rows, adjustments, receipts
    Step("GET", "/inventory?limit=200&store_id=1&low_stock=true", 200),
    Step("GET", "/inventory?q=rice&limit=50", 200),
    Step("GET", "/inventory?limit=201", 422),
    Step("GET", "/inventory/1/1", 200),
    Step("GET", "/inventory/1/{product_id}", 404),
    Step("GET", "/inventory/1/1/movements?limit=50", 200),
    Step("POST", "/inventory/1/1/adjust", 200, {"delta": 5, "reason": "receipt", "reference": "parity"}),
    Step("POST", "/inventory/1/1/adjust", 409, {"delta": 1, "reason": "receipt", "expected_version": 0}),
    Step("POST", "/inventory/1/1/adjust", 409, {"delta": -999, "reason": "adjustment"}),
    Step("POST", "/inventory/1/1/adjust", 422, {"delta": 0, "reason": "receipt"}),
    Step("POST", "/inventory/1/receipts", 201, RECEIPT),
    Step("POST", "/inventory/1/receipts", 404, {"reference": "PO-MISSING", "lines": [{"product_id": 9999, "quantity": 1}]}),
    Step("POST", "/inventory/1/receipts", 422, {"reference": "", "lines": []}),
    # movement feed and exports
    Step("GET", "/movements?limit=10", 200),
    Step("GET", "/movements?store_id=1&reason=receipt&limit=100", 200),
    Step("GET", "/movements?reason=bogus", 422),
    Step("GET", "/movements?limit=501", 422),
    Step("GET", "/movements/export.csv?store_id=1&product_id=1", 200),
    Step("GET", "/inventory/export.csv?store_id=1&low_stock=true", 200),
    # orders and idempotency
    Step("POST", "/orders", 201, ORDER, (("Idempotency-Key", "parity-order"),), capture="order_id"),
    Step("POST", "/orders", 200, ORDER, (("Idempotency-Key", "parity-order"),)),
    Step("POST", "/orders", 422, {**ORDER, "lines": [{"product_id": 2, "quantity": 2}]}, (("Idempotency-Key", "parity-order"),)),
    Step("POST", "/orders", 409, {"store_id": 1, "lines": [{"product_id": 1, "quantity": 999}]}),
    Step("POST", "/orders", 422, {"store_id": 1, "lines": []}),
    Step("POST", "/orders", 422, "{not json"),
    Step("POST", "/orders", 422, ORDER, (("Idempotency-Key", "k" * 65),)),
    Step("GET", "/orders?limit=10&offset=0&status=placed", 200),
    Step("GET", "/orders?limit=0", 422),
    Step("GET", "/orders/1", 200),
    Step("GET", "/orders/9999", 404),
    Step("POST", "/orders/{order_id}/cancel", 200),
    Step("POST", "/orders/{order_id}/cancel", 200),
    Step("POST", "/orders/{order_id}/fulfil", 409),
    Step("POST", "/orders/9999/fulfil", 404),
    Step("GET", "/orders/export.csv?status=cancelled", 200),
    # transfers
    Step("POST", "/transfers", 201, TRANSFER, (("Idempotency-Key", "parity-transfer"),), capture="transfer_id"),
    Step("POST", "/transfers", 200, TRANSFER, (("Idempotency-Key", "parity-transfer"),)),
    Step("POST", "/transfers", 409, {**TRANSFER, "quantity": 999}),
    Step("POST", "/transfers", 422, {**TRANSFER, "to_store_id": 1}),
    Step("GET", "/transfers?limit=20&store_id=2", 200),
    Step("GET", "/transfers?offset=-1", 422),
    Step("GET", "/transfers/{transfer_id}", 200),
    Step("GET", "/transfers/9999", 404),
    # reports
    Step("GET", "/reports/summary", 200),
    Step("GET", "/reports/reorder?days=14&store_id=1", 200),
    Step("GET", "/reports/reorder?days=0", 422),
    Step("GET", "/reports/sales?days=30&group_by=day", 200),
    Step("GET", "/reports/sales?days=30&group_by=product", 200),
    Step("GET", "/reports/sales?group_by=bogus", 422),
    Step("GET", "/reports/reorder.csv?days=30", 200),
    # audit & repair
    Step("GET", "/integrity", 200),
    Step("POST", "/integrity/rebuild", 200),
    # soft delete
    Step("DELETE", "/products/12", 204),
    Step("DELETE", "/products/12", 204),
    Step("GET", "/products/12", 200),
    Step("POST", "/orders", 409, {"store_id": 1, "lines": [{"product_id": 12, "quantity": 1}]}),
    Step("DELETE", "/products/9999", 404),
    # unknown paths and wrong methods
    Step("GET", "/nope", 404),
    Step("GET", "/orders/export.csv/extra", 404),
    Step("PUT", "/orders", 405),
    Step("GET", "/integrity/rebuild", 405),
    Step("DELETE", "/health", 405),
    Step("PUT", "/products/1", 405),
    Step("DELETE", "/stores/1", 405),
    Step("PATCH", "/orders/1", 405),
    Step("POST", "/reports/summary", 405),
)


def _compare(step: Step, server: Observed, browser: Observed) -> None:
    where = f"{step.method} {step.url}"
    assert server.status == browser.status == step.expect, f"{where}: server {server.status}, bridge {browser.status}, expected {step.expect}"
    if isinstance(browser.body, str):  # CSV export
        assert isinstance(server.body, str), f"{where}: server body is not text"
        assert server.body.split("\n")[0] == browser.body.split("\n")[0], f"{where}: CSV header rows differ"
        assert server.headers.get("content-type") == browser.headers.get("content-type") == "text/csv; charset=utf-8", where
        stem = re.match(r'attachment; filename="([a-z]+)-\d{8}\.csv"', browser.headers.get("content-disposition", ""))
        assert stem is not None, f"{where}: bridge Content-Disposition {browser.headers.get('content-disposition')!r}"
        assert server.headers.get("content-disposition", "").startswith(f'attachment; filename="{stem.group(1)}-'), where
        assert server.headers.get("x-row-count", "").isdigit(), f"{where}: server X-Row-Count {server.headers.get('x-row-count')!r}"
        assert server.headers.get("x-row-count") == browser.headers.get("x-row-count"), f"{where}: X-Row-Count differs"
        return
    if step.expect == 204:
        assert server.body is None and browser.body is None, where
        return
    error = step.expect >= 400
    assert shape(server.body, error=error) == shape(browser.body, error=error), f"{where}: body shapes differ"
    if error:
        assert server.body["code"] == browser.body["code"], f"{where}: error codes differ"
    if "idempotent-replayed" in server.headers or "idempotent-replayed" in browser.headers:
        assert server.headers.get("idempotent-replayed") == browser.headers.get("idempotent-replayed"), where
    if step.expect == 405:
        path = step.url.split("?")[0]
        declared = declared_methods(path)
        bridge_allow = {m.strip() for m in browser.headers.get("allow", "").split(",") if m.strip()}
        server_allow = {m.strip() for m in server.headers.get("allow", "").split(",") if m.strip()}
        assert bridge_allow == declared, f"{where}: bridge Allow {bridge_allow} != declared {declared}"
        assert server_allow == declared, f"{where}: server Allow {server_allow} != declared {declared}"


def test_golden_sequence_matches_status_codes_and_key_sets(sides):
    server, browser = sides
    for step in GOLDEN_SEQUENCE:
        _compare(step, server.run(step), browser.run(step))
    assert server.captured.keys() == browser.captured.keys() == {"product_id", "order_id", "transfer_id"}
    assert server.captured == browser.captured, "identically seeded databases must hand out the same ids"


def test_wrong_method_on_an_api_path_never_reaches_the_static_mount(sides):
    """Every API path answers a wrong method with 405 + ``Allow`` on both sides — even a ``GET`` of a POST-only path,
    which the server must not hand to the static demo mount at ``/`` (that would turn it into a 404)."""
    server, browser = sides
    for method, path in (("GET", "/integrity/rebuild"), ("PATCH", "/orders/1/cancel"), ("DELETE", "/transfers"), ("PUT", "/inventory/1/1/adjust")):
        step = Step(method, path, 405)
        on_server, on_bridge = server.run(step), browser.run(step)
        assert (on_server.status, on_server.body["code"]) == (405, "method_not_allowed"), (method, path, on_server)
        assert (on_bridge.status, on_bridge.body["code"]) == (405, "method_not_allowed"), (method, path, on_bridge)
        expected = ", ".join(sorted(declared_methods(path)))
        assert on_server.headers.get("allow") == on_bridge.headers.get("allow") == expected, (method, path, on_server.headers.get("allow"))
    # HEAD is a wrong method too (the table declares GET only); a HEAD response has no body on the wire, so compare status and Allow.
    head_on_server, head_on_bridge = server.run(Step("HEAD", "/health", 405)), browser.run(Step("HEAD", "/health", 405))
    assert (head_on_server.status, head_on_server.headers.get("allow")) == (405, "GET"), (head_on_server.status, head_on_server.headers)
    assert (head_on_bridge.status, head_on_bridge.headers.get("allow"), head_on_bridge.body["code"]) == (405, "GET", "method_not_allowed")


def test_literal_segment_under_a_parameterised_sibling_is_the_one_known_divergence(sides):
    """``GET /inventory/1/receipts``: 422 on the server, 405 on the bridge — inherent to the two routing styles.

    FastAPI matches ``GET /inventory/{store_id}/{product_id}`` first (a path parameter accepts any segment) and then
    rejects ``product_id="receipts"`` as a non-integer (422 ``validation_error``); the bridge patterns accept digits
    only (``\\d+``), so the path is known to the ``POST …/receipts`` route alone → 405 ``method_not_allowed``.
    Pinned so that a change on either side (for example typed path convertors on the server) shows up here.
    """
    server, browser = sides
    step = Step("GET", "/inventory/1/receipts", 405)
    on_server, on_bridge = server.run(step), browser.run(step)
    assert (on_server.status, on_server.body["code"]) == (422, "validation_error"), on_server
    assert on_server.body["detail"][0]["loc"] == ["path", "product_id"], on_server.body
    assert (on_bridge.status, on_bridge.body["code"], on_bridge.headers.get("allow")) == (405, "method_not_allowed", "POST")


def test_golden_sequence_covers_every_bridge_route():
    """Each bridge route (and so each mirrored API route) is exercised at least once by the golden sequence."""
    hit: set[str] = set()
    for step in GOLDEN_SEQUENCE:
        path = step.url.split("?")[0].format_map({"product_id": 13, "order_id": 41, "transfer_id": 3})
        for method, pattern, _handler in bridge.ROUTES:
            if method == step.method and pattern.fullmatch(path):
                hit.add(f"{method} {pattern.pattern}")
    missing = {f"{m} {p.pattern}" for m, p, _h in bridge.ROUTES} - hit
    assert not missing, sorted(missing)
