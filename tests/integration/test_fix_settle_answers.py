"""A facilitator that settled is believed, however it words its answer.

Review of the receipt fix, 2026-11-16: with the SDK's HTTP client over a mock socket, a
/settle answer of ``{"success": true, ...}`` with the amount as a JSON number, without
``network``, with the hash under ``txHash``, with ``payer`` as an object, with ``extensions``
as a list, or with HTTP 201, was each answered 402 (REST) and ``isError`` (MCP) with the
output deleted: the SDK reads every answer its strict model rejects, and every status but
200, as "not settled", after the facilitator has moved the payer's money.

``Payments.from_settings`` now builds ``payments/settle.LenientSettleClient`` for every
facilitator, and every test here goes through the real app with that client over a mock
socket (``kit.wire_facilitator``). The other half matters as much: a facilitator must not be
able to have work delivered by answering vaguely, so the refusals and the unreadable answers
are pinned beside the settlements.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import httpx
import pytest

import aimart_testkit as kit
from aimart_parquet.payments import Payments
from aimart_parquet.payments.settle import UNREADABLE, LenientSettleClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

CSV = b"region,units\nNorth,10\nSouth,20\n"
BODY = {"content_base64": kit.b64(CSV), "filename": "sales.csv"}
N = kit.BASE_SEPOLIA
TX = kit.TX_HASH
URL = "https://wire.facilitator.test"
# In every body below: it must never reach a log line (a third party's content).
MARK = "BODY-TEXT-THAT-MUST-NOT-BE-LOGGED"

# name -> (HTTP status, /settle body, the transaction the receipt names)
SETTLED: dict[str, tuple[int, Any, str]] = {
    "amount_as_a_json_number": (
        200,
        {"success": True, "transaction": TX, "network": N, "amount": 20000, "note": MARK},
        TX,
    ),
    "network_left_out": (200, {"success": True, "transaction": TX, "note": MARK}, TX),
    "network_null": (200, {"success": True, "transaction": TX, "network": None, "note": MARK}, TX),
    "transaction_left_out": (200, {"success": True, "network": N, "note": MARK}, ""),
    "transaction_null": (
        200,
        {"success": True, "transaction": None, "network": N, "note": MARK},
        "",
    ),
    "transaction_as_txHash": (200, {"success": True, "txHash": TX, "network": N, "note": MARK}, TX),
    "transaction_as_transactionHash": (
        200,
        {"success": True, "transactionHash": TX, "network": N, "note": MARK},
        TX,
    ),
    "payer_as_an_object": (
        200,
        {"success": True, "transaction": TX, "network": N, "payer": {"address": MARK}},
        TX,
    ),
    "extensions_as_a_list": (
        200,
        {"success": True, "transaction": TX, "network": N, "extensions": [MARK], "extra": MARK},
        TX,
    ),
    "http_201": (201, {"success": True, "transaction": TX, "network": N, "note": MARK}, TX),
    "http_202": (202, {"success": True, "transaction": TX, "network": N, "note": MARK}, TX),
    "everything_at_once": (
        201,
        {"success": True, "txHash": TX, "amount": 2e4, "payer": 7, "extensions": MARK},
        TX,
    ),
    # No boolean success, but a transaction id and no error: something was submitted.
    "no_success_but_a_transaction": (200, {"txHash": TX, "status": "confirmed", "note": MARK}, TX),
    "success_as_text_and_a_transaction": (201, {"success": "true", "transaction": TX}, TX),
}

# name -> (HTTP status, /settle body, the errorReason the caller is given)
NOT_SETTLED: dict[str, tuple[int, Any, str]] = {
    # Refusals: the facilitator says no. Terse ones were "invalid data" to the SDK.
    "refusal_terse": (
        200,
        {"success": False, "errorReason": "insufficient_funds"},
        "insufficient_funds",
    ),
    "refusal_snake_case": (200, {"success": False, "error_reason": "nonce_used"}, "nonce_used"),
    "refusal_error_key": (200, {"success": False, "error": "expired", "note": MARK}, "expired"),
    "refusal_error_object": (
        200,
        {"success": False, "error": {"code": "reverted"}},
        '{"code":"reverted"}',
    ),
    "refusal_with_201": (
        201,
        {"success": False, "errorReason": "insufficient_funds", "transaction": "", "network": N},
        "insufficient_funds",
    ),
    # A transaction id beside a refusal is a failed or pending transfer, not a settlement.
    "refusal_as_text_with_a_transaction": (
        200,
        {"success": "false", "transaction": TX, "errorReason": "reverted"},
        "reverted",
    ),
    "refusal_as_zero_with_a_transaction": (
        201,
        {"success": 0, "txHash": TX, "error": "reverted"},
        "reverted",
    ),
    # Vague answers: nothing says "settled", and no transaction id to check.
    "empty_object": (200, {}, UNREADABLE),
    "status_ok_only": (200, {"status": "ok", "note": MARK}, UNREADABLE),
    "success_as_text_without_a_transaction": (200, {"success": "true", "network": N}, UNREADABLE),
    "success_null": (200, {"success": None, "network": N, "note": MARK}, UNREADABLE),
    "transaction_with_an_error": (200, {"transaction": TX, "error": "reverted"}, "reverted"),
    "transaction_with_an_error_message": (
        200,
        {"txHash": TX, "errorMessage": "out of gas"},
        UNREADABLE,
    ),
    "not_json": (200, f"<html>{MARK}</html>", UNREADABLE),
    "empty_body": (200, b"", UNREADABLE),
    "a_json_list": (200, [{"success": True, "transaction": TX}], UNREADABLE),
    "a_json_true": (200, "true", UNREADABLE),
    "a_json_null": (204, "null", UNREADABLE),
}

# name -> (HTTP status, /settle body): not a 2xx, so not settled whatever the body says.
NOT_2XX: dict[str, tuple[int, Any]] = {
    "http_500_claiming_success": (500, {"success": True, "transaction": TX, "network": N}),
    "http_400": (400, {"error": "bad request", "note": MARK}),
    "http_502_page": (502, "<html>bad gateway</html>"),
    "http_302": (302, {"success": True, "transaction": TX, "network": N}),
}


def kept(settings: Settings) -> int:
    root = settings.artifact_dir
    return sum(1 for _ in root.rglob("*.parquet")) if root.exists() else 0


def settle_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "aimart.payments.settle"]


def nothing_of_the_body_was_logged(caplog: pytest.LogCaptureFixture) -> bool:
    return not any(MARK in json.dumps(r.__dict__, default=str) for r in caplog.records)


async def rest_call(settings: Settings, facilitator: Any) -> httpx.Response:
    async with kit.running_app(settings, facilitators=[facilitator]) as app:
        return await app.paying.post("/v1/convert", json=BODY)


async def mcp_call(settings: Settings, facilitator: Any) -> Any:
    payer = kit.paying_client()
    args = {"content_base64": kit.b64(CSV), "filename": "sales.csv"}
    async with (
        kit.running_app(settings, facilitators=[facilitator]) as app,
        kit.mcp_over_http(app.app) as client,
    ):
        unpaid = await client.call_tool("convert_to_parquet", args)
        meta = await kit.mcp_payment_meta(payer, unpaid.structured_content)
        return await client.call_tool("convert_to_parquet", args, meta=meta)


# ------------------------------------------------------------------------------ rule a, c
@pytest.mark.parametrize("name", list(SETTLED))
async def test_rest_delivers_what_a_facilitator_settled(
    name: str, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    status, body, transaction = SETTLED[name]
    facilitator = kit.wire_facilitator(body, status=status)
    with caplog.at_level(logging.INFO):
        paid = await rest_call(settings, facilitator)
    # Before: 402, body {}, the Parquet deleted, and the payer charged.
    assert paid.status_code == 200, (paid.status_code, paid.headers.get("payment-response"))
    assert paid.json()["artifact"]["id"]
    assert kept(settings) == 1
    assert facilitator.settle_calls == 1
    receipt = kit.payment_response(paid)
    assert receipt["success"] is True
    assert receipt["transaction"] == transaction
    assert receipt["network"] == N  # the requirement's, when the answer names none
    assert receipt["amount"] == "20000"
    assert "payer" not in receipt or isinstance(receipt["payer"], str)
    assert "extensions" not in receipt
    # One line for the owner to reconcile from: who, which status, which transaction.
    (line,) = settle_lines(caplog)
    assert line.levelno == logging.WARNING
    assert (line.facilitator, line.status, line.transaction) == (URL, status, transaction)  # type: ignore[attr-defined]
    assert nothing_of_the_body_was_logged(caplog)


@pytest.mark.parametrize("name", list(SETTLED))
async def test_mcp_delivers_what_a_facilitator_settled(
    name: str, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    status, body, transaction = SETTLED[name]
    facilitator = kit.wire_facilitator(body, status=status)
    with caplog.at_level(logging.INFO):
        paid = await mcp_call(settings, facilitator)
    # Before: isError, no result, the Parquet deleted.
    assert not paid.is_error, paid.structured_content
    assert paid.structured_content["artifact"]["id"]
    assert kept(settings) == 1
    assert facilitator.settle_calls == 1
    receipt = paid.meta["x402/payment-response"]
    assert (receipt["success"], receipt["transaction"], receipt["network"]) == (
        True,
        transaction,
        N,
    )
    assert receipt["amount"] == "20000"
    (line,) = settle_lines(caplog)
    assert line.levelno == logging.WARNING
    assert (line.facilitator, line.status, line.transaction) == (URL, status, transaction)  # type: ignore[attr-defined]
    assert nothing_of_the_body_was_logged(caplog)


# ------------------------------------------------------------------------------ rule b, c
@pytest.mark.parametrize("name", list(NOT_SETTLED))
async def test_rest_withholds_what_no_facilitator_said_it_settled(
    name: str, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    status, body, reason = NOT_SETTLED[name]
    facilitator = kit.wire_facilitator(body, status=status)
    with caplog.at_level(logging.INFO):
        refused = await rest_call(settings, facilitator)
    assert refused.status_code == 402
    assert refused.json() == {}  # nothing delivered
    assert kept(settings) == 0  # and nothing kept
    assert "payment-required" not in refused.headers
    receipt = kit.payment_response(refused)
    assert receipt["success"] is False
    assert receipt["errorReason"] == reason
    assert "amount" not in receipt
    assert facilitator.settle_calls == 1
    (line,) = settle_lines(caplog)
    # A refusal read leniently is a WARNING; an answer that says neither is an ERROR.
    assert line.levelno == (logging.WARNING if name.startswith("refusal_") else logging.ERROR)
    assert (line.facilitator, line.status) == (URL, status)  # type: ignore[attr-defined]
    assert nothing_of_the_body_was_logged(caplog)


@pytest.mark.parametrize("name", list(NOT_SETTLED))
async def test_mcp_withholds_what_no_facilitator_said_it_settled(
    name: str, settings: Settings
) -> None:
    status, body, reason = NOT_SETTLED[name]
    facilitator = kit.wire_facilitator(body, status=status)
    refused = await mcp_call(settings, facilitator)
    assert refused.is_error
    assert "artifact" not in refused.structured_content
    assert refused.structured_content["error"] == f"Payment settlement failed: {reason}"
    assert refused.structured_content["x402/payment-response"]["success"] is False
    assert "x402/payment-response" not in (refused.meta or {})
    assert kept(settings) == 0
    assert facilitator.settle_calls == 1


async def test_an_unreadable_answer_names_its_transaction_to_the_owner_and_the_payer(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """A transaction id beside an error: not a settlement, and the one thing both sides can
    look up on chain. It is in the ERROR line and in the receipt, with what to do about it."""
    facilitator = kit.wire_facilitator({"transaction": TX, "error": "reverted", "note": MARK})
    with caplog.at_level(logging.INFO):
        refused = await rest_call(settings, facilitator)
    receipt = kit.payment_response(refused)
    assert (refused.status_code, receipt["transaction"]) == (402, TX)
    assert "before paying again" in receipt["errorMessage"]
    (line,) = settle_lines(caplog)
    assert (line.levelno, line.transaction, line.settled) == (logging.ERROR, TX, False)  # type: ignore[attr-defined]


# ------------------------------------------------------------------------------ rule d
@pytest.mark.parametrize("name", list(NOT_2XX))
async def test_a_status_that_is_not_2xx_is_not_a_settlement(
    name: str, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    status, body = NOT_2XX[name]
    facilitator = kit.wire_facilitator(body, status=status)
    with caplog.at_level(logging.INFO):
        refused = await rest_call(settings, facilitator)
    assert refused.status_code == 402
    assert refused.json() == {}
    assert kept(settings) == 0
    receipt = kit.payment_response(refused)
    assert receipt["success"] is False
    assert receipt["errorReason"].startswith(f"Facilitator settle failed ({status})")
    assert len(receipt["errorReason"]) < 300  # an excerpt, never a whole error page
    (line,) = settle_lines(caplog)
    assert (line.levelno, line.facilitator, line.status) == (logging.WARNING, URL, status)  # type: ignore[attr-defined]
    assert nothing_of_the_body_was_logged(caplog)

    refused_mcp = await mcp_call(settings, kit.wire_facilitator(body, status=status))
    assert refused_mcp.is_error
    assert "artifact" not in refused_mcp.structured_content
    assert kept(settings) == 0


async def test_a_facilitator_that_does_not_answer_is_not_a_settlement(settings: Settings) -> None:
    """Unchanged: a timeout or a dropped connection raises in the client, and both paywalls
    answer it as a failed settlement (the known gap: the transfer may still complete)."""
    facilitator = kit.wire_facilitator(httpx.ReadTimeout("no answer"))
    refused = await rest_call(settings, facilitator)
    assert refused.status_code == 402
    assert kit.payment_response(refused)["success"] is False
    assert kept(settings) == 0
    assert (await mcp_call(settings, kit.wire_facilitator(httpx.ReadTimeout("no answer")))).is_error


# ------------------------------------------------------------------------------ unchanged
@pytest.mark.parametrize(
    "body",
    [
        {"success": True, "transaction": TX, "network": N},  # xpay's shape on mainnet
        {"success": True, "transaction": TX, "network": N, "payer": kit.PAYER, "amount": "20000"},
        {"success": True, "transaction": TX, "network": N, "payer": None, "amount": None},
    ],
)
async def test_an_answer_in_the_sdk_schema_is_read_as_before_and_logs_nothing(
    body: dict[str, Any], settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    facilitator = kit.wire_facilitator(body)
    with caplog.at_level(logging.INFO):
        paid = await rest_call(settings, facilitator)
        paid_mcp = await mcp_call(settings, kit.wire_facilitator(body))
    assert paid.status_code == 200
    expected = {"success": True, "transaction": TX, "network": N, "amount": "20000"}
    if body.get("payer"):
        expected["payer"] = kit.PAYER
    assert kit.payment_response(paid) == expected
    assert paid_mcp.meta["x402/payment-response"] == expected
    assert settle_lines(caplog) == []


async def test_a_refusal_in_the_sdk_schema_is_read_as_before(settings: Settings) -> None:
    body = {"success": False, "errorReason": "insufficient_funds", "transaction": "", "network": N}
    refused = await rest_call(settings, kit.wire_facilitator(body))
    assert refused.status_code == 402
    assert kit.payment_response(refused) == body
    assert kept(settings) == 0


async def test_verify_is_the_sdks_own(settings: Settings) -> None:
    """Only the settle answer is read leniently. An answer to /verify the SDK cannot parse is
    refused before any work is done or anything is settled: there is nothing to lose there."""
    facilitator = kit.wire_facilitator(
        {"success": True, "transaction": TX, "network": N}, verify={"isValid": "perhaps"}
    )
    refused = await rest_call(settings, facilitator)
    assert refused.status_code == 402
    assert "payment-required" in refused.headers  # a quote: nothing was charged
    assert facilitator.settle_calls == 0
    assert kept(settings) == 0
    assert "verify" not in vars(LenientSettleClient)


async def test_every_configured_facilitator_gets_the_lenient_settle_client(
    make_settings: Callable[..., Settings],
) -> None:
    """Mutation check: build the SDK's HTTPFacilitatorClient here and this fails."""
    for overrides in (
        {"facilitator_urls": "https://a.invalid,https://b.invalid"},
        {"network_mode": "mainnet", "facilitator_urls": None},
    ):
        payments = Payments.from_settings(make_settings(**overrides))
        assert payments._facilitators
        assert all(type(c) is LenientSettleClient for c in payments._facilitators)
        await payments.aclose()
