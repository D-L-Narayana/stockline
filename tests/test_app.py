"""Assembled-application tests.

These cover what no single domain test can: router inclusion, middleware order (security headers on
static files, JSON and errors alike), the error-body contract, compatibility aliases, and the
architecture boundary between browser-shipped and server-only modules.  Expected header values are
spelled out literally on purpose, so this file checks the served contract rather than a constant.
"""
import ast
import sys
from pathlib import Path

from app import common

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

DEFAULT_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)
DOCS_CSP = (
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.jsdelivr.net; "
    "font-src 'self' https://cdn.jsdelivr.net data:; connect-src 'self'; object-src 'none'; "
    "base-uri 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
}
SERVER_IMPORTS = {"fastapi", "starlette", "uvicorn", "queue", "threading"}


# --------------------------------------------------------------------------- served contract
def test_health_reports_schema_version_and_runtime(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["version"] == "0.2.0"
    assert body["schema_version"] == 2
    assert body["runtime"] == "server"
    assert body["uptime_s"] >= 0


def test_security_headers_on_static_json_and_errors(client):
    for path in ("/", "/styles.css", "/app.js", "/health", "/orders/999999", "/nope"):
        r = client.get(path)
        assert r.headers.get("content-security-policy") == DEFAULT_CSP, path
        for name, value in SECURITY_HEADERS.items():
            assert r.headers.get(name) == value, (path, name)
        assert r.headers.get("x-request-id"), path
    assert client.get("/health").headers.get("cache-control") == "no-store"
    assert client.get("/").headers.get("cache-control") != "no-store"


def test_docs_get_their_own_policy(client):
    r = client.get("/docs")
    assert r.status_code == 200
    assert r.headers.get("content-security-policy") == DOCS_CSP


def test_error_body_has_code_and_request_id(client):
    r = client.get("/orders/999999", headers={"X-Request-ID": "lead-test-1"})
    assert r.status_code == 404
    assert r.json() == {"detail": "order 999999 not found", "code": "not_found", "request_id": "lead-test-1"}
    assert r.headers["x-request-id"] == "lead-test-1"


def test_validation_error_body_is_machine_readable(client):
    r = client.post("/orders", json={"store_id": "x"})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "validation_error"
    assert body["request_id"]
    assert isinstance(body["detail"], list)


def test_wrong_method_on_an_api_path_is_405_with_allow(client):
    """A known path with the wrong method answers 405 with ``Allow`` listing the declared methods — not the static mount's reply.

    The dashboard is mounted at ``/`` and matches every path, so without an explicit guard a wrong-method request
    would be answered by the static file server (405 without ``Allow`` for non-GET, 404 for GET on a POST-only path).
    """
    cases = {
        ("GET", "/integrity/rebuild"): "POST",
        ("PUT", "/orders"): "GET, POST",
        ("DELETE", "/health"): "GET",
        ("PATCH", "/inventory/1/1/adjust"): "POST",
        ("POST", "/reports/summary"): "GET",
    }
    for (method, path), allow in cases.items():
        r = client.request(method, path, headers={"X-Request-ID": "lead-405"})
        assert r.status_code == 405, (method, path, r.status_code, r.text)
        assert r.headers.get("allow") == allow, (method, path, r.headers.get("allow"))
        body = r.json()
        assert (body["code"], body["request_id"]) == ("method_not_allowed", "lead-405"), body
        assert r.headers.get("content-security-policy") == DEFAULT_CSP, (method, path)
    head = client.head("/health")  # FastAPI declares GET only; HEAD is therefore a wrong method, not an unknown path
    assert (head.status_code, head.headers.get("allow")) == (405, "GET"), (head.status_code, dict(head.headers))
    # Unknown paths and the dashboard itself are untouched by the guard.
    assert client.get("/nope").status_code == 404
    index = client.get("/")
    assert index.status_code == 200 and index.headers["content-type"].startswith("text/html")
    assert client.get("/styles.css").status_code == 200


def test_openapi_lists_the_new_surface(client):
    spec = client.get("/openapi.json").json()
    assert spec["info"]["version"] == "0.2.0"
    for path in ("/inventory/export.csv", "/orders/export.csv", "/movements", "/transfers/{transfer_id}", "/reports/summary", "/integrity/rebuild"):
        assert path in spec["paths"], path


def test_compatibility_aliases_and_facade():
    from app import deps, main, service

    assert main.get_conn is deps.get_conn
    assert main.reset_state is deps.reset_state
    assert service.ServiceError is common.ServiceError
    for name in ("place_order", "transfer", "reorder_report", "ledger_integrity", "create_product", "adjust_stock"):
        assert callable(getattr(service, name)), name


# --------------------------------------------------------------------------- architecture boundary
def _imports(path: Path) -> set[str]:
    assert path.is_file(), f"{path.name} is listed in BROWSER_MODULES but missing from app/"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.module:
                names.add(f".{node.module.split('.')[0]}")  # from .deps import x
            elif node.level:
                names.update(f".{alias.name}" for alias in node.names)  # from . import deps
            elif node.module:
                names.add(node.module.split(".")[0])
    return names


def test_every_app_module_is_classified():
    files = {p.name for p in APP.glob("*.py")}
    assert files == set(common.BROWSER_MODULES) | set(common.SERVER_ONLY_MODULES)
    assert not set(common.BROWSER_MODULES) & set(common.SERVER_ONLY_MODULES)
    assert len(common.BROWSER_MODULES) >= 10


def test_browser_modules_do_not_import_server_code():
    server_relative = {f".{name[:-3]}" for name in common.SERVER_ONLY_MODULES}
    for name in common.BROWSER_MODULES:
        found = _imports(APP / name)
        assert not (found & SERVER_IMPORTS), f"{name} imports server-only packages: {sorted(found & SERVER_IMPORTS)}"
        assert not (found & server_relative), f"{name} imports server-only modules: {sorted(found & server_relative)}"


def test_python_version_floor_is_respected():
    # CI runs 3.11; the local interpreter may be newer. Guard the syntax that breaks 3.11.
    assert sys.version_info >= (3, 11)
    for path in list(APP.glob("*.py")) + list((APP / "routers").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        assert "\ntype " not in src, f"{path.name} uses the 3.12 `type` statement"
        assert "typing.override" not in src and "from typing import override" not in src, path.name
