"""Unit tests for the framework-free primitives in ``app.common``."""
from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app import common
from app.common import NOT_FOUND, VALIDATION, BridgeCall, ServiceError, not_found

APP_DIR = Path(common.__file__).resolve().parent
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

EXPECTED_BROWSER = (
    "__init__.py", "common.py", "db.py", "schemas.py", "ledger.py", "catalog.py",
    "inventory.py", "orders.py", "reports.py", "service.py", "seed.py", "bridge.py",
)
EXPECTED_SERVER_ONLY = frozenset({"main.py", "deps.py", "observability.py", "security.py"})
EXPECTED_CODES = {
    "NOT_FOUND": "not_found",
    "CONFLICT": "conflict",
    "INSUFFICIENT_STOCK": "insufficient_stock",
    "VERSION_CONFLICT": "version_conflict",
    "DUPLICATE": "duplicate",
    "IDEMPOTENCY_KEY_REUSE": "idempotency_key_reuse",
    "IDEMPOTENCY_CONFLICT": "idempotency_conflict",
    "PRODUCT_INACTIVE": "product_inactive",
    "INVALID_STATE": "invalid_state",
    "VALIDATION": "validation_error",
    "METHOD_NOT_ALLOWED": "method_not_allowed",
    "UNAUTHORIZED": "unauthorized",
    "POOL_EXHAUSTED": "pool_exhausted",
    "INTERNAL": "internal",
}
FORBIDDEN_IMPORTS = {"fastapi", "starlette", "uvicorn", "threading", "queue"}
SERVER_ONLY_LOCAL = {"deps", "main", "observability", "security"}


def _call(**query: str) -> BridgeCall:
    return BridgeCall(conn=sqlite3.connect(":memory:"), params={}, query=dict(query), body={}, headers={}, out_headers={})


# --------------------------------------------------------------------------- module lists
def test_module_lists_are_exact_and_disjoint():
    assert common.BROWSER_MODULES == EXPECTED_BROWSER
    assert common.SERVER_ONLY_MODULES == EXPECTED_SERVER_ONLY
    assert isinstance(common.BROWSER_MODULES, tuple)
    assert isinstance(common.SERVER_ONLY_MODULES, frozenset)
    assert not set(common.BROWSER_MODULES) & common.SERVER_ONLY_MODULES
    assert len(set(common.BROWSER_MODULES)) == len(common.BROWSER_MODULES)


def test_module_lists_cover_every_app_module():
    present = {p.name for p in APP_DIR.glob("*.py")}
    assert present, "the app package must contain modules"
    assert present <= set(common.BROWSER_MODULES) | common.SERVER_ONLY_MODULES


def test_browser_modules_literal_is_regex_parseable():
    src = (APP_DIR / "common.py").read_text(encoding="utf-8")
    m = re.search(r"^BROWSER_MODULES: tuple\[str, \.\.\.\] = \((.*?)\)\n", src, re.S | re.M)
    assert m, "BROWSER_MODULES must keep the documented literal form"
    assert "#" not in m.group(1)
    assert tuple(re.findall(r'"([^"]+)"', m.group(1))) == EXPECTED_BROWSER
    assert 'SERVER_ONLY_MODULES: frozenset[str] = frozenset({"main.py", "deps.py", "observability.py", "security.py"})' in src


@pytest.mark.parametrize("name", ["common.py", "db.py", "ledger.py"])
def test_browser_modules_are_framework_free(name: str):
    tree = ast.parse((APP_DIR / name).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in FORBIDDEN_IMPORTS, f"{name} imports {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                assert module.split(".")[0] not in SERVER_ONLY_LOCAL, f"{name} imports server-only module {module}"
                if not module:
                    assert not {a.name for a in node.names} & SERVER_ONLY_LOCAL, f"{name} imports a server-only module"
            else:
                assert module.split(".")[0] not in FORBIDDEN_IMPORTS, f"{name} imports {module}"


def test_bridge_type_aliases_exist():
    assert hasattr(common, "BridgeHandler")
    assert hasattr(common, "BridgeRoute")


# --------------------------------------------------------------------------- errors
def test_error_code_constants_have_exact_values():
    for name, value in EXPECTED_CODES.items():
        assert getattr(common, name) == value, name
    assert len(set(EXPECTED_CODES.values())) == len(EXPECTED_CODES)


def test_service_error_positional_compatibility_and_body():
    e = ServiceError(409, "msg")
    assert (e.status, e.detail, e.code) == (409, "msg", "error")
    assert str(e) == "msg"
    assert e.to_body() == {"detail": "msg", "code": "error"}
    assert isinstance(e, Exception)
    with pytest.raises(ServiceError, match="msg"):
        raise ServiceError(409, "msg")
    e2 = ServiceError(status=422, detail="bad", code=VALIDATION)
    assert e2.to_body() == {"detail": "bad", "code": "validation_error"}


def test_not_found_helper():
    e = not_found("product", 7)
    assert (e.status, e.detail, e.code) == (404, "product 7 not found", NOT_FOUND)
    assert not_found("store", "X1").detail == "store X1 not found"


def test_error_body_model():
    assert common.ErrorBody(detail="x").model_dump() == {"detail": "x", "code": "error", "request_id": None}
    body = common.ErrorBody(detail=[{"loc": ["body"], "msg": "bad"}], code=VALIDATION, request_id="r1")
    assert body.model_dump() == {"detail": [{"loc": ["body"], "msg": "bad"}], "code": "validation_error", "request_id": "r1"}


# --------------------------------------------------------------------------- time / hashing / sql helpers
def test_now_iso_matches_sqlite_format_and_sorts_lexicographically():
    mem = sqlite3.connect(":memory:")
    before = mem.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
    stamp = common.now_iso()
    after = mem.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
    assert ISO_RE.match(stamp), stamp
    assert ISO_RE.match(before) and len(before) == len(stamp)
    assert before <= stamp <= after
    parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    assert abs((parsed - datetime.now(UTC)).total_seconds()) < 5


def test_request_hash_is_stable_and_key_order_independent():
    payload = {"store_id": 1, "lines": [[1, 2], [3, 4]]}
    a = common.request_hash(payload)
    b = common.request_hash({"lines": [[1, 2], [3, 4]], "store_id": 1})
    assert a == b
    assert re.fullmatch(r"[0-9a-f]{32}", a)
    assert common.request_hash({"store_id": 2, "lines": [[1, 2], [3, 4]]}) != a
    assert common.request_hash({"store_id": 1, "lines": [[3, 4], [1, 2]]}) != a  # list order is significant; callers sort
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]
    assert a == expected


