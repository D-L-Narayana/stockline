"""OpenAPI contract of the assembled application (``GET /openapi.json``).

The published document is what API clients and the interactive docs rely on, so it is pinned here as a
contract: the operation table from the design plan, unique operation ids, tags and summaries everywhere,
a described body for every success response, the shared ``ErrorBody`` for validation / conflict / not-found
errors, the idempotency header documentation on ``POST /orders`` and ``POST /transfers``, typed pages and no
internal fields.

Rule for success responses: every 2xx response other than 204 must describe its body. JSON responses must
resolve (through ``$ref``) to an object with ``properties`` or an array with typed ``items``; the four CSV
exports document ``text/csv`` and ``/metrics`` documents ``text/plain``, both with a ``string`` schema —
text content counts as the documented schema there.

Each contract rule is a ``check_*`` function over the document. The ``test_*`` functions run them against the
served document; ``test_checks_fail_when_the_contract_is_broken`` runs them against deliberately mutated copies
and requires every one of them to fail, so the checks themselves are known to bite.
"""
from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

from app import __version__

# (method, path) — the complete public surface (PLAN route table); the bridge mirrors everything but /metrics.
EXPECTED_OPERATIONS = {
    ("get", "/health"),
    ("get", "/integrity"),
    ("post", "/integrity/rebuild"),
    ("get", "/metrics"),
    ("post", "/stores"),
    ("get", "/stores"),
    ("get", "/stores/{store_id}"),
    ("patch", "/stores/{store_id}"),
    ("post", "/products"),
    ("get", "/products"),
    ("get", "/products/{product_id}"),
    ("patch", "/products/{product_id}"),
    ("delete", "/products/{product_id}"),
    ("get", "/products/{product_id}/inventory"),
    ("get", "/inventory"),
    ("get", "/inventory/export.csv"),
    ("post", "/inventory/{store_id}/receipts"),
    ("get", "/inventory/{store_id}/{product_id}"),
    ("post", "/inventory/{store_id}/{product_id}/adjust"),
    ("get", "/inventory/{store_id}/{product_id}/movements"),
    ("get", "/movements"),
    ("get", "/movements/export.csv"),
    ("post", "/transfers"),
    ("get", "/transfers"),
    ("get", "/transfers/{transfer_id}"),
    ("post", "/orders"),
    ("get", "/orders"),
    ("get", "/orders/export.csv"),
    ("get", "/orders/{order_id}"),
    ("post", "/orders/{order_id}/cancel"),
    ("post", "/orders/{order_id}/fulfil"),
    ("get", "/reports/reorder"),
    ("get", "/reports/reorder.csv"),
    ("get", "/reports/summary"),
    ("get", "/reports/sales"),
}
TEXT_OPERATIONS = {
    ("get", "/inventory/export.csv"): "text/csv",
    ("get", "/movements/export.csv"): "text/csv",
    ("get", "/orders/export.csv"): "text/csv",
    ("get", "/reports/reorder.csv"): "text/csv",
    ("get", "/metrics"): "text/plain",
}
# Operations whose implementation can answer 409 (duplicate, version_conflict, insufficient_stock, invalid_state,
# idempotency_conflict, or a negative ledger balance on rebuild).
CONFLICT_OPERATIONS = {
    ("post", "/stores"),
    ("post", "/products"),
    ("post", "/inventory/{store_id}/{product_id}/adjust"),
    ("post", "/transfers"),
    ("post", "/orders"),
    ("post", "/orders/{order_id}/cancel"),
    ("post", "/orders/{order_id}/fulfil"),
    ("post", "/integrity/rebuild"),
}
# Operations that address an entity (or reference one in their body) and answer 404 ``not_found`` when it is missing.
NOT_FOUND_OPERATIONS = {
    ("get", "/stores/{store_id}"),
    ("patch", "/stores/{store_id}"),
    ("get", "/products/{product_id}"),
    ("patch", "/products/{product_id}"),
    ("delete", "/products/{product_id}"),
    ("get", "/products/{product_id}/inventory"),
    ("post", "/inventory/{store_id}/receipts"),
    ("get", "/inventory/{store_id}/{product_id}"),
    ("post", "/inventory/{store_id}/{product_id}/adjust"),
    ("post", "/transfers"),
    ("get", "/transfers/{transfer_id}"),
    ("post", "/orders"),
    ("get", "/orders/{order_id}"),
    ("post", "/orders/{order_id}/cancel"),
    ("post", "/orders/{order_id}/fulfil"),
}
IDEMPOTENT_OPERATIONS = {("post", "/orders"), ("post", "/transfers")}
TYPED_PAGES = {"OrderPage": "OrderOut", "InventoryPage": "InventoryRowOut", "TransferPage": "TransferOut", "ProductPage": "ProductOut"}
TAGS = {"system", "catalog", "inventory", "orders", "reports"}
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")


