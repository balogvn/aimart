"""A non-2xx settle answer is an error, unless its JSON object says ``"success": false``.

CDP answers every settle it rejects with HTTP 400 and ``X402SettlePaymentRejection``,
``settlement_pending`` with its transaction included (review of 2026-11-26). Such a body is read
as the refusal it states, so the SDK's pending retry runs and the receipt keeps the transaction;
nothing else in a non-2xx is ever read as a settlement.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from tests.unit.test_fix_settle_reading import PAYLOAD, REQUIREMENTS, TX, N, client_over

from aimart_parquet.payments.settle import LenientSettleClient

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("status", [400, 402, 409, 422, 500])
async def test_a_refusal_stated_in_a_non_2xx_is_returned_as_that_refusal(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    body = {
        "success": False,
        "errorReason": "settlement_pending",
        "errorMessage": "receipt not found yet",
        "transaction": TX,
        "network": N,
    }
    client = client_over(LenientSettleClient, httpx.Response(status, json=body), [])
    with caplog.at_level(logging.INFO):
        settled = await client.settle(PAYLOAD, REQUIREMENTS)
    assert settled.success is False
    assert (settled.error_reason, settled.transaction, settled.network) == (
        "settlement_pending",
        TX,
        N,
    )
    lines = [r for r in caplog.records if r.name == "aimart.payments.settle"]
    assert len(lines) == 1
    assert "named a transaction" in lines[0].getMessage()
    assert (lines[0].status, lines[0].transaction) == (status, TX)  # type: ignore[attr-defined]


async def test_a_stated_refusal_without_a_network_gets_the_quoted_one() -> None:
    body = {"success": False, "errorReason": "insufficient_funds"}
    client = client_over(LenientSettleClient, httpx.Response(400, json=body), [])
    settled = await client.settle(PAYLOAD, REQUIREMENTS)
    assert (settled.success, settled.error_reason, settled.transaction, settled.network) == (
        False,
        "insufficient_funds",
        "",
        N,
    )


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(400, json={"success": True, "transaction": TX}),  # never a settlement
        httpx.Response(400, json={"success": "false", "errorReason": "x"}),  # a JSON false only
        httpx.Response(400, json={"success": None, "errorReason": "x"}),
        httpx.Response(400, json={"errorType": "invalid_request", "errorMessage": "bad"}),
        httpx.Response(402, json={"errorType": "payment_method_required", "errorMessage": "add"}),
        httpx.Response(400, json=[{"success": False}]),
        httpx.Response(400, text="<html>bad request</html>"),
        httpx.Response(502, content=b"\xff\xfe\x00"),
    ],
    ids=["success-true", "string-false", "null", "cdp-error", "cdp-402", "list", "html", "bytes"],
)
async def test_any_other_non_2xx_is_still_an_error(answer: httpx.Response) -> None:
    client = client_over(LenientSettleClient, answer, [])
    with pytest.raises(ValueError, match="Facilitator settle failed"):
        await client.settle(PAYLOAD, REQUIREMENTS)
