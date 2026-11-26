"""CDP's settle failures as CDP sends them, and its verify saying it could not read the chain.

Review of 2026-11-26, before CDP first settles real money (release 0.2.0):

1. CDP answers every settle it rejects with HTTP 400 and ``X402SettlePaymentRejection``
   (cdp-sdk ``openapi.yaml``), ``settlement_pending`` included: a transfer it broadcast and
   whose receipt it could not read yet, with the transaction named. The service raised on any
   non-2xx: the SDK's one retry never ran, the caller was told "failed" while the transfer could
   land, and neither the receipt nor a log line named the transaction. It is now read as the
   refusal it states (``payments/settle.py``).
2. A CDP whose verify worked and whose settle raised (its 402 ``payment_method_required`` once
   the free settlements are used up; a settle-only 429 or 5xx; a timeout) got every later Base
   call, each verified, run and refused at settle, while xpay stood behind it. A settle that
   raised now rests CDP as a verify that raised does, and a 402 drops CDP until the next deploy,
   with an ERROR line naming the fix (``payments/__init__.py``, ``payments/cdp.py``).
3. CDP's verify answers that say it could not read the chain (``invalid_exact_evm_failed_to_
   check_nonce`` and its kin) are its failure, not the payer's: xpay verifies the call instead.

Only the socket is fake: the service's own CDP client, JWT and all, talks to it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from tests.integration.test_cdp_facilitator import BODY, CSV, TX, FakeCdp, cdp_wire, new_secret

import aimart_testkit as kit
from aimart_parquet.payments import VERIFY_REST_S

if TYPE_CHECKING:
    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

ARGS = {"content_base64": kit.b64(CSV), "filename": "sales.csv"}
RECEIPT = "x402/payment-response"
PAYMENT_METHOD = {
    "errorType": "payment_method_required",
    "errorMessage": "A valid payment method is required to complete this operation. Please add "
    "a payment method to your account at https://portal.cdp.coinbase.com.",
}


def rejection(reason: str, transaction: str = "") -> dict[str, Any]:
    """CDP's ``X402SettlePaymentRejection``, the body of its settle 400."""
    return {
        "success": False,
        "errorReason": reason,
        "errorMessage": "The settlement failed.",
        "payer": kit.PAYER,
        "transaction": transaction,
        "network": kit.BASE_SEPOLIA,
    }


