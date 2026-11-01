"""A paid MCP call naming an argument its tool does not take is refused before the quote.

Incident (round-3 review of fix/final-isolate): the MCP SDK drops argument names a tool does not
declare. convert_to_parquet took no artifact_id then, so {artifact_id, url} was quoted, charged
and converted the url alone, and nothing told the agent its artifact_id was ignored. The same
holds for any tool: inspect_parquet with {artifact_id, content_base64} inspected the artifact
and charged for it, though the agent sent bytes it meant to be read. ``argument_checker`` now
refuses undeclared names (with a value) before the SDK can drop them.
"""

from __future__ import annotations

from typing import Any

import pytest

import aimart_testkit as kit

pytestmark = pytest.mark.anyio

A = "Zz" * 12


def _named(hint: str) -> list[str]:
    return hint.removeprefix("Its parameters are: ").split(";")[0].split(", ")


async def _call(app: kit.AppHarness, tool: str, args: dict[str, Any], *, pay: bool) -> Any:
    async with kit.mcp_over_http(app.app) as client:
        result = await client.call_tool(tool, args)
        if pay and "accepts" in (result.structured_content or {}):
            meta = await kit.mcp_payment_meta(kit.paying_client(), result.structured_content)
            result = await client.call_tool(tool, args, meta=meta)
    return result


@pytest.mark.parametrize("pay", [False, True], ids=["unpaid", "paid"])
@pytest.mark.parametrize(
    ("tool", "args", "unknown"),
    [
        ("inspect_parquet", {"artifact_id": A, "content_base64": "YQ=="}, "content_base64"),
        ("preview_parquet", {"artifact_id": A, "colums": ["a"]}, "colums"),
        ("query_parquet", {"artifact_id": A, "sql": "SELECT 1", "table": {"t": A}}, "table"),
    ],
)
async def test_an_undeclared_argument_is_refused_before_the_quote(
    tool: str,
    args: dict[str, Any],
    unknown: str,
    pay: bool,
    paid_app: kit.AppHarness,
    facilitator: kit.FakeFacilitator,
) -> None:
    result = await _call(paid_app, tool, args, pay=pay)
    assert result.is_error
    error = (result.structured_content or {}).get("error")
    # A PaymentRequired quote has "error": "Payment required"; a refusal has the typed object.
    assert isinstance(error, dict), result.structured_content
    assert error["code"] == "invalid_request"
    assert error["message"] == f"{tool} does not take {unknown}."
    assert error["hint"].startswith("Its parameters are: ")
    assert error["hint"].endswith("; you were not charged.")
    assert unknown not in _named(error["hint"])
    assert facilitator.verified == []
    assert facilitator.settled == []


async def test_the_hint_lists_exactly_the_tools_parameters(paid_app: kit.AppHarness) -> None:
    async with kit.mcp_over_http(paid_app.app) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        result = await client.call_tool("inspect_parquet", {"artifact_id": A, "sheet": "x"})
    error = (result.structured_content or {})["error"]
    assert _named(error["hint"]) == list(tools["inspect_parquet"].input_schema["properties"])


async def test_a_null_undeclared_argument_is_ignored_as_before(paid_app: kit.AppHarness) -> None:
    # A client that sends every name it knows, unset ones as null, asks for nothing extra.
    result = await _call(
        paid_app, "inspect_parquet", {"artifact_id": A, "content_base64": None}, pay=False
    )
    assert "accepts" in (result.structured_content or {})
