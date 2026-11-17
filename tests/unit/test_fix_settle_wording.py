"""What a review found after the lenient settle reading (2026-11-16), one test per finding.

V1: an answer without a boolean ``success`` was a settlement whenever it named any string as a
transaction and none of five error keys, so ``{"status": "reverted", "txHash": ...}``,
``{"ok": false, ...}`` or ``{"transaction": "pending"}`` had unpaid work delivered with a receipt
saying 20000 was paid (the rows are in ``test_fix_settle_reading.py`` and, end to end, in
``integration/test_fix_settle_answers.py``). Here: the transaction id's shape on each network,
the schema-valid "settled with an error" answer, and an amount Python cannot ``int()``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from x402 import SettleResponse
from x402.http import FacilitatorConfig
from x402.schemas import PaymentPayload, PaymentRequirements, ResourceInfo, SupportedResponse

import aimart_testkit as kit
from aimart_parquet.payments import _Answered, _same_units
from aimart_parquet.payments.settle import UNREADABLE, LenientSettleClient, read_settle_answer

pytestmark = pytest.mark.anyio

TX = kit.TX_HASH
SOLANA = kit.SOLANA_DEVNET
# A Solana signature: 64 bytes in base58, 87 or 88 characters.
SIGNATURE = "5" + "VERYreaLsiGnatureHere" * 4 + "abc"


@pytest.mark.parametrize(
    ("transaction", "network", "settled"),
    [
        (TX, kit.BASE_SEPOLIA, True),
        (TX, "eip155:8453", True),
        (SIGNATURE, SOLANA, True),
        (TX, SOLANA, False),  # 0x is not base58
        (SIGNATURE, kit.BASE_SEPOLIA, False),
        ("1" * 88, SOLANA, False),  # the zero signature
        ("pending", SOLANA, False),
        (SIGNATURE + "0", SOLANA, False),  # 0 is not base58
        (TX, "base-sepolia", False),  # no network this service quotes
        (TX, "", False),
    ],
)
def test_without_a_boolean_success_only_a_real_transaction_id_is_a_settlement(
    transaction: str, network: str, *, settled: bool
) -> None:
    answer, reading = read_settle_answer(200, {"transaction": transaction}, network)
    assert (answer.success, reading) == (settled, "lenient" if settled else "unreadable")
    assert answer.transaction == transaction
    if not settled:
        assert answer.error_reason == UNREADABLE


async def test_a_schema_settlement_that_names_an_error_is_flagged_and_its_receipt_is_clean(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The SDK reads {"success": true, "errorReason": ...} as settled, and so does this service
    (withholding it could leave a payer charged for nothing), but the owner gets a WARNING and
    the payer a receipt that does not say both."""
    body = {"success": True, "errorReason": "insufficient_funds", "transaction": TX}
    body |= {"network": kit.BASE_SEPOLIA, "errorMessage": "x"}
    requirements = PaymentRequirements(
        scheme="exact",
        network=kit.BASE_SEPOLIA,
        asset=kit.USDC_BASE_SEPOLIA,
        amount="20000",
        pay_to=kit.PAY_TO_EVM,
        max_timeout_seconds=300,
    )
    client = LenientSettleClient(
        FacilitatorConfig(
            url="https://f.test",
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
            ),
        )
    )
    with caplog.at_level(logging.INFO, logger="aimart.payments.settle"):
        payload = PaymentPayload(
            x402_version=2,
            payload={"signature": "0x00"},
            accepted=requirements,
            resource=ResourceInfo(url="http://test/v1/convert"),
        )
        settled = await client.settle(payload, requirements)
    assert (settled.success, settled.transaction) == (True, TX)
    assert (settled.error_reason, settled.error_message) == (None, None)
    (line,) = [r for r in caplog.records if r.name == "aimart.payments.settle"]
    assert line.levelno == logging.WARNING
    assert "named an error" in line.getMessage()
    assert line.__dict__["reason"] == "insufficient_funds"


class _Stating:
    def __init__(self, amount: str) -> None:
        self.amount = amount

    async def settle(self, payload: Any, requirements: Any) -> SettleResponse:
        return SettleResponse(
            success=True, transaction=TX, network=requirements.network, amount=self.amount
        )


@pytest.mark.parametrize(
    ("stated", "warned"),
    [("1" * 5000, True), ("0" * 4296 + "20000", False), ("0" * 5000, True), ("20000", False)],
)
async def test_an_amount_too_long_for_int_never_fails_a_settled_call(
    stated: str, caplog: pytest.LogCaptureFixture, *, warned: bool
) -> None:
    """``int()`` refuses over 4300 digits: after the money moved that was a 402 (review, low)."""
    requirements = PaymentRequirements(
        scheme="exact",
        network=kit.BASE_SEPOLIA,
        asset=kit.USDC_BASE_SEPOLIA,
        amount="20000",
        pay_to=kit.PAY_TO_EVM,
        max_timeout_seconds=300,
    )
    wrapped = _Answered(_Stating(stated), SupportedResponse(kinds=[]), {})  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger="aimart.payments"):
        settled = await wrapped.settle(object(), requirements)  # type: ignore[arg-type]
    assert (settled.success, settled.amount) == (True, "20000")
    assert any("another amount" in r.getMessage() for r in caplog.records) is warned


def test_same_units_compares_digits_without_converting_them() -> None:
    assert _same_units("020000", "20000")
    assert _same_units("0", "000")
    assert not _same_units("2" + "0" * 5000, "20000")
    assert not _same_units("2e4", "20000")
    assert not _same_units("٢٠٠٠٠", "20000")  # Arabic-Indic digits are not ASCII
