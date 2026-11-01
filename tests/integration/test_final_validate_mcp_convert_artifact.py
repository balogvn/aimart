"""RR1 (round-3 review of fix/final-isolate): the remote MCP convert_to_parquet takes an
artifact_id, as REST /v1/convert does and as the "provide exactly one input" hint promises.

Before: the tool's parameters were url, content_base64, filename and options. The SDK dropped an
artifact_id argument, the call was quoted, and the paid retry was verified and then refused
"Provide exactly one input; got 0 (none)." with the hint "Set one of: url, content_base64,
artifact_id.", naming a parameter the tool could not take. Ready-to-drop test for the owner of
mcp/server.py (final-validate); it fails at fix/final-isolate 7940b8b.
"""

from __future__ import annotations

from typing import Any

import pytest

import aimart_testkit as kit

pytestmark = pytest.mark.anyio


async def _stored_artifact(app: kit.AppHarness) -> str:
    r = await app.paying.post(
        "/v1/convert", json={"content_base64": kit.b64(b"a,b\n1,2\n"), "filename": "x.csv"}
    )
    assert r.status_code == 200, r.text
    return str(r.json()["artifact"]["id"])


async def test_remote_convert_lists_artifact_id(paid_app: kit.AppHarness) -> None:
    async with kit.mcp_over_http(paid_app.app) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert "artifact_id" in tools["convert_to_parquet"].input_schema["properties"]


async def test_remote_convert_of_a_stored_artifact_is_paid_and_converts(
    paid_app: kit.AppHarness,
) -> None:
    art = await _stored_artifact(paid_app)
    fake = paid_app.facilitator
    assert fake is not None
    async with kit.mcp_over_http(paid_app.app) as client:
        args: dict[str, Any] = {"artifact_id": art}
        unpaid = await client.call_tool("convert_to_parquet", args)
        assert "accepts" in (unpaid.structured_content or {})
        settled = len(fake.settled)
        meta = await kit.mcp_payment_meta(kit.paying_client(), unpaid.structured_content or {})
        result = await client.call_tool("convert_to_parquet", args, meta=meta)
    assert not result.is_error, result.content[0].text
    out = result.structured_content or {}
    assert out["artifact"]["id"] != art
    assert out["report"]["rows"] == 1
    assert len(fake.settled) == settled + 1


async def test_input_hint_names_only_inputs_the_tool_takes(paid_app: kit.AppHarness) -> None:
    # Two inputs: refused before the quote, and the hint's parameters are all in the schema.
    art = await _stored_artifact(paid_app)
    fake = paid_app.facilitator
    assert fake is not None
    verified = len(fake.verified)
    async with kit.mcp_over_http(paid_app.app) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        params = set(tools["convert_to_parquet"].input_schema["properties"])
        result = await client.call_tool(
            "convert_to_parquet", {"artifact_id": art, "url": "https://example.com/x.csv"}
        )
    assert result.is_error
    assert "accepts" not in (result.structured_content or {})
    text = result.content[0].text
    hint = text.split("Set one of:", 1)[1] if "Set one of:" in text else ""
    named = {p.strip(" .\"'}") for p in hint.split(",") if p.strip(" .\"'}")}
    assert named, text
    assert named <= params, (named - params, text)
    assert len(fake.verified) == verified