class ScriptedCdp(FakeCdp):
    """The fake CDP. Each settle answers the next of ``settles`` (a ``(status, body)`` or an
    exception), then as CDP settles; each verify answers ``verify`` when it is set. The JWT is
    checked first, as CDP checks it."""

    def __init__(
        self, public: Any, settles: list[Any], verify: tuple[int, Any] | None = None
    ) -> None:
        super().__init__(public)
        self.settles = list(settles)
        self.verify = verify

    def __call__(self, request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if self.authorized(request):
            if endpoint == "settle" and self.settles:
                return self._answer(request, self.settles.pop(0))
            if endpoint == "verify" and self.verify is not None:
                return self._answer(request, self.verify)
        return super().__call__(request)

    def _answer(self, request: httpx.Request, what: Any) -> httpx.Response:
        self.requests.append(request)
        if isinstance(what, Exception):
            raise what
        status, body = what
        return httpx.Response(status, json=body)

    def calls(self, endpoint: str) -> int:
        return sum(1 for r in self.requests if r.url.path.endswith(f"/{endpoint}"))


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Any:
    return cdp_wire(monkeypatch)


async def paid_mcp_call(app: kit.AppHarness) -> Any:
    async with kit.mcp_over_http(app.app) as client:
        unpaid = await client.call_tool("convert_to_parquet", ARGS)
        meta = await kit.mcp_payment_meta(kit.paying_client(), unpaid.structured_content)
        return await client.call_tool("convert_to_parquet", ARGS, meta=meta)


def messages(caplog: pytest.LogCaptureFixture, text: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if text in r.getMessage()]


# ----------------------------------------------------------------- 1. CDP's 400 settle answer
@pytest.mark.parametrize("over", ["rest", "mcp"])
async def test_cdps_pending_settlement_is_asked_once_more_at_cdp_and_the_call_is_served(
    over: str, settings: Settings, wire: Any
) -> None:
    """400 ``settlement_pending`` naming the transaction, then settled: the SDK's one retry
    runs, at CDP (which verified it), and the call is delivered with that transaction."""
    secret, public = new_secret()
    fake = ScriptedCdp(public, [(400, rejection("settlement_pending", TX))])
    behind = kit.FakeFacilitator()  # xpay's place
    async with kit.running_app(settings, facilitators=[wire(fake, secret), behind]) as app:
        if over == "rest":
            paid = await app.paying.post("/v1/convert", json=BODY)
            assert paid.status_code == 200, paid.text
            receipt = kit.payment_response(paid)
        else:
            paid_mcp = await paid_mcp_call(app)
            assert not paid_mcp.is_error, paid_mcp.structured_content
            receipt = paid_mcp.meta[RECEIPT]
        payments = app.payments
        assert payments is not None
        assert payments._failover._settlers == {}  # nothing left behind after the retry
    assert (receipt["success"], receipt["transaction"]) == (True, TX)
    assert (fake.calls("verify"), fake.calls("settle")) == (1, 2)
    assert (behind.verified, behind.settled) == ([], [])


@pytest.mark.parametrize("over", ["rest", "mcp"])
async def test_a_rejection_naming_a_transaction_reaches_the_receipt_and_the_owners_log(
    over: str, settings: Settings, wire: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """400 'confirmation timed out' naming the transaction: unpaid, and the hash is not lost.

    It was raised, so the receipt said ``"transaction": ""`` and the only line was
    'facilitator refused to settle' with the status: the owner had nothing to reconcile from.
    """
    caplog.set_level(logging.INFO)
    secret, public = new_secret()
    timed_out = rejection("settle_exact_evm_transaction_confirmation_timed_out", TX)
    fake = ScriptedCdp(public, [(400, timed_out)])
    behind = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitators=[wire(fake, secret), behind]) as app:
        if over == "rest":
            refused = await app.paying.post("/v1/convert", json=BODY)
            assert refused.status_code == 402, refused.text
            receipt = kit.payment_response(refused)
        else:
            refused_mcp = await paid_mcp_call(app)
            assert refused_mcp.is_error
            receipt = refused_mcp.structured_content[RECEIPT]
        sold = await app.paying.post("/v1/convert", json=BODY)
    assert (receipt["success"], receipt["transaction"]) == (False, TX)
    lines = messages(caplog, "named a transaction")
    assert len(lines) == 1
    assert lines[0].levelno == logging.WARNING
    assert (lines[0].transaction, lines[0].status) == (TX, 400)  # type: ignore[attr-defined]
    assert lines[0].reason == timed_out["errorReason"]  # type: ignore[attr-defined]
    # CDP's answer about one payment, not a failure of CDP: it is not rested, and sells the next.
    assert sold.status_code == 200, sold.text
    assert (fake.calls("verify"), fake.calls("settle")) == (2, 2)
    assert (behind.verified, behind.settled) == ([], [])


async def test_a_payers_rejection_without_a_transaction_does_not_move_base(
    settings: Settings, wire: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """400 'insufficient_funds' (the payer spent the USDC after verify): unpaid, one line, and
    CDP keeps Base: a payer must not be able to move the network to xpay."""
    caplog.set_level(logging.INFO)
    secret, public = new_secret()
    fake = ScriptedCdp(public, [(400, rejection("insufficient_funds"))])
    behind = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitators=[wire(fake, secret), behind]) as app:
        refused = await app.paying.post("/v1/convert", json=BODY)
        sold = await app.paying.post("/v1/convert", json=BODY)
    assert refused.status_code == 402
    assert kit.payment_response(refused)["errorReason"] == "insufficient_funds"
    assert sold.status_code == 200, sold.text
    assert (fake.calls("verify"), fake.calls("settle")) == (2, 2)
    assert behind.verified == []
    assert len(messages(caplog, "facilitator refused to settle")) == 1
    assert messages(caplog, "settle failed: the next facilitator") == []


# ------------------------------------------------------- 2. a settle that raised moves Base
RAISED = [
    (503, {"errorType": "service_unavailable", "errorMessage": "Service unavailable."}),
    (429, {"errorType": "rate_limit_exceeded", "errorMessage": "Too many requests."}),
    httpx.ReadTimeout("slow"),
]


@pytest.mark.parametrize("failure", RAISED, ids=["s503", "s429", "stimeout"])
async def test_a_settle_that_failed_rests_cdp_and_xpay_sells_meanwhile(
    failure: Any, settings: Settings, wire: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    secret, public = new_secret()
    fake = ScriptedCdp(public, [failure])
    behind = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitators=[wire(fake, secret), behind]) as app:
        first = await app.paying.post("/v1/convert", json=BODY)
        second = await app.paying.post("/v1/convert", json=BODY)
        payments = app.payments
        assert payments is not None
        base = payments._clock()
        payments._clock = lambda: base + VERIFY_REST_S + 1  # type: ignore[method-assign]
        third = await app.paying.post("/v1/convert", json=BODY)
    # Unpaid, and never settled elsewhere; then xpay sells while CDP rests; then CDP again.
    assert [first.status_code, second.status_code, third.status_code] == [402, 200, 200]
    assert (fake.calls("verify"), fake.calls("settle")) == (2, 2)
    assert (len(behind.verified), len(behind.settled)) == (1, 1)
    lines = messages(caplog, "settle failed: the next facilitator")
    assert len(lines) == 1
    assert lines[0].levelno == logging.WARNING


@pytest.mark.parametrize("over", ["rest", "mcp"])
async def test_cdp_asking_for_a_payment_method_hands_base_to_xpay_until_the_next_deploy(
    over: str, settings: Settings, wire: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """CDP's 402 ``payment_method_required``: the call it verified is unpaid, every call after
    it is sold through xpay, and CDP is not asked again by this process, rest or no rest."""
    caplog.set_level(logging.INFO)
    secret, public = new_secret()
    fake = ScriptedCdp(public, [(402, PAYMENT_METHOD)] * 3)
    behind = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitators=[wire(fake, secret), behind]) as app:
        if over == "rest":
            first = (await app.paying.post("/v1/convert", json=BODY)).status_code
        else:
            first = 402 if (await paid_mcp_call(app)).is_error else 200
        statuses = [first]
        statuses.append((await app.paying.post("/v1/convert", json=BODY)).status_code)
        payments = app.payments
        assert payments is not None
        base = payments._clock()
        payments._clock = lambda: base + 10_000  # type: ignore[method-assign]
        statuses.append((await app.paying.post("/v1/convert", json=BODY)).status_code)
    assert statuses == [402, 200, 200]
    assert (fake.calls("verify"), fake.calls("settle")) == (1, 1)
    assert (len(behind.verified), len(behind.settled)) == (2, 2)
    errors = messages(caplog, "asks for a payment method")
    assert len(errors) == 1
    assert errors[0].levelno == logging.ERROR
    assert "Deploy to Modal" in errors[0].getMessage()


# -------------------------------------------------- 3. CDP could not read the chain at verify
@pytest.mark.parametrize("status", [400, 200])
async def test_cdp_unable_to_read_the_chain_is_its_failure_and_xpay_verifies(
    status: int, settings: Settings, wire: Any
) -> None:
    secret, public = new_secret()
    unread = {
        "isValid": False,
        "invalidReason": "invalid_exact_evm_failed_to_check_nonce",
        "payer": kit.PAYER,
    }
    fake = ScriptedCdp(public, [], verify=(status, unread))
    behind = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitators=[wire(fake, secret), behind]) as app:
        first = await app.paying.post("/v1/convert", json=BODY)
        second = await app.paying.post("/v1/convert", json=BODY)
    assert (first.status_code, second.status_code) == (200, 200)
    assert (fake.calls("verify"), fake.calls("settle")) == (1, 0)  # rested after the first
    assert (len(behind.verified), len(behind.settled)) == (2, 2)


async def test_a_signature_cdp_cannot_parse_is_still_the_payers_answer(
    settings: Settings, wire: Any
) -> None:
    secret, public = new_secret()
    bad = {
        "isValid": False,
        "invalidReason": "invalid_exact_evm_failed_to_parse_signature",
        "payer": kit.PAYER,
    }
    fake = ScriptedCdp(public, [], verify=(400, bad))
    behind = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitators=[wire(fake, secret), behind]) as app:
        refused = await app.paying.post("/v1/convert", json=BODY)
    assert refused.status_code == 402
    assert (behind.verified, behind.settled) == ([], [])
