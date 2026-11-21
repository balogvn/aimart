"""CDP failing after it has served: Base goes on through xpay, and nobody pays for nothing.

Review of feat/cdp-facilitator (2026-11-17): once CDP had answered GET /supported in a warm
container, any later failure of its verify (a key deleted in the portal: 401/403; 429; 5xx; a
timeout) made every Base paid call a 402, and xpay, listed behind it, was never asked. No
retry round was ever scheduled (every facilitator had answered), and a 401 on /supported
would have kept CDP first for ``KEPT_ROUNDS`` more rounds.

Now a verify that fails (an exception: a non-200, a timeout; not an answer saying the payment
is invalid) falls through to the next facilitator that offers the network, and that one
settles. Verify moves no money; settle never falls through. A refused key keeps CDP out for
the life of the process; any other failure rests it from verify for ``VERIFY_REST_S``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from tests.integration.test_cdp_facilitator import BODY, FakeCdp, cdp_wire, new_secret

import aimart_testkit as kit
from aimart_parquet.payments import VERIFY_REST_S

if TYPE_CHECKING:
    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Any:
    return cdp_wire(monkeypatch)


class Flaky(FakeCdp):
    """The fake CDP, answering ``fail[endpoint]`` (a status, or an exception) once armed."""

    def __init__(self, public: Any, fail: dict[str, Any]) -> None:
        super().__init__(public)
        self.fail = fail
        self.armed = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if self.armed and endpoint in self.fail:
            self.requests.append(request)
            what = self.fail[endpoint]
            if isinstance(what, Exception):
                raise what
            return httpx.Response(what, json={"errorType": "x", "errorMessage": "y"})
        return super().__call__(request)

    def calls(self, endpoint: str) -> int:
        return sum(1 for r in self.requests if r.url.path.endswith(f"/{endpoint}"))


FAILURES = [
    {"verify": 401},
    {"verify": 403},
    {"verify": 429},
    {"verify": 503},
    {"verify": httpx.ReadTimeout("slow")},
    {"supported": 401, "verify": 401},
]
IDS = ["v401", "v403", "v429", "v503", "vtimeout", "revoked-all"]


@pytest.mark.parametrize("fail", FAILURES, ids=IDS)
async def test_a_warm_cdp_that_fails_verify_hands_base_to_xpay(
    fail: dict[str, Any], settings: Settings, wire: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    secret, public = new_secret()
    fake = Flaky(public, fail)
    behind = kit.FakeFacilitator()  # xpay's place
    async with kit.running_app(settings, facilitators=[wire(fake, secret), behind]) as app:
        first = await app.paying.post("/v1/convert", json=BODY)
        assert first.status_code == 200, first.text
        assert (fake.calls("settle"), len(behind.settled)) == (1, 0)
        fake.armed = True
        statuses = []
        for _ in range(3):
            response = await app.paying.post("/v1/convert", json=BODY)
            statuses.append(response.status_code)
            if response.status_code == 200:
                assert kit.payment_response(response)["transaction"] == kit.TX_HASH
        payments = app.payments
        assert payments is not None
        base = payments._clock()
        payments._clock = lambda: base + 10_000  # type: ignore[method-assign]
        for _ in range(3):
            await app.http.get("/v1/pricing")
            response = await app.paying.post("/v1/convert", json=BODY)
            statuses.append(response.status_code)
    # Every call after the failure is sold, through xpay: verified there and settled there.
    assert statuses == [200] * 6
    assert len(behind.settled) == 6
    assert len(behind.verified) == 6
    # CDP settled only the first: settle never goes to a facilitator that did not verify.
    assert fake.calls("settle") == 1
    refused = 401 in fail.values() or 403 in fail.values()
    if refused:
        # A key CDP refused is not offered another verify: one failed request, then none.
        assert fake.calls("verify") == 2
        lines = [r for r in caplog.records if "refused this service's API key" in r.getMessage()]
        assert lines
        assert all("restart" in r.getMessage() for r in lines)
    else:
        # Rested after its failure, then asked again once the rest is over (and failing
        # again, rested again): never once per call.
        assert fake.calls("verify") == 3


async def test_cdp_is_dropped_on_a_refused_key_without_kept_rounds(
    settings: Settings, wire: Any
) -> None:
    """A 401 on GET /supported drops CDP at once: the next quote comes from xpay's answer."""
    secret, public = new_secret()
    fake = Flaky(public, {"supported": 401, "verify": 401})
    behind = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitators=[wire(fake, secret), behind]) as app:
        assert (await app.paying.post("/v1/convert", json=BODY)).status_code == 200
        fake.armed = True
        assert (await app.paying.post("/v1/convert", json=BODY)).status_code == 200
        payments = app.payments
        assert payments is not None
        await app.http.post("/v1/convert", json=BODY)  # a quote: rebuilds without CDP
        assert 0 not in payments._answers
        assert payments._retry_at is None  # nothing left to ask: a refused key is not re-asked
        asked = fake.calls("supported")
        base = payments._clock()
        payments._clock = lambda: base + 10_000  # type: ignore[method-assign]
        assert (await app.paying.post("/v1/convert", json=BODY)).status_code == 200
        assert fake.calls("supported") == asked
    assert len(behind.settled) == 2


