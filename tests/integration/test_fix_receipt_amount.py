"""A receipt always states the amount that was settled.

The first paid calls on mainnet (2026-11-15) settled and the money arrived, and their receipts
had no ``amount``: xpay's settle response leaves the field out (x402 v2 makes it optional), the
service passed the receipt on as it came, and this repository's own example client crashed
reading it (``KeyError: 'amount'``) after the payer had been charged. docs/payments.md shows a
receipt with the amount and says 'exact' settles exactly the quoted one, so the service now
states it when the facilitator does not, on REST and on MCP; a failed settlement gets none.
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import httpx2
import pytest
from mcp.client.streamable_http import streamable_http_client
from x402 import SettleResponse, SupportedResponse

import aimart_testkit as kit
from aimart_parquet.payments import _Answered

if TYPE_CHECKING:
    from types import ModuleType

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[2]
CSV = b"region,units\nNorth,10\nSouth,20\n"
BODY = {"content_base64": kit.b64(CSV), "filename": "sales.csv"}


@dataclass
class TerseFacilitator(kit.FakeFacilitator):
    """Settles the way xpay answered on mainnet: success, transaction, network; no amount."""

    stated_amount: str | None = None  # what its settle response says was settled, if anything

    async def settle(self, payload: Any, requirements: Any) -> SettleResponse:
        self.settled.append((payload, requirements))
        return SettleResponse(
            success=True,
            transaction=kit.TX_HASH,
            network=requirements.network,
            amount=self.stated_amount,
        )


def load_example() -> ModuleType:
    path = ROOT / "docs" / "examples" / "agent_pays.py"
    spec = importlib.util.spec_from_file_location("agent_pays_example", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_a_rest_receipt_states_the_amount_the_facilitator_left_out(
    settings: Settings,
) -> None:
    async with kit.running_app(settings, facilitator=TerseFacilitator()) as app:
        quote = kit.payment_required(await app.http.post("/v1/convert", json=BODY))
        paid = await app.paying.post("/v1/convert", json=BODY)
    assert paid.status_code == 200, paid.text
    receipt = kit.payment_response(paid)
    assert receipt["success"] is True
    assert receipt["transaction"] == kit.TX_HASH
    # Before: no "amount" key at all, as on mainnet.
    assert receipt["amount"] == quote["accepts"][0]["amount"] == "20000"


async def test_an_mcp_receipt_states_it_too(settings: Settings) -> None:
    payer = kit.paying_client()
    args = {"content_base64": kit.b64(CSV), "filename": "sales.csv"}
    async with (
        kit.running_app(settings, facilitator=TerseFacilitator()) as app,
        kit.mcp_over_http(app.app) as client,
    ):
        unpaid = await client.call_tool("convert_to_parquet", args)
        meta = await kit.mcp_payment_meta(payer, unpaid.structured_content)
        paid = await client.call_tool("convert_to_parquet", args, meta=meta)
    assert not paid.is_error, paid.structured_content
    receipt = paid.meta["x402/payment-response"]
    assert receipt["success"] is True
    assert receipt["amount"] == unpaid.structured_content["accepts"][0]["amount"] == "20000"


# Was test_an_amount_the_facilitator_states_is_passed_on_as_it_is, which pinned a stated
# "19999" reaching the payer as the receipt's amount ("what a facilitator reports is its to
# report"). Review of 2026-11-16 (S3): under 'exact' the payer signed a transfer of exactly the
# quoted amount, and a settlement that succeeded moved that and nothing else, so a receipt
# saying 19999, "", "2e4" or "20000.5" misstates what was paid. The receipt now always names
# the quoted amount, and a facilitator that states another one is logged, not repeated.
@pytest.mark.parametrize(
    ("stated", "warned"),
    [
        ("19999", True),  # different
        ("20001", True),
        ("20000.5", True),  # not a whole number of atomic units
        ("2e4", True),
        ("0.02", True),  # dollars, not atomic units
        ("-20000", True),
        ("twenty thousand", True),
        ("", False),  # empty: nothing was stated
        ("   ", False),
        ("20000", False),  # the quote itself
        (" 20000 ", False),  # the same number, written otherwise
        ("020000", False),
    ],
)
async def test_an_exact_receipt_names_the_quoted_amount_whatever_the_facilitator_states(
    stated: str, warned: bool, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    payer = kit.paying_client()
    args = {"content_base64": kit.b64(CSV), "filename": "sales.csv"}
    with caplog.at_level(logging.INFO):
        async with kit.running_app(
            settings, facilitator=TerseFacilitator(stated_amount=stated)
        ) as app:
            paid = await app.paying.post("/v1/convert", json=BODY)
            async with kit.mcp_over_http(app.app) as client:
                unpaid = await client.call_tool("convert_to_parquet", args)
                meta = await kit.mcp_payment_meta(payer, unpaid.structured_content)
                paid_mcp = await client.call_tool("convert_to_parquet", args, meta=meta)
    assert paid.status_code == 200, paid.text
    assert kit.payment_response(paid)["amount"] == "20000"
    assert not paid_mcp.is_error
    assert paid_mcp.meta["x402/payment-response"]["amount"] == "20000"
    lines = [r for r in caplog.records if "another amount" in r.getMessage()]
    # One line per settlement (REST, then MCP) for the owner: who said it, and both amounts.
    assert len(lines) == (2 if warned else 0)
    for line in lines:
        assert line.levelno == logging.WARNING
        assert (line.facilitator, line.stated_amount, line.quoted_amount) == (  # type: ignore[attr-defined]
            "https://facilitator.test",
            stated,
            "20000",
        )


@pytest.mark.parametrize("stated", [19999, 20000.5, "19999", ""])
async def test_an_amount_read_from_the_wire_is_replaced_the_same_way(
    stated: Any, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """The same through the service's own client over a mock socket: a JSON number is read
    leniently (payments/settle.py) and then replaced like any other stated amount."""
    answer = {"success": True, "transaction": kit.TX_HASH, "network": kit.BASE_SEPOLIA}
    facilitator = kit.wire_facilitator({**answer, "amount": stated})
    with caplog.at_level(logging.INFO):
        async with kit.running_app(settings, facilitators=[facilitator]) as app:
            paid = await app.paying.post("/v1/convert", json=BODY)
    assert paid.status_code == 200
    assert kit.payment_response(paid)["amount"] == "20000"
    lines = [r for r in caplog.records if "another amount" in r.getMessage()]
    assert [(r.facilitator, r.stated_amount, r.quoted_amount) for r in lines] == (  # type: ignore[attr-defined]
        [("https://wire.facilitator.test", str(stated), "20000")] if stated != "" else []
    )


async def test_a_failed_settlement_is_given_no_amount(settings: Settings) -> None:
    async with kit.running_app(settings, facilitator=kit.FakeFacilitator(settle_ok=False)) as app:
        refused = await app.paying.post("/v1/convert", json=BODY)
    assert refused.status_code == 402  # not settled: the caller keeps its money
    header = refused.headers.get("payment-response")
    if header:  # the SDK may or may not attach the failed settlement
        assert "amount" not in kit.decode_b64_json(header)


async def test_the_example_client_finishes_against_a_facilitator_that_states_no_amount(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """What the owner ran on mainnet: it paid, then died printing the receipt."""
    example = load_example()
    async with kit.running_app(settings, facilitator=TerseFacilitator()) as h:
        payer = example.make_payer(kit.PAYER_KEY, network=kit.BASE_SEPOLIA, max_usd=Decimal("0.02"))
        converted = await example.rest_flow(
            kit.BASE_URL, payer, CSV, "sales.csv", transport=httpx.ASGITransport(app=h.app)
        )
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=h.app), base_url=kit.BASE_URL
        ) as http_client:
            await example.mcp_flow(
                f"{kit.BASE_URL}/mcp",
                payer,
                converted["artifact"]["id"],
                transport=streamable_http_client(f"{kit.BASE_URL}/mcp", http_client=http_client),
            )
    printed = capsys.readouterr().out
    assert f"paid 20000 in tx {kit.TX_HASH}" in printed
    assert "paid 5000" in printed  # the MCP query


@pytest.mark.parametrize(
    ("receipt", "expected"),
    [
        ({"success": True, "transaction": "0xabc"}, "20000"),  # another service's terse receipt
        ({"success": True, "transaction": "0xabc", "amount": None}, "20000"),
        ({"success": True, "transaction": "0xabc", "amount": "19999"}, "19999"),
    ],
)
def test_the_example_reads_a_receipt_without_an_amount(
    receipt: dict[str, Any], expected: str
) -> None:
    """The example must not depend on this service's fill-in: other x402 services pass
    their facilitator's receipt on untouched."""
    assert load_example().settled_amount(receipt, "20000") == expected


class _NoAmount:
    """A requirement as x402 v1 shapes it: a scheme, and no ``amount`` attribute."""

    scheme = "exact"
    network = kit.BASE_SEPOLIA


async def test_filling_in_the_amount_can_never_fail_a_call_that_was_settled() -> None:
    """By the time a receipt is completed the payer's money has moved: an exception here would
    be a 500 for a call that was charged. A requirement without an amount leaves the receipt
    as the facilitator gave it."""
    wrapped = _Answered(TerseFacilitator(), SupportedResponse(kinds=[]), {})
    settled = await wrapped.settle(object(), _NoAmount())  # type: ignore[arg-type]
    assert settled.success is True
    assert settled.amount is None