@pytest.fixture()
def spec(client) -> dict:
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    return r.json()


# --------------------------------------------------------------------------- helpers
def operations(spec: dict):
    """Yield ``(method, path, operation)`` for every operation in the document."""
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method in HTTP_METHODS:
                yield method, path, op


def resolve(schema: Any, spec: dict) -> dict:
    """Follow ``$ref`` pointers into ``components/schemas`` until a concrete schema is reached."""
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = spec["components"]["schemas"][name]
        seen += 1
        assert seen < 10, "circular $ref"
    assert isinstance(schema, dict), schema
    return schema


def ref_name(schema: Any) -> str | None:
    return schema["$ref"].rsplit("/", 1)[-1] if isinstance(schema, dict) and "$ref" in schema else None


def mentions_max_length(schema: Any, value: int) -> bool:
    if isinstance(schema, dict):
        return schema.get("maxLength") == value or any(mentions_max_length(v, value) for v in schema.values())
    if isinstance(schema, list):
        return any(mentions_max_length(v, value) for v in schema)
    return False


def contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(contains_key(v, key) for v in value.values())
    if isinstance(value, list):
        return any(contains_key(v, key) for v in value)
    return False


# --------------------------------------------------------------------------- contract checks
def check_identity(spec: dict) -> None:
    assert spec["openapi"].startswith("3.")
    assert spec["info"]["title"] == "StockLine Inventory API"
    assert spec["info"]["version"] == __version__ == "0.2.0"
    assert spec["info"]["description"]


def check_operation_table(spec: dict) -> None:
    found = {(method, path) for method, path, _op in operations(spec)}
    assert found == EXPECTED_OPERATIONS, {"unexpected": sorted(found - EXPECTED_OPERATIONS), "missing": sorted(EXPECTED_OPERATIONS - found)}


def check_ids_tags_summaries(spec: dict) -> None:
    ids = []
    for method, path, op in operations(spec):
        assert op.get("operationId"), (method, path)
        ids.append(op["operationId"])
        assert op.get("tags"), (method, path)
        assert op["tags"][0] in TAGS, (method, path, op["tags"])
        assert op.get("summary", "").strip(), (method, path)
    assert len(ids) == len(set(ids)), "operation ids must be unique"


def check_success_bodies(spec: dict) -> None:
    for method, path, op in operations(spec):
        for code, response in op["responses"].items():
            if not code.startswith("2"):
                continue
            content = response.get("content", {})
            if code == "204":
                assert not content, (method, path, "a 204 has no body")
                continue
            assert content, (method, path, code, "success response without content")
            text_media = TEXT_OPERATIONS.get((method, path))
            if text_media is not None:
                assert set(content) == {text_media}, (method, path, code, sorted(content))
                assert content[text_media]["schema"] == {"type": "string"}, (method, path, code)
                continue
            assert set(content) == {"application/json"}, (method, path, code, sorted(content))
            schema = resolve(content["application/json"]["schema"], spec)
            if schema.get("type") == "array":
                items = resolve(schema["items"], spec)
                assert items.get("properties"), (method, path, code, "array items are untyped")
            else:
                assert schema.get("properties"), (method, path, code, "object schema without properties")


