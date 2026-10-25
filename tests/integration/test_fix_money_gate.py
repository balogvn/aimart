"""What gets quoted, verified and charged before any work runs.

M3: a tool this deployment does not serve is never quoted or verified (the quantum tier is off
by default): it is a free "Unknown tool".
Q1: the advertised table limit matches the service's, and a request that fails validation is
told so before it is asked to pay (400 / isError, not 402 / PaymentRequired), on REST and MCP.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit
from aimart_parquet.payments import Payments
from aimart_parquet.service import MAX_QUERY_TABLES

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

ARTIFACT = "Zz" * 12  # well-formed, never issued
QUANTUM_TOOLS = ("quantum_compress_features", "quantum_optimize_portfolio")


@pytest.fixture
async def quantum_off(
    make_settings: Callable[..., Settings],
) -> AsyncIterator[kit.AppHarness]:
    """The production default: the quantum tier is not served."""
    async with kit.running_app(make_settings(enable_quantum=False)) as harness:
        yield harness


# ------------------------------------------------------------------------------ M3
@pytest.mark.parametrize("tool", QUANTUM_TOOLS)
async def test_unserved_tool_is_not_quoted(tool: str, quantum_off: kit.AppHarness) -> None:
    # Before: a $0.50 / $1.00 PaymentRequired for a tool tools/list does not even show.
    async with kit.mcp_over_http(quantum_off.app) as client:
        result = await client.call_tool(tool, {"artifact_id": ARTIFACT})
    assert result.is_error
    assert result.structured_content is None or "accepts" not in result.structured_content
    assert f"Unknown tool: {tool}" in result.content[0].text
    fake = quantum_off.facilitator
    assert fake is not None
    assert fake.supported_calls == 0  # not even initialized for it
    assert fake.verified == []


@pytest.mark.parametrize("tool", QUANTUM_TOOLS)
async def test_unserved_tool_with_a_payment_attached_is_never_verified(
    tool: str, quantum_off: kit.AppHarness, settings: Settings
) -> None:
    # An agent holding a quote from a deployment that does serve quantum (here: the enabled
    # fixture's) pays for a tool this one does not serve: nothing is verified or charged.
    paying = Payments(settings, [kit.FakeFacilitator()])
    reqs, resource = await paying.mcp_requirements(paying.tool_operation(tool))  # type: ignore[arg-type]
    pr = await paying.server.create_payment_required_response(reqs, resource, error="x")
    meta = await kit.mcp_payment_meta(
        kit.paying_client(), pr.model_dump(by_alias=True, exclude_none=True)
    )
    async with kit.mcp_over_http(quantum_off.app) as client:
        result = await client.call_tool(tool, {"artifact_id": ARTIFACT}, meta=meta)
    assert result.is_error
    assert f"Unknown tool: {tool}" in result.content[0].text
    fake = quantum_off.facilitator
    assert fake is not None
    assert fake.verified == []
    assert fake.settled == []


def test_tool_operation_follows_what_the_deployment_serves(
    make_settings: Callable[..., Settings],
) -> None:
    off = Payments(make_settings(enable_quantum=False), [kit.FakeFacilitator()])
    on = Payments(make_settings(enable_quantum=True), [kit.FakeFacilitator()])
    for tool in QUANTUM_TOOLS:
        assert off.tool_operation(tool) is None
        op = on.tool_operation(tool)
        assert op is not None
        assert op.tool == tool
    assert off.tool_operation("convert_to_parquet") is not None
    assert off.tool_operation("verify_provenance") is None  # free tools are never gated
    assert set(off.mcp_extension().settings()["prices"]) == {
        "convert_to_parquet",
        "inspect_parquet",
        "preview_parquet",
        "query_parquet",
    }


async def test_unserved_rest_routes_are_404_not_402(quantum_off: kit.AppHarness) -> None:
    for path in ("/v1/quantum/compress", "/v1/quantum/portfolio"):
        r = await quantum_off.http.post(path, json={"artifact_id": ARTIFACT})
        assert r.status_code == 404, path
        assert "payment-required" not in r.headers
    resources = (await quantum_off.http.get("/.well-known/x402")).json()["resources"]
    assert not any("quantum" in url for url in resources)


# ------------------------------------------------------------------------------ Q1: the limit
def extra_tables(n: int) -> dict[str, dict[str, str]]:
    return {f"t{i}": {"artifact_id": ARTIFACT} for i in range(n)}


async def test_openapi_table_limit_counts_data(free_app: kit.AppHarness) -> None:
    schema = (await free_app.http.get("/openapi.json")).json()
    tables = schema["components"]["schemas"]["QueryBody"]["properties"]["tables"]
    [obj] = [s for s in tables["anyOf"] if s.get("type") == "object"]
    assert obj["maxProperties"] == MAX_QUERY_TABLES - 1 == 7  # plus `data`: the service's 8
    assert obj["propertyNames"]["pattern"] == "^[a-z_][a-z0-9_]{0,62}$"
    assert "7" in tables["description"]


async def test_mcp_table_limit_counts_data(paid_app: kit.AppHarness) -> None:
    async with kit.mcp_over_http(paid_app.app) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    tables = tools["query_parquet"].input_schema["properties"]["tables"]
    [obj] = [s for s in tables["anyOf"] if s.get("type") == "object"]
    assert obj["maxProperties"] == MAX_QUERY_TABLES - 1
    assert "up to 7 besides data" in tables["description"]


# ------------------------------------------------------------------------------ Q1: the order
INVALID_REST: list[tuple[str, dict[str, Any]]] = [
    # schema-valid before the fix (maxProperties 8), refused by the service after payment
    ("/v1/query", {"artifact_id": ARTIFACT, "sql": "SELECT 1", "tables": extra_tables(8)}),
    ("/v1/query", {"artifact_id": ARTIFACT, "sql": "SELECT 1", "tables": {"Bad-Name": {}}}),
    ("/v1/query", {"artifact_id": ARTIFACT, "sql": "SELECT 1", "limit": 0}),
    ("/v1/query", {"artifact_id": ARTIFACT}),
    ("/v1/preview", {"artifact_id": ARTIFACT, "limit": 0}),
    ("/v1/preview", {"artifact_id": ARTIFACT, "format": "xml"}),
    ("/v1/inspect", {"artifact_id": ARTIFACT, "bogus": 1}),
    ("/v1/convert", {"url": "https://e.example/a.csv", "options": {"sheet": []}}),
    ("/v1/quantum/compress", {"artifact_id": ARTIFACT, "options": {"components": 0}}),
    ("/v1/quantum/portfolio", {"artifact_id": ARTIFACT, "options": {"budget": "two"}}),
]


@pytest.mark.parametrize(
    ("path", "body"), INVALID_REST, ids=lambda v: v if isinstance(v, str) else ""
)
async def test_rest_validation_error_comes_before_the_payment_request(
    path: str, body: dict[str, Any], paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    r = await paid_app.http.post(path, json=body)
    assert r.status_code == 400, r.text
    assert "payment-required" not in r.headers
    error = r.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["hint"] == "Fix the listed fields."
    assert error["details"]["problems"]
    assert facilitator.supported_calls == 0
    assert facilitator.verified == []


async def test_rest_malformed_json_comes_before_the_payment_request(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    r = await paid_app.http.post(
        "/v1/inspect", content=b"{nope", headers={"content-type": "application/json"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert facilitator.verified == []


async def test_rest_valid_body_is_still_quoted_and_paid(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    body = {"artifact_id": ARTIFACT, "sql": "SELECT 1", "tables": extra_tables(7)}
    pr = kit.payment_required(await paid_app.http.post("/v1/query", json=body))
    assert pr["accepts"][0]["amount"] == "5000"
    # Paid, the body reaches the route intact (the pre-check must not consume it): the unknown
    # artifact is a 404 from the service, so the route saw every field.
    r = await paid_app.paying.post("/v1/query", json=body)
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "artifact_not_found"
    assert len(facilitator.verified) == 1
    assert facilitator.settled == []


async def test_rest_paid_request_with_an_invalid_body_is_not_verified(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    good = {"artifact_id": ARTIFACT, "sql": "SELECT 1"}
    quote = await paid_app.http.post("/v1/query", json=good)
    header = await _signature_for(quote)
    bad = good | {"tables": extra_tables(8)}
    r = await paid_app.http.post("/v1/query", json=bad, headers={"payment-signature": header})
    assert r.status_code == 400
    assert facilitator.verified == []
    assert facilitator.settled == []


async def _signature_for(quote: Any) -> str:
    import base64
    import json

    from x402 import parse_payment_required

    required = parse_payment_required(base64.b64decode(quote.headers["payment-required"]))
    payload = await kit.paying_client().create_payment_payload(required)
    return base64.b64encode(
        json.dumps(payload.model_dump(by_alias=True, exclude_none=True)).encode()
    ).decode()


INVALID_MCP: list[tuple[str, dict[str, Any]]] = [
    (
        "query_parquet",
        {
            "artifact_id": ARTIFACT,
            "sql": "SELECT 1",
            "tables": {f"t{i}": ARTIFACT for i in range(8)},
        },
    ),
    (
        "query_parquet",
        {"artifact_id": ARTIFACT, "sql": "SELECT 1", "tables": {"Bad-Name": ARTIFACT}},
    ),
    ("query_parquet", {"artifact_id": ARTIFACT, "sql": "SELECT 1", "limit": 0}),
    ("query_parquet", {"artifact_id": ARTIFACT}),
    ("preview_parquet", {"artifact_id": ARTIFACT, "limit": 0}),
    ("inspect_parquet", {"artifact_id": "x" * 65}),
    ("convert_to_parquet", {"url": "https://e.example/a.csv", "options": {"sheet": []}}),
]


@pytest.mark.parametrize(
    ("tool", "args"), INVALID_MCP, ids=lambda v: v if isinstance(v, str) else ""
)
async def test_mcp_validation_error_comes_before_the_payment_request(
    tool: str, args: dict[str, Any], paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    async with kit.mcp_over_http(paid_app.app) as client:
        result = await client.call_tool(tool, args)
    assert result.is_error
    assert result.structured_content is None or "accepts" not in result.structured_content
    assert "validation error" in result.content[0].text
    assert facilitator.supported_calls == 0
    assert facilitator.verified == []


async def test_mcp_paid_call_with_invalid_arguments_is_not_verified(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    good = {"artifact_id": ARTIFACT, "sql": "SELECT 1"}
    async with kit.mcp_over_http(paid_app.app) as client:
        unpaid = await client.call_tool("query_parquet", good)
        meta = await kit.mcp_payment_meta(kit.paying_client(), unpaid.structured_content)
        bad = good | {"limit": 0}
        result = await client.call_tool("query_parquet", bad, meta=meta)
        ok = await client.call_tool("query_parquet", good, meta=meta)
    assert result.is_error
    assert "validation error" in result.content[0].text
    assert ok.is_error  # the payment is still good: the valid call ran (unknown artifact: 404)
    assert ok.structured_content["error"]["code"] == "artifact_not_found"
    assert len(facilitator.verified) == 1  # only the valid call was verified
    assert facilitator.settled == []


# ------------------------------------------------------------------------------ Q1: probes
# Validating before the quote must never turn a price probe into a 400: catalogs (x402scan)
# and `make smoke` learn prices that way, and a route that answers 400 drops out of them.
async def _bare_post(app: Any, path: str, headers: list[tuple[bytes, bytes]]) -> tuple[int, bool]:
    """POST with no body through the whole ASGI stack, with exactly these headers (httpx always
    adds Content-Length: 0, which is not what `curl -X POST` sends)."""
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), *headers],
        "client": ("203.0.113.9", 4000),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    start = sent[0]
    return start["status"], any(k == b"payment-required" for k, _ in start["headers"])


@pytest.mark.parametrize(
    "headers",
    [[], [(b"content-length", b"0")]],
    ids=["no-content-length", "content-length-0"],
)
async def test_a_bodiless_price_probe_is_quoted_on_every_paid_route(
    headers: list[tuple[bytes, bytes]], paid_app: kit.AppHarness
) -> None:
    # Before: /v1/convert/upload answered `curl -X POST` (no Content-Length) with a 400.
    from aimart_parquet.payments.pricing import active_operations

    for op in active_operations(paid_app.settings):
        status, quoted = await _bare_post(paid_app.app, op.path, headers)
        assert (status, quoted) == (402, True), op.path


async def test_a_chunked_non_form_body_to_the_upload_route_is_refused_unread(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    status, quoted = await _bare_post(
        paid_app.app, "/v1/convert/upload", [(b"transfer-encoding", b"chunked")]
    )
    assert (status, quoted) == (400, False)
    assert facilitator.verified == []


async def test_every_bazaar_example_input_is_quoted_not_rejected(
    paid_app: kit.AppHarness,
) -> None:
    # Catalogs may replay the advertised example body: it must reach its 402.
    from aimart_parquet.payments.bazaar import EXAMPLES
    from aimart_parquet.payments.pricing import active_operations

    for op in active_operations(paid_app.settings):
        example = EXAMPLES[op.key]["input"]
        if op.key == "convert_upload":
            r = await paid_app.http.post(op.path, data=example)
        else:
            r = await paid_app.http.post(op.path, json=example)
        assert r.status_code == 402, (op.path, r.text)
        assert kit.payment_required(r)["accepts"][0]["amount"]
