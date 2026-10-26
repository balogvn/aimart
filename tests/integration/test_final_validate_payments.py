"""A facilitator outage is the same typed, unpaid error on REST and MCP.

Before: a facilitator that was down when MCP first needed it became a JSON-RPC -32603 protocol
error, where REST answers a typed 503 that says "you were not charged".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import aimart_testkit as kit

if TYPE_CHECKING:
    from collections.abc import Callable

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

A = "Zz" * 12


# ------------------------------------------------------------------------------ facilitator down
async def test_mcp_facilitator_down_is_the_typed_error_rest_sends(
    make_settings: Callable[..., Settings],
) -> None:
    fake = kit.FakeFacilitator(supported_error=RuntimeError("facilitator down"))
    async with kit.running_app(make_settings(), facilitator=fake) as h:
        rest = await h.http.post("/v1/inspect", json={"artifact_id": A})
        async with kit.mcp_over_http(h.app) as client:
            result = await client.call_tool("inspect_parquet", {"artifact_id": A})
            probe = await client.call_tool("inspect_parquet", {})
    assert rest.status_code == 503
    expected = {
        "error": {
            "code": "payments_unavailable",
            "message": "The payment facilitator is unreachable.",
            "hint": "Retry shortly; you were not charged.",
        }
    }
    assert rest.json() == expected
    # Before: MCPError(-32603, 'Internal server error') on the client, no code or hint.
    assert result.is_error
    assert result.structured_content == expected
    assert probe.structured_content == expected
    assert fake.verified == []
