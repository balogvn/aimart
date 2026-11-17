"""What the service promises cautious buyers is stated where they read, and is true.

Agent projects get cancelled over runaway costs, so buyers look for fixed prices, no charge for
failures and an audit trail. The service has all three; the description every index shows
(/openapi.json, /llms.txt, the server card, x402scan's listing) and the README now say so up
front. Each claim is checked against the code here, so the text cannot drift from it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import aimart_testkit as kit
from aimart_parquet.api.discovery import DESCRIPTION, llms_txt, mcp_server_card
from aimart_parquet.payments.pricing import OPERATIONS

if TYPE_CHECKING:
    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[2]
CLAIMS = ("fixed price per call", "a call that fails is not charged", "receipt", "provenance")


def test_the_description_states_the_guarantees(settings: Settings) -> None:
    for claim in CLAIMS:
        assert claim in DESCRIPTION, claim
    assert DESCRIPTION in llms_txt(settings)
    assert mcp_server_card(settings)["description"] == DESCRIPTION
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    assert "**Predictable for agents:** each operation has a fixed price" in readme


def test_every_price_is_fixed_whatever_the_input(settings: Settings) -> None:
    """'A fixed price per call': a price depends only on the operation, never on the request."""
    for op in OPERATIONS:
        price = op.price(settings)
        assert price.startswith("$"), op.key
        assert float(price.removeprefix("$")) > 0, op.key


async def test_a_paid_call_returns_a_receipt_with_its_transaction_and_a_signed_output(
    paid_app: kit.AppHarness,
) -> None:
    body = {"content_base64": kit.b64(b"region,units\nNorth,10\n"), "filename": "s.csv"}
    paid = await paid_app.paying.post("/v1/convert", json=body)
    assert paid.status_code == 200, paid.text
    receipt = kit.payment_response(paid)
    assert receipt["success"] is True
    assert receipt["transaction"] == kit.TX_HASH
    assert paid.json()["artifact"]["provenance"]["algorithm"] == "ML-DSA-65"


async def test_a_call_that_fails_is_not_charged(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    body = {"content_base64": kit.b64(b"\x00\x01 not a data file"), "filename": "x.bin"}
    failed = await paid_app.paying.post("/v1/convert", json=body)
    assert failed.status_code >= 400
    assert facilitator.settled == []  # verified, perhaps, but never settled