def check_error_documentation(spec: dict) -> None:
    for method, path, op in operations(spec):
        has_inputs = "requestBody" in op or op.get("parameters")
        if method in ("post", "patch") and has_inputs:
            assert "422" in op["responses"], (method, path, "422 not documented")
            assert ref_name(op["responses"]["422"]["content"]["application/json"]["schema"]) == "ErrorBody", (method, path)
        if (method, path) in CONFLICT_OPERATIONS:
            assert "409" in op["responses"], (method, path, "409 not documented")
            assert ref_name(op["responses"]["409"]["content"]["application/json"]["schema"]) == "ErrorBody", (method, path)
        if (method, path) in NOT_FOUND_OPERATIONS:
            assert "404" in op["responses"], (method, path, "404 not documented")
            assert ref_name(op["responses"]["404"]["content"]["application/json"]["schema"]) == "ErrorBody", (method, path)
    rebuild = spec["paths"]["/integrity/rebuild"]["post"]["responses"]
    assert ref_name(rebuild["401"]["content"]["application/json"]["schema"]) == "ErrorBody", "rebuild is write-protected by the API key"


def check_idempotency_documentation(spec: dict) -> None:
    for method, path in IDEMPOTENT_OPERATIONS:
        op = spec["paths"][path][method]
        for code in ("200", "201"):
            response = op["responses"][code]
            assert "Idempotent-Replayed" in response.get("headers", {}), (path, code)
            header = response["headers"]["Idempotent-Replayed"]
            assert set(header["schema"].get("enum", [])) == {"true", "false"}, (path, code)
            assert ref_name(response["content"]["application/json"]["schema"]) in {"OrderOut", "TransferOut"}, (path, code)
        keys = [p for p in op.get("parameters", []) if p["in"] == "header" and p["name"].lower() == "idempotency-key"]
        assert len(keys) == 1, (path, "Idempotency-Key header parameter")
        assert keys[0].get("required", False) is False, (path, "the key is optional")
        assert mentions_max_length(keys[0]["schema"], 64), (path, keys[0]["schema"])


def check_components(spec: dict) -> None:
    schemas = spec["components"]["schemas"]
    assert set(schemas["ErrorBody"]["properties"]) == {"detail", "code", "request_id"}
    assert set(schemas["Health"]["properties"]) == {"status", "version", "schema_version", "runtime", "uptime_s"}
    for page, item in TYPED_PAGES.items():
        props = schemas[page]["properties"]
        assert set(props) == {"items", "total", "limit", "offset"}, page
        assert props["items"]["type"] == "array" and ref_name(props["items"]["items"]) == item, page
    assert set(schemas["MovementFeed"]["properties"]) == {"items", "limit", "next_before_id"}
    assert ref_name(schemas["MovementFeed"]["properties"]["items"]["items"]) == "Movement"
    assert "balance_after" in schemas["Movement"]["properties"]
    assert {"id", "from", "to", "from_store_id", "to_store_id", "product_id", "quantity", "idempotency_key", "created_at"} <= set(schemas["TransferOut"]["properties"])
    assert "updated_at" in schemas["OrderOut"]["properties"] and "updated_at" in schemas["ProductOut"]["properties"]
    assert ref_name(schemas["SummaryReport"]["properties"]["stores"]["items"]) == "StoreSummary"
    assert {"sold_window", "returned_window", "net_sold", "daily_velocity", "days_of_cover", "suggested_qty"} <= set(schemas["ReorderRow"]["properties"])
    assert not contains_key(schemas, "request_hash"), "request fingerprints are internal and must never be documented"


# --------------------------------------------------------------------------- the served document
def test_schema_loads_and_identifies_the_service(spec):
    check_identity(spec)


