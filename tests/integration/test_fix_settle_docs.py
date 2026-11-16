"""docs/payments.md and docs/agents-guide.md on a failed settlement, checked against the wire.

Review of the receipt fix, 2026-11-16: the REST flow stopped at "Rejected payment → 402 with
the reason in PAYMENT-REQUIRED", so a client written from it indexed that header on every 402
and died where the payer may already have been charged: a settlement that fails answers 402
with only PAYMENT-RESPONSE. The MCP table said the receipt of a failed settlement exists but
not where it sits (``structuredContent``, where a successful one is in ``_meta``).

Each test takes a sentence of the documents and compares it with what the real app answers,
through the service's own facilitator client over a mock socket.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

import aimart_testkit as kit

if TYPE_CHECKING:
    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[2]
CSV = b"region,units\nNorth,10\nSouth,20\n"
ARGS = {"content_base64": kit.b64(CSV), "filename": "sales.csv"}
N = kit.BASE_SEPOLIA
TX = kit.TX_HASH
REFUSAL = {"success": False, "errorReason": "insufficient_funds", "transaction": "", "network": N}
RECEIPT_KEY = "x402/payment-response"


def doc(rel: str) -> str:
    """A repository document with line wrapping collapsed, so phrases can span lines."""
    return " ".join((ROOT / rel).read_text(encoding="utf-8").split())


PAYMENTS = doc("docs/payments.md")
GUIDE = doc("docs/agents-guide.md")


def kept(settings: Settings) -> int:
    root = settings.artifact_dir
    return sum(1 for _ in root.rglob("*.parquet")) if root.exists() else 0


async def rest_call(settings: Settings, facilitator: Any) -> httpx.Response:
    async with kit.running_app(settings, facilitators=[facilitator]) as app:
        return await app.paying.post("/v1/convert", json=ARGS)


async def mcp_call(settings: Settings, facilitator: Any) -> Any:
    payer = kit.paying_client()
    async with (
        kit.running_app(settings, facilitators=[facilitator]) as app,
        kit.mcp_over_http(app.app) as client,
    ):
        unpaid = await client.call_tool("convert_to_parquet", ARGS)
        meta = await kit.mcp_payment_meta(payer, unpaid.structured_content)
        return await client.call_tool("convert_to_parquet", ARGS, meta=meta)


def step(number: int) -> str:
    """One numbered step of the REST flow in docs/payments.md."""
    found = re.search(
        rf" {number}\. \*\*(.*?)(?= {number + 1}\. \*\*| A client that has paid)", PAYMENTS
    )
    assert found, f"step {number} of the REST flow is gone"
    return found.group(0)


async def test_rest_a_failed_settlement_is_the_402_the_flow_describes(settings: Settings) -> None:
    text = step(5)
    assert "**Failed settlement**" in text
    assert (
        "`402` with body `{}` and the header **`PAYMENT-RESPONSE`**, and no `PAYMENT-REQUIRED`"
        in text
    )
    assert "The result is withheld and the converted file deleted." in text
    # The receipt printed in the document is the one on the wire, key for key.
    (printed,) = re.findall(r'`(\{"success": false[^`]*\})`', text)
    assert json.loads(printed) == REFUSAL

    facilitator = kit.wire_facilitator(REFUSAL)
    refused = await rest_call(settings, facilitator)
    assert facilitator.settle_calls == 1  # after the work: the facilitator was asked to settle
    assert refused.status_code == 402
    assert refused.json() == {}
    assert "payment-required" not in refused.headers
    assert kit.payment_response(refused) == json.loads(printed)
    assert "artifact" not in refused.text
    assert kept(settings) == 0


async def test_rest_a_named_transaction_is_passed_on_with_what_to_do(settings: Settings) -> None:
    text = step(5)
    assert '`transaction` is `""` unless the facilitator named one' in text
    assert "look that transaction up on chain before paying again" in text
    assert "`errorMessage` says more when there is more to say" in text
    # An answer that names a transaction without saying "settled": withheld, and the id kept.
    refused = await rest_call(settings, kit.wire_facilitator({"txHash": TX, "error": "reverted"}))
    receipt = kit.payment_response(refused)
    assert (refused.status_code, refused.json()) == (402, {})
    assert "payment-required" not in refused.headers
    assert (receipt["success"], receipt["errorReason"], receipt["transaction"]) == (
        False,
        "reverted",
        TX,
    )
    assert "before paying again" in receipt["errorMessage"]
    assert kept(settings) == 0


async def test_rest_refused_unanswered_and_vague_are_the_same_402(settings: Settings) -> None:
    assert (
        "the facilitator refused to settle, did not answer, or answered without saying that it"
        " settled" in step(5)
    )
    answers: list[tuple[Any, int]] = [
        (REFUSAL, 200),
        (httpx.ConnectTimeout("no answer"), 200),
        ({"error": "boom"}, 503),
        ({"status": "ok"}, 200),
    ]
    for body, status in answers:
        refused = await rest_call(settings, kit.wire_facilitator(body, status=status))
        receipt = kit.payment_response(refused)
        assert (refused.status_code, refused.json()) == (402, {}), body
        assert "payment-required" not in refused.headers, body
        assert receipt["success"] is False, body
        assert receipt["errorReason"], body
        assert (receipt["transaction"], receipt["network"]) == ("", N), body
        assert kept(settings) == 0, body


async def test_rest_a_rejected_payment_has_the_other_header_only(settings: Settings) -> None:
    text = step(4)
    assert "**Rejected payment** (before the work" in text
    assert "The reason is in the new `PAYMENT-REQUIRED` header's `error` field" in text
    assert "there is no `PAYMENT-RESPONSE`. Nothing was charged." in text
    facilitator = kit.wire_facilitator(
        REFUSAL, verify={"isValid": False, "invalidReason": "invalid_signature"}
    )
    rejected = await rest_call(settings, facilitator)
    assert (rejected.status_code, rejected.json()) == (402, {})
    assert "payment-response" not in rejected.headers
    assert kit.payment_required(rejected)["error"]
    assert facilitator.settle_calls == 0  # nothing was charged: settle was never asked
    assert kept(settings) == 0


async def test_rest_a_receipt_names_the_quoted_amount_and_an_empty_transaction(
    settings: Settings,
) -> None:
    text = step(3)
    assert "`amount` is the quoted amount, the one the payer signed" in text
    assert "or state another, and the receipt names the quoted one all the same" in text
    assert '`transaction` is `""` when the facilitator names none' in text
    paid = await rest_call(
        settings, kit.wire_facilitator({"success": True, "network": N, "amount": "19999"})
    )
    receipt = kit.payment_response(paid)
    assert paid.status_code == 200
    assert (receipt["success"], receipt["amount"], receipt["transaction"]) == (True, "20000", "")


async def test_the_reading_of_a_settle_answer_is_the_one_described(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    assert (
        'any 2xx answer whose JSON says `"success": true` is a settlement however the rest is'
        " worded (the amount as a number, no `network`, the hash under `txHash` or"
        " `transactionHash`)" in PAYMENTS
    )
    assert (
        "an answer that says neither is a settlement only if it names a transaction and no error;"
        " everything else, and every status that is not 2xx, is not settled" in PAYMENTS
    )
    assert (
        "one log line (`aimart.payments.settle`: the facilitator's URL, the HTTP status, the"
        " transaction)" in PAYMENTS
    )
    outcomes = {
        "worded_TX": (201, {"success": True, "txHash": TX, "amount": 20000}, 200),
        "neither_with_a_TX": (200, {"transactionHash": TX}, 200),
        "neither_with_a_TX_and_an_error": (200, {"transactionHash": TX, "error": "reverted"}, 402),
        "neither": (200, {"status": "ok"}, 402),
        "not_2xx": (500, {"success": True, "transaction": TX, "network": N}, 402),
    }
    for name, (status, body, expected) in outcomes.items():
        caplog.clear()
        with caplog.at_level(logging.INFO):
            answer = await rest_call(settings, kit.wire_facilitator(body, status=status))
        assert answer.status_code == expected, name
        lines = [r for r in caplog.records if r.name == "aimart.payments.settle"]
        assert len(lines) == 1, name
        assert lines[0].facilitator == "https://wire.facilitator.test"  # type: ignore[attr-defined]
        assert lines[0].status == status  # type: ignore[attr-defined]
        if 200 <= status < 300:  # a status that is not 2xx is logged without one
            named = TX if "TX" in name else ""
            assert lines[0].transaction == named, name  # type: ignore[attr-defined]


async def test_mcp_a_failed_settlements_receipt_sits_where_the_table_says(
    settings: Settings,
) -> None:
    (row,) = re.findall(r"\| settlement failure after the tool ran \|(.*?)\| \|", PAYMENTS + " |")
    assert "`isError: true`" in row
    assert "`error` is `Payment settlement failed: <reason>`" in row
    assert (
        '`structuredContent["x402/payment-response"]` (not in `_meta`, where a successful one is)'
        in row
    )
    assert "The same JSON is the first text block" in row
    assert "The tool output is withheld" in row

    refused = await mcp_call(settings, kit.wire_facilitator(REFUSAL))
    assert refused.is_error
    content = refused.structured_content
    assert content["error"] == "Payment settlement failed: insufficient_funds"
    assert content["accepts"]  # a PaymentRequired object
    assert content[RECEIPT_KEY] == REFUSAL
    assert RECEIPT_KEY not in (refused.meta or {})
    assert json.loads(refused.content[0].text) == content
    assert "artifact" not in content
    assert kept(settings) == 0

    named = await mcp_call(settings, kit.wire_facilitator({"txHash": TX, "error": "reverted"}))
    assert named.structured_content[RECEIPT_KEY]["transaction"] == TX

    # ... and a successful one is in _meta, not in structuredContent.
    paid = await mcp_call(
        settings, kit.wire_facilitator({"success": True, "transaction": TX, "network": N})
    )
    assert not paid.is_error
    assert paid.meta[RECEIPT_KEY]["success"] is True
    assert RECEIPT_KEY not in paid.structured_content


async def test_the_agents_guide_says_the_same(settings: Settings) -> None:
    assert (
        "With `PAYMENT-REQUIRED`, the payment was rejected before the work (read its `error`"
        " field): nothing was charged." in GUIDE
    )
    assert (
        "With `PAYMENT-RESPONSE` and no `PAYMENT-REQUIRED`, the work was done but the settlement"
        " failed (read `errorReason`), and the result is withheld" in GUIDE
    )
    assert (
        'an `isError` result with the receipt at `structuredContent["x402/payment-response"]`'
        in GUIDE
    )
    # The wire behind those sentences is pinned above; here, that the guide's two cases are
    # told apart by the header alone.
    failed = await rest_call(settings, kit.wire_facilitator(REFUSAL))
    rejected = await rest_call(
        settings, kit.wire_facilitator(REFUSAL, verify={"isValid": False, "invalidReason": "x"})
    )
    assert {k for k in failed.headers if k.startswith("payment-")} == {"payment-response"}
    assert {k for k in rejected.headers if k.startswith("payment-")} == {"payment-required"}