async def test_a_settle_failure_never_falls_through(settings: Settings, wire: Any) -> None:
    """CDP verified, then its settle failed: unpaid, withheld, and xpay is not asked to settle."""
    secret, public = new_secret()
    fake = Flaky(public, {"settle": 503})
    fake.armed = True
    behind = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitators=[wire(fake, secret), behind]) as app:
        response = await app.paying.post("/v1/convert", json=BODY)
    assert response.status_code == 402, response.text
    assert (fake.calls("verify"), fake.calls("settle")) == (1, 1)
    assert (behind.verified, behind.settled) == ([], [])


async def test_an_invalid_payment_is_not_asked_again_elsewhere(settings: Settings) -> None:
    """A verify that answers 'invalid' is the payer's answer, not a facilitator failure."""
    front = kit.FakeFacilitator(valid=False)
    behind = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitators=[front, behind]) as app:
        response = await app.paying.post("/v1/convert", json=BODY)
    assert response.status_code == 402
    assert (len(front.verified), behind.verified, behind.settled) == (1, [], [])


async def test_with_nobody_behind_a_verify_failure_is_still_an_unpaid_402(
    settings: Settings,
) -> None:
    front = kit.FakeFacilitator(verify_error=RuntimeError("facilitator 500"))
    async with kit.running_app(settings, facilitators=[front]) as app:
        first = await app.paying.post("/v1/convert", json=BODY)
        second = await app.paying.post("/v1/convert", json=BODY)
    assert (first.status_code, second.status_code) == (402, 402)
    assert front.settled == []
    assert len(front.verified) == 2  # alone, it is never rested: there is nobody else to ask


async def test_fall_through_goes_only_to_a_facilitator_offering_the_network(
    settings: Settings,
) -> None:
    front = kit.FakeFacilitator(verify_error=RuntimeError("facilitator 500"))
    elsewhere = kit.FakeFacilitator(networks=("solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",))
    async with kit.running_app(settings, facilitators=[front, elsewhere]) as app:
        response = await app.paying.post("/v1/convert", json=BODY)
    assert response.status_code == 402
    assert elsewhere.verified == []


async def test_the_settler_is_remembered_per_payment_and_forgotten_after(
    settings: Settings,
) -> None:
    """Two calls in a row: each settles where it was verified, and nothing is left behind."""
    front = kit.FakeFacilitator()
    behind = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitators=[front, behind]) as app:
        front.verify_error = RuntimeError("facilitator 503")
        assert (await app.paying.post("/v1/convert", json=BODY)).status_code == 200
        front.verify_error = None
        payments = app.payments
        assert payments is not None
        base = payments._clock()
        payments._clock = lambda: base + VERIFY_REST_S + 1  # type: ignore[method-assign]
        assert (await app.paying.post("/v1/convert", json=BODY)).status_code == 200
        assert payments._failover._settlers == {}
    assert (len(front.settled), len(behind.settled)) == (1, 1)
    assert (len(front.verified), len(behind.verified)) == (2, 1)


class PendingOnce(kit.FakeFacilitator):
    """Answers its first settle 'settlement_pending' (with the transaction), then settled."""

    async def settle(self, payload: Any, requirements: Any) -> Any:
        from x402.schemas import SettleResponse

        if not self.settled:
            self.settled.append((payload, requirements))
            return SettleResponse(
                success=False,
                error_reason="settlement_pending",
                transaction=kit.TX_HASH,
                network=requirements.network,
            )
        return await super().settle(payload, requirements)


async def test_the_sdks_pending_retry_settles_with_the_same_facilitator(
    settings: Settings,
) -> None:
    """The SDK settles once more on 'settlement_pending': that retry goes where verify went."""
    front = kit.FakeFacilitator(verify_error=RuntimeError("facilitator 503"))
    behind = PendingOnce()
    async with kit.running_app(settings, facilitators=[front, behind]) as app:
        response = await app.paying.post("/v1/convert", json=BODY)
        payments = app.payments
        assert payments is not None
        assert payments._failover._settlers == {}
    assert response.status_code == 200, response.text
    assert (len(front.settled), len(behind.settled)) == (0, 2)


class RejectsPayment(FakeCdp):
    """CDP's own answer to an invalid payment: HTTP 400 with ``X402VerifyPaymentRejection``
    (cdp-sdk 1.53.0, ``x402_facilitator_api.verify_x402_payment``: '400' is that model)."""

    def __init__(self, public: Any) -> None:
        super().__init__(public)
        self.reject = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.reject and request.url.path.endswith("/verify"):
            self.requests.append(request)
            body = {"isValid": False, "invalidReason": "insufficient_funds", "payer": kit.PAYER}
            return httpx.Response(400, json=body)
        return super().__call__(request)


async def test_cdps_400_for_an_invalid_payment_is_the_payers_answer(
    settings: Settings, wire: Any
) -> None:
    """Not a facilitator failure: no fall-through to xpay, and CDP is not rested for it."""
    secret, public = new_secret()
    fake = RejectsPayment(public)
    behind = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitators=[wire(fake, secret), behind]) as app:
        fake.reject = True
        refused = await app.paying.post("/v1/convert", json=BODY)
        fake.reject = False
        sold = await app.paying.post("/v1/convert", json=BODY)
    assert refused.status_code == 402
    assert sold.status_code == 200, sold.text
    assert (behind.verified, behind.settled) == ([], [])
    verifies = [r for r in fake.requests if r.url.path.endswith("/verify")]
    assert len(verifies) == 2
