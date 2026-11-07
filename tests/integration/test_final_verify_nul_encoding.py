"""An ``encoding`` with a NUL in it is refused before the quote, typed, on every route.

``codecs.lookup`` raises ValueError, not LookupError, for a name with a NUL ('utf-8\\x00'). The
pre-quote option check (``ConvertOptions.refusal``, run by ``Service.check_options``) handles
only AimartError, so it crashed into the InternalError 500, whose hint says "Retry; if it
persists, report it.", on REST, the upload form and MCP alike: unpaid, but a retry can never
help and nothing said the encoding was the problem. bebfe33 refused it with a 400 from a field
validator; 07fa450 raised the ValueError after the payment was verified.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import aimart_testkit as kit

pytestmark = pytest.mark.anyio

CSV = b"id,amount\n1,1.5\n2,2.5\n"


@pytest.mark.parametrize("encoding", ["utf-8\x00", "x\x00"])
@pytest.mark.parametrize("paying", [False, True], ids=["unpaid", "paid"])
async def test_a_nul_in_the_encoding_is_a_400_before_the_quote(
    encoding: str, paying: bool, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    message = f"Unknown encoding {encoding!r}."
    options = {"encoding": encoding}
    body: dict[str, Any] = {"content_base64": kit.b64(CSV), "filename": "a.csv", "options": options}
    client = paid_app.paying if paying else paid_app.http
    responses = [
        await client.post("/v1/convert", json=body),
        await client.post(
            "/v1/convert/upload",
            data={"options": json.dumps(options)},
            files={"file": ("a.csv", CSV, "text/csv")},
        ),
    ]
    for r in responses:
        assert r.status_code == 400, (
            r.text
        )  # before: 500 internal_error 'Internal error (ValueError).'
        assert "payment-required" not in r.headers
        error = r.json()["error"]
        assert error["code"] == "invalid_request"
        assert error["message"] == message
        assert "Retry" not in error["hint"]
    async with kit.mcp_over_http(paid_app.app) as mcp:
        result = await mcp.call_tool("convert_to_parquet", body)
    assert result.is_error
    structured = result.structured_content or {}
    assert "accepts" not in structured  # not quoted
    assert structured["error"]["message"] == message
    assert facilitator.verified == []
    assert facilitator.settled == []