def test_operation_table_matches_the_plan(spec):
    check_operation_table(spec)


def test_every_operation_has_a_unique_id_tags_and_summary(spec):
    check_ids_tags_summaries(spec)


def test_every_success_response_describes_its_body(spec):
    check_success_bodies(spec)


def test_write_operations_document_validation_conflicts_and_not_found(spec):
    check_error_documentation(spec)


def test_idempotent_operations_document_the_replay_header_and_key(spec):
    check_idempotency_documentation(spec)


def test_components_are_typed_and_hide_internal_fields(spec):
    check_components(spec)


# --------------------------------------------------------------------------- the checks must bite
def _break_version(doc: dict) -> None:
    doc["info"]["version"] = "0.0.0"


def _drop_operation(doc: dict) -> None:
    del doc["paths"]["/reports/reorder.csv"]


def _duplicate_operation_ids(doc: dict) -> None:
    doc["paths"]["/stores"]["get"]["operationId"] = doc["paths"]["/stores"]["post"]["operationId"]


def _drop_summary(doc: dict) -> None:
    del doc["paths"]["/orders"]["get"]["summary"]


def _untype_success_body(doc: dict) -> None:
    doc["paths"]["/stores"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {"type": "array", "items": {}}


def _drop_csv_schema(doc: dict) -> None:
    doc["paths"]["/orders/export.csv"]["get"]["responses"]["200"]["content"] = {"text/csv": {"schema": {}}}


def _drop_conflict(doc: dict) -> None:
    del doc["paths"]["/orders"]["post"]["responses"]["409"]


def _generic_validation_error(doc: dict) -> None:
    doc["paths"]["/products"]["post"]["responses"]["422"]["content"]["application/json"]["schema"] = {"$ref": "#/components/schemas/HTTPValidationError"}


def _drop_not_found(doc: dict) -> None:
    del doc["paths"]["/orders/{order_id}"]["get"]["responses"]["404"]


def _drop_replay_header(doc: dict) -> None:
    del doc["paths"]["/transfers"]["post"]["responses"]["201"]["headers"]["Idempotent-Replayed"]


def _unbounded_key(doc: dict) -> None:
    for param in doc["paths"]["/orders"]["post"]["parameters"]:
        if param["name"].lower() == "idempotency-key":
            param["schema"] = {"type": "string"}


def _untyped_page(doc: dict) -> None:
    doc["components"]["schemas"]["OrderPage"]["properties"]["items"] = {"type": "array", "items": {}}


def _leak_request_hash(doc: dict) -> None:
    doc["components"]["schemas"]["OrderOut"]["properties"]["request_hash"] = {"type": "string"}


BREAKAGES: list[tuple[Callable[[dict], None], Callable[[dict], None]]] = [
    (check_identity, _break_version),
    (check_operation_table, _drop_operation),
    (check_ids_tags_summaries, _duplicate_operation_ids),
    (check_ids_tags_summaries, _drop_summary),
    (check_success_bodies, _untype_success_body),
    (check_success_bodies, _drop_csv_schema),
    (check_error_documentation, _drop_conflict),
    (check_error_documentation, _generic_validation_error),
    (check_error_documentation, _drop_not_found),
    (check_idempotency_documentation, _drop_replay_header),
    (check_idempotency_documentation, _unbounded_key),
    (check_components, _untyped_page),
    (check_components, _leak_request_hash),
]


@pytest.mark.parametrize(("check", "breakage"), BREAKAGES, ids=[f"{c.__name__}-{b.__name__.lstrip('_')}" for c, b in BREAKAGES])
def test_checks_fail_when_the_contract_is_broken(spec, check, breakage):
    """Self-check: each rule passes on the served document and fails on a copy with exactly one contract break."""
    check(spec)
    broken = copy.deepcopy(spec)
    breakage(broken)
    assert broken != spec, "the breakage must change the document"
    with pytest.raises(AssertionError):
        check(broken)
