"""A refusal for "not exactly one input" names only the inputs the calling route or tool takes.

The service had one list for every surface: "Set one of: url, content_base64, artifact_id.".
inspect_parquet, preview_parquet, query_parquet and the quantum tools take no content_base64,
the local tools no artifact_id, and the REST quantum routes no content_base64, yet their hints
named them; remote convert_to_parquet took no artifact_id at all (the SDK dropped it, so the
call was quoted and, paid, verified and then refused "got 0 (none)"). An agent that followed
the hint was refused again. Each surface is checked against the schema a client reads: the
tool's input schema, or the route's request body in /openapi.json.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import mcp
import pytest

import aimart_testkit as kit
from aimart_parquet.artifacts import build_store
from aimart_parquet.mcp.server import build_mcp
from aimart_parquet.payments.pricing import active_operations
from aimart_parquet.service import INPUT_KINDS, Service

if TYPE_CHECKING:
    from mcp_types import CallToolResult, Tool

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

PAID_TOOLS = set(dict.fromkeys(op.tool for op in active_operations()))
VALUES: dict[str, Any] = {
    "url": "https://example.com/x.parquet",
    "content_base64": kit.b64(b"a,b\n1,2\n"),
    "artifact_id": "Zz" * 12,
    "path": "/nonexistent/x.csv",
}
# Arguments that are not inputs, for a call with arguments but no input (not a price probe).
NOT_INPUTS: dict[str, Any] = {"filename": "x.csv", "limit": 5, "options": {}}


def named(hint: str) -> set[str]:
    """The parameters a "Set one of: a, b." hint names."""
    assert hint.startswith("Set one of: "), hint
    return {name.strip() for name in hint.removeprefix("Set one of: ").rstrip(".").split(",")}


def calls(tool: Tool) -> tuple[set[str], list[dict[str, Any]]]:
    """A tool's inputs, and calls of it that can only fail: two inputs, or none (when it has
    another argument to send; an unpaid call with none is a price probe)."""
    props = set(tool.input_schema.get("properties", {}))
    takes = props & set(INPUT_KINDS)
    first, second = sorted(takes)[:2]
    extra = {"sql": "SELECT 1"} if "sql" in props else {}
    two = {**extra, first: VALUES[first], second: VALUES[second]}
    none = {k: v for k, v in NOT_INPUTS.items() if k in props}
    return takes, [two, none] if none and not extra else [two]


def hint_of(result: CallToolResult) -> str:
    assert result.is_error, result.content
    body = result.structured_content or {}
    assert "accepts" not in body, "quoted"
    return str(body["error"]["hint"])


async def test_mcp_paid_tools_name_only_their_own_inputs_before_the_quote(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    checked = []
    async with kit.mcp_over_http(paid_app.app) as client:
        for tool in (await client.list_tools()).tools:
            if tool.name not in PAID_TOOLS:
                continue
            takes, cases = calls(tool)
            for args in cases:
                hint = hint_of(await client.call_tool(tool.name, args))
                assert named(hint) == takes, (tool.name, args, hint)
                checked.append(tool.name)
    assert {"convert_to_parquet", "inspect_parquet", "query_parquet"} <= set(checked)
    assert facilitator.verified == []


@pytest.mark.parametrize("local", [False, True], ids=["remote", "local"])
async def test_mcp_tools_name_only_their_own_inputs_when_they_run(
    settings: Settings, local: bool
) -> None:
    # The tool's own refusal (Service._materialize), as a local server, or one without
    # payments, answers it: no gate checked the arguments first.
    service = Service(settings, build_store(settings), local_mode=local)
    server = build_mcp(service, settings, local_mode=local)
    async with mcp.Client(server) as client:
        tools = [t for t in (await client.list_tools()).tools if t.name in PAID_TOOLS]
        for tool in tools:
            takes, cases = calls(tool)
            for args in cases:
                hint = hint_of(await client.call_tool(tool.name, args))
                assert named(hint) == takes, (tool.name, args, hint)
    assert {t.name for t in tools} >= {"convert_to_parquet", "inspect_parquet", "query_parquet"}
    if local:
        assert all("artifact_id" not in t.input_schema["properties"] for t in tools)


async def test_remote_convert_takes_an_artifact_id_as_rest_does(
    paid_app: kit.AppHarness,
) -> None:
    async with kit.mcp_over_http(paid_app.app) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    rest = (await paid_app.http.get("/openapi.json")).json()["components"]["schemas"]
    mcp_inputs = set(tools["convert_to_parquet"].input_schema["properties"]) & set(INPUT_KINDS)
    assert mcp_inputs == set(rest["ConvertBody"]["properties"]) & set(INPUT_KINDS)
    schema = tools["convert_to_parquet"].input_schema["properties"]["artifact_id"]
    assert schema["anyOf"][0]["pattern"] == "^[A-Za-z0-9_-]{22,64}$"


REST_ROUTES = [
    "/v1/convert",
    "/v1/inspect",
    "/v1/preview",
    "/v1/query",
    "/v1/quantum/compress",
    "/v1/quantum/portfolio",
]


@pytest.mark.parametrize("route", REST_ROUTES)
async def test_rest_routes_name_only_their_own_inputs_before_the_quote(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator, route: str
) -> None:
    spec = (await paid_app.http.get("/openapi.json")).json()
    if route not in spec["paths"]:
        pytest.skip(f"{route} is not served here (the quantum extra is not installed)")
    ref = spec["paths"][route]["post"]["requestBody"]["content"]["application/json"]["schema"]
    body_schema = spec["components"]["schemas"][ref["$ref"].rsplit("/", 1)[1]]
    takes = set(body_schema["properties"]) & set(INPUT_KINDS)
    body: dict[str, Any] = {"url": VALUES["url"], "artifact_id": VALUES["artifact_id"]}
    if route == "/v1/query":
        body["sql"] = "SELECT 1"
    r = await paid_app.http.post(route, json=body)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"] == "Provide exactly one input; got 2 (url, artifact_id)."
    assert named(r.json()["error"]["hint"]) == takes
    assert facilitator.verified == []