def test_like_pattern_escapes_wildcards():
    assert common.like_pattern("50%_off\\") == "%50\\%\\_off\\\\%"
    mem = sqlite3.connect(":memory:")

    def matches(value: str, q: str) -> bool:
        return bool(mem.execute("SELECT ? LIKE ? ESCAPE '\\'", (value, common.like_pattern(q))).fetchone()[0])

    assert matches("Rice 50% off", "50%")
    assert not matches("Rice 50x off", "50%")
    assert matches("a_b", "a_b")
    assert not matches("axb", "a_b")
    assert matches("back\\slash", "k\\s")
    assert matches("anything", "")


def test_page_shape():
    assert common.page([1, 2], 10, 20, 0) == {"items": [1, 2], "total": 10, "limit": 20, "offset": 0}


# --------------------------------------------------------------------------- csv helpers
def test_csv_text_header_values_and_types():
    rows = [
        {"id": 1, "name": "Widget", "flag": True, "none": None, "neg": -5, "extra": "ignored"},
        {"id": 2, "name": 'say "hi", ok', "flag": False, "none": "", "neg": 0},
    ]
    text = common.csv_text(rows, ["id", "name", "flag", "none", "neg", "missing"])
    assert text == 'id,name,flag,none,neg,missing\n1,Widget,true,,-5,\n2,"say ""hi"", ok",false,,0,\n'
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0] == ["id", "name", "flag", "none", "neg", "missing"]
    assert parsed[2][1] == 'say "hi", ok'
    assert "\r" not in text


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_csv_text_formula_guard(prefix: str):
    text = common.csv_text([{"v": prefix + "cmd"}], ["v"])
    assert text.startswith("v\n")
    assert "'" + prefix + "cmd" in text


def test_csv_text_numbers_are_not_guarded():
    assert common.csv_text([{"n": -5, "s": "-5", "f": -1.5}], ["n", "s", "f"]) == "n,s,f\n-5,'-5,-1.5\n"


def test_csv_text_empty_rows_keep_header():
    assert common.csv_text([], ["a", "b"]) == "a,b\n"


def test_csv_filename_and_row_cap():
    before = datetime.now(UTC)
    name = common.csv_filename("inventory")
    after = datetime.now(UTC)
    assert name in {f"inventory-{before:%Y%m%d}.csv", f"inventory-{after:%Y%m%d}.csv"}
    assert re.fullmatch(r"inventory-\d{8}\.csv", name)
    assert common.CSV_ROW_CAP == 10_000


# --------------------------------------------------------------------------- BridgeCall query helpers
def test_qint_parses_defaults_and_bounds():
    assert _call(limit="5").qint("limit", 20, lo=1, hi=100) == 5
    assert _call().qint("limit", 20, lo=1, hi=100) == 20
    assert _call().qint("store_id") is None
    assert _call(limit="100").qint("limit", 20, lo=1, hi=100) == 100
    for bad in ("abc", "1.5", "", "0", "101"):
        with pytest.raises(ServiceError) as ei:
            _call(limit=bad).qint("limit", 20, lo=1, hi=100)
        assert (ei.value.status, ei.value.code) == (422, VALIDATION), bad
        assert "limit" in ei.value.detail


@pytest.mark.parametrize("raw", ["1", "true", "yes", "TRUE", "Yes"])
def test_qbool_truthy(raw: str):
    assert _call(low_stock=raw).qbool("low_stock") is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "", "maybe"])
def test_qbool_falsy(raw: str):
    assert _call(low_stock=raw).qbool("low_stock") is False


def test_qbool_default():
    assert _call().qbool("x") is False
    assert _call().qbool("x", True) is True


def test_qstr_choices_and_length():
    choices = ("placed", "cancelled", "fulfilled")
    assert _call(status="placed").qstr("status", choices=choices) == "placed"
    assert _call().qstr("status") is None
    assert _call().qstr("q", "dflt") == "dflt"
    assert _call(q="abc").qstr("q", max_length=3) == "abc"
    with pytest.raises(ServiceError) as ei:
        _call(status="bogus").qstr("status", choices=choices)
    assert (ei.value.status, ei.value.code) == (422, VALIDATION)
    assert "status" in ei.value.detail
    with pytest.raises(ServiceError) as ei:
        _call(q="abcd").qstr("q", max_length=3)
    assert (ei.value.status, ei.value.code) == (422, VALIDATION)
