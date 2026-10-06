"""Security headers / CSP, opt-in API key and CORS (``app.security``) on a private app.

The app is assembled exactly like ``app.main`` will be (install_auth → observability.install →
install_headers) with the system router and the real ``public/`` directory mounted, so the header
values asserted here are the ones the browser gate sees on ``/``, ``/app.js``, ``/styles.css``,
``/health``, CSV responses and ``/docs``.
"""
from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from app import db, deps, observability, security
from app.common import CONFLICT, ServiceError
from app.routers import system

PUBLIC = Path(__file__).resolve().parent.parent / "public"
KEY = "s3cret-key"
HEADER_NAMES = {name.lower() for name in security.SECURITY_HEADERS}


def make_app(settings: deps.Settings | None = None, *, static: bool = True) -> FastAPI:
    """Private app wired in the production order, plus a few probe routes."""
    settings = settings or deps.Settings()
    app = FastAPI(docs_url="/docs", redoc_url="/redoc")
    security.install_auth(app, settings)
    observability.install(app, settings)
    security.install_headers(app, settings)
    app.include_router(system.router)

    @app.post("/echo")
    def echo(body: dict):
        return body

    @app.put("/echo")
    def put_echo(body: dict):
        return body

    @app.patch("/echo")
    def patch_echo(body: dict):
        return body

    @app.delete("/echo")
    def delete_echo():
        return Response(status_code=204)

    @app.get("/csv")
    def csv():
        return Response("a,b\n1,2\n", media_type="text/csv; charset=utf-8")

    @app.get("/custom-csp")
    def custom_csp():
        return JSONResponse({"ok": True}, headers={"Content-Security-Policy": "default-src 'none'"})

    @app.get("/boom")
    def boom():
        raise ServiceError(409, "boom", CONFLICT)

    @app.get("/db")
    def with_db(conn: sqlite3.Connection = Depends(deps.get_conn)):
        return {"one": conn.execute("SELECT 1").fetchone()[0]}

    if static:
        app.mount("/", StaticFiles(directory=str(PUBLIC), html=True), name="ui")
    return app


@pytest.fixture()
def dbpath(tmp_path, monkeypatch):
    path = str(tmp_path / "sec.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    for var in ("STOCKLINE_SEED", "STOCKLINE_POOL_SIZE", "STOCKLINE_POOL_TIMEOUT", "STOCKLINE_API_KEY", "STOCKLINE_CORS_ORIGINS"):
        monkeypatch.delenv(var, raising=False)
    deps.reset_state(seed=False)
    observability.METRICS.reset()
    yield path
    deps.reset_state(seed=False)


@pytest.fixture()
def client(dbpath):
    with TestClient(make_app(), raise_server_exceptions=False) as c:
        yield c


def _assert_baseline_headers(r, path: str, csp: str = security.DEFAULT_CSP) -> None:
    assert r.headers.get("content-security-policy") == csp, path
    for name, value in security.SECURITY_HEADERS.items():
        assert r.headers.get(name) == value, (path, name)


# --------------------------------------------------------------------------- constants
def test_constants_are_the_published_contract():
    assert security.DEFAULT_CSP == (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    )
    assert security.DOCS_CSP == (
        "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.jsdelivr.net; "
        "font-src 'self' https://cdn.jsdelivr.net data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'none'"
    )
    assert security.DOCS_PATHS == ("/docs", "/docs/oauth2-redirect", "/redoc")
    assert security.SECURITY_HEADERS == {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
    assert security.HSTS_VALUE == "max-age=31536000; includeSubDomains"


def test_middlewares_are_pure_asgi():
    for cls in (security.SecurityHeadersMiddleware, security.ApiKeyMiddleware, observability.RequestContextMiddleware):
        assert not issubclass(cls, BaseHTTPMiddleware), cls
        assert inspect.iscoroutinefunction(cls.__call__), cls


def test_install_order_matches_the_main_py_contract():
    app = FastAPI()
    settings = deps.Settings(api_key=KEY, cors_origins=("https://ui.example",))
    security.install_auth(app, settings)
    observability.install(app, settings)
    security.install_headers(app, settings)
    # Starlette keeps user middleware outermost-first.
    assert [m.cls for m in app.user_middleware] == [
        security.SecurityHeadersMiddleware,
        CORSMiddleware,
        observability.RequestContextMiddleware,
        security.ApiKeyMiddleware,
    ]
    plain = FastAPI()
    security.install_auth(plain, deps.Settings())
    observability.install(plain, deps.Settings())
    security.install_headers(plain, deps.Settings())
    assert [m.cls for m in plain.user_middleware] == [security.SecurityHeadersMiddleware, observability.RequestContextMiddleware]


# --------------------------------------------------------------------------- headers on every response
def test_exact_headers_on_health_root_and_static_assets(client):
    r = client.get("/health")
    assert r.status_code == 200
    _assert_baseline_headers(r, "/health")
    assert r.headers.get("content-type", "").startswith("application/json")
    assert r.headers.get("cache-control") == "no-store"

    r = client.get("/")
    assert r.status_code == 200
    _assert_baseline_headers(r, "/")
    assert r.headers.get("content-type", "").startswith("text/html")
    assert r.headers.get("cache-control") != "no-store"

    r = client.get("/styles.css")
    assert r.status_code == 200
    _assert_baseline_headers(r, "/styles.css")
    assert r.headers.get("content-type", "").startswith("text/css")
    assert r.headers.get("cache-control") != "no-store"

    for asset in ("/app.js", "/lib.js"):
        r = client.get(asset)
        assert r.status_code == 200, asset
        _assert_baseline_headers(r, asset)
        ctype = r.headers.get("content-type", "")
        assert ctype.startswith(("text/javascript", "application/javascript")), (asset, ctype)
        assert r.headers.get("cache-control") != "no-store"


def test_csv_and_json_responses_are_never_cached(client):
    r = client.get("/csv")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("text/csv")
    assert r.headers.get("cache-control") == "no-store"
    _assert_baseline_headers(r, "/csv")
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"
    _assert_baseline_headers(r, "/openapi.json")


def test_docs_paths_get_the_docs_policy(client):
    for path in security.DOCS_PATHS:
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers.get("content-security-policy") == security.DOCS_CSP, path
        for name, value in security.SECURITY_HEADERS.items():
            assert r.headers.get(name) == value, (path, name)
        assert r.headers.get("cache-control") != "no-store"


def test_csp_override_applies_everywhere_except_docs(dbpath):
    override = "default-src 'none'; frame-ancestors 'none'"
    with TestClient(make_app(deps.Settings(csp=override)), raise_server_exceptions=False) as c:
        for path in ("/health", "/", "/styles.css", "/csv", "/nope"):
            assert c.get(path).headers.get("content-security-policy") == override, path
        assert c.get("/docs").headers.get("content-security-policy") == security.DOCS_CSP
        assert c.get("/redoc").headers.get("content-security-policy") == security.DOCS_CSP


def test_app_set_csp_is_not_overwritten(client):
    r = client.get("/custom-csp")
    assert r.status_code == 200
    assert r.headers.get("content-security-policy") == "default-src 'none'"
    for name, value in security.SECURITY_HEADERS.items():
        assert r.headers.get(name) == value, name
    assert r.headers.get("cache-control") == "no-store"


def test_hsts_only_when_enabled(dbpath):
    with TestClient(make_app(deps.Settings(hsts=True)), raise_server_exceptions=False) as c:
        for path in ("/health", "/", "/styles.css", "/docs"):
            assert c.get(path).headers.get("strict-transport-security") == security.HSTS_VALUE, path
    with TestClient(make_app(deps.Settings()), raise_server_exceptions=False) as c:
        for path in ("/health", "/"):
            assert "strict-transport-security" not in c.get(path).headers, path


def test_headers_present_on_error_responses(dbpath):
    with TestClient(make_app(deps.Settings(api_key=KEY)), raise_server_exceptions=False) as c:
        r404 = c.get("/nope")
        assert r404.status_code == 404
        _assert_baseline_headers(r404, "/nope")
        assert r404.headers.get("cache-control") == "no-store"
        assert r404.headers.get("x-request-id")

        r422 = c.post("/echo", content=b"not json", headers={"Content-Type": "application/json", "X-API-Key": KEY})
        assert r422.status_code == 422
        _assert_baseline_headers(r422, "/echo 422")
        assert r422.headers.get("cache-control") == "no-store"
        assert r422.headers.get("x-request-id")

        r401 = c.post("/echo", json={"a": 1})
        assert r401.status_code == 401
        _assert_baseline_headers(r401, "/echo 401")
        assert r401.headers.get("cache-control") == "no-store"
        assert r401.headers.get("x-request-id")

        r409 = c.get("/boom")
        assert r409.status_code == 409
        _assert_baseline_headers(r409, "/boom")


def test_headers_on_204_without_body(client):
    r = client.delete("/echo")
    assert r.status_code == 204
    _assert_baseline_headers(r, "DELETE /echo")
    assert r.content == b""


# --------------------------------------------------------------------------- API key
def test_api_key_unset_leaves_writes_open(client):
    assert client.post("/echo", json={"a": 1}).status_code == 200
    assert client.put("/echo", json={"a": 1}).status_code == 200
    assert client.delete("/echo").status_code == 204


def test_api_key_protects_mutating_methods(dbpath):
    with TestClient(make_app(deps.Settings(api_key=KEY)), raise_server_exceptions=False) as c:
        r = c.post("/echo", json={"a": 1}, headers={"X-Request-ID": "auth-1"})
        assert r.status_code == 401
        assert r.json() == {"detail": "missing or invalid API key", "code": "unauthorized", "request_id": "auth-1"}
        assert r.headers.get("www-authenticate") == "Bearer"
        assert r.headers.get("x-request-id") == "auth-1"
        assert r.headers.get("content-type", "").startswith("application/json")

        assert c.post("/echo", json={"a": 1}, headers={"X-API-Key": KEY}).status_code == 200
        assert c.post("/echo", json={"a": 1}, headers={"Authorization": f"Bearer {KEY}"}).status_code == 200
        assert c.post("/echo", json={"a": 1}, headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.post("/echo", json={"a": 1}, headers={"X-API-Key": KEY[:-1]}).status_code == 401
        assert c.post("/echo", json={"a": 1}, headers={"Authorization": f"Basic {KEY}"}).status_code == 401
        assert c.post("/echo", json={"a": 1}, headers={"Authorization": "Bearer"}).status_code == 401
        for method in ("put", "patch"):
            assert getattr(c, method)("/echo", json={"a": 1}).status_code == 401, method
            assert getattr(c, method)("/echo", json={"a": 1}, headers={"X-API-Key": KEY}).status_code == 200, method
        assert c.delete("/echo").status_code == 401
        assert c.delete("/echo", headers={"X-API-Key": KEY}).status_code == 204


def test_api_key_leaves_reads_health_and_preflight_open(dbpath):
    with TestClient(make_app(deps.Settings(api_key=KEY)), raise_server_exceptions=False) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/").status_code == 200
        assert c.get("/db").json() == {"one": 1}
        assert c.get("/boom").status_code == 409  # the request reached the route
        assert c.head("/health").status_code != 401  # HEAD passes the key check (FastAPI itself answers 404/405 here)
        assert c.options("/echo").status_code != 401
        assert c.post("/health").status_code != 401  # /health is always open (405 here)


def test_unauthorised_requests_are_metered_and_carry_request_ids(dbpath):
    observability.METRICS.reset()
    with TestClient(make_app(deps.Settings(api_key=KEY)), raise_server_exceptions=False) as c:
        r = c.post("/echo", json={"a": 1})
        assert r.status_code == 401
        assert r.json().get("request_id") == r.headers.get("x-request-id")
    text = observability.METRICS.render_prometheus()
    assert 'stockline_requests_total{method="POST",path="/echo",status="401"} 1' in text


def test_install_convenience_wraps_auth_and_headers(dbpath):
    app = FastAPI()
    security.install(app, deps.Settings(api_key=KEY))

    @app.post("/w")
    def write():
        return {"ok": True}

    @app.get("/r")
    def read():
        return {"ok": True}

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/w")
        assert r.status_code == 401
        assert r.json() == {"detail": "missing or invalid API key", "code": "unauthorized"}
        _assert_baseline_headers(r, "POST /w")
        assert c.post("/w", headers={"X-API-Key": KEY}).status_code == 200
        ok = c.get("/r")
        assert ok.status_code == 200
        _assert_baseline_headers(ok, "GET /r")
        assert ok.headers.get("cache-control") == "no-store"
    assert [m.cls for m in app.user_middleware] == [security.SecurityHeadersMiddleware, security.ApiKeyMiddleware]


# --------------------------------------------------------------------------- CORS
def test_cors_preflight_and_exposed_headers_when_configured(dbpath):
    origin = "https://ui.example"
    with TestClient(make_app(deps.Settings(cors_origins=(origin,), api_key=KEY)), raise_server_exceptions=False) as c:
        pre = c.options(
            "/echo",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "X-API-Key, Content-Type"},
        )
        assert pre.status_code == 200
        assert pre.headers.get("access-control-allow-origin") == origin
        assert "POST" in pre.headers.get("access-control-allow-methods", "")
        assert "x-api-key" in pre.headers.get("access-control-allow-headers", "").lower()
        _assert_baseline_headers(pre, "preflight")  # headers middleware is outermost

        simple = c.get("/health", headers={"Origin": origin})
        assert simple.status_code == 200
        assert simple.headers.get("access-control-allow-origin") == origin
        exposed = {h.strip().lower() for h in simple.headers.get("access-control-expose-headers", "").split(",")}
        for name in ("Idempotent-Replayed", "X-Request-ID", "X-Response-Time-ms", "X-Row-Count", "X-Truncated", "Content-Disposition"):
            assert name.lower() in exposed, name

        other = c.get("/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in other.headers


def test_cors_absent_when_not_configured(client):
    r = client.get("/health", headers={"Origin": "https://ui.example"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers
    pre = client.options("/echo", headers={"Origin": "https://ui.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in pre.headers
