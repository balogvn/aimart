"""One facilitator that cannot answer GET /supported must not take every paid call down.

2026-11-12, preparing the switch to mainnet: the first default facilitator there
(facilitator.heurist.xyz) answered /supported with a 502 and a Cloudflare error page. The SDK's
``initialize()`` asks the configured facilitators in order and stops at the first that raises,
so ``Payments.ensure_initialized()`` failed as a whole (``ValueError: Facilitator get_supported
failed (502)``) although the second one (Dexter) answered: every paid call would have been a
503 and the deploy's smoke test would have failed. And a network no facilitator offered was a
bare 500 on REST (``{"error": "Internal Server Error"}``, the SDK's SchemeNotFoundError).

Now: a facilitator that fails /supported is skipped with one warning; a network nobody can
verify and settle is left out of the quote; when nothing can be served the answer is the typed,
unpaid 503 and the next request asks again; a skipped facilitator is asked again after a
cool-down, never on every request, and never by a request that carries a payment.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

import anyio
import httpx
import pytest
from x402.http.facilitator_client_base import FacilitatorResponseError

import aimart_parquet.payments as payments_module
import aimart_testkit as kit
from aimart_parquet.payments import Payments

if TYPE_CHECKING:
    from collections.abc import Callable

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

A = "Zz" * 12  # well-formed, never issued
CSV = b"a,b\n1,2\n3,4\n"
CONVERT = {"content_base64": kit.b64(CSV), "filename": "x.csv"}
PAGE = "<!DOCTYPE html><html><title>502 Bad Gateway</title>cloudflare</html>"
UNAVAILABLE = {
    "error": {
        "code": "payments_unavailable",
        "message": "The payment facilitator is unreachable.",
        "hint": "Retry shortly; you were not charged.",
    }
}

# What a facilitator's /supported fails with, as the SDK's HTTP client raises each.
FAILURES = [
    ValueError(f"Facilitator get_supported failed (502): {PAGE}"),
    httpx.ReadTimeout("timed out"),
    httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known"),
    FacilitatorResponseError(f"Facilitator supported returned invalid JSON: {PAGE}"),
]


def dead(error: Exception | None = None) -> kit.FakeFacilitator:
    return kit.FakeFacilitator(
        url="https://dead.example", supported_error=error or RuntimeError("down")
    )


def live(**kwargs: Any) -> kit.FakeFacilitator:
    return kit.FakeFacilitator(url="https://live.example", **kwargs)


def skips(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "aimart.payments" and r.levelno == 30]


class Clock:
    """Stands in for ``time.monotonic`` on one ``Payments``: the cool-down without the wait."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ------------------------------------------------------------------------------ INIT a
@pytest.mark.parametrize("failure", FAILURES, ids=lambda e: type(e).__name__)
async def test_a_facilitator_that_fails_supported_is_skipped_and_the_next_one_serves(
    failure: Exception, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    first, second = dead(failure), live()
    with caplog.at_level(logging.WARNING, logger="aimart.payments"):
        async with kit.running_app(settings, facilitators=[first, second]) as h:
            quote = await h.http.post("/v1/inspect", json={"artifact_id": A})
            paid = await h.paying.post("/v1/convert", json=CONVERT)
    # Before: 503 payments_unavailable for both, and nothing ever verified.
    accepts = kit.payment_required(quote)["accepts"]
    assert [(a["network"], a["payTo"], a["amount"]) for a in accepts] == [
        (kit.BASE_SEPOLIA, kit.PAY_TO_EVM, "2000")
    ]
    assert paid.status_code == 200, paid.text
    assert kit.payment_response(paid)["success"] is True
    assert (len(second.verified), len(second.settled)) == (1, 1)
    assert (first.verified, first.settled) == ([], [])
    # One warning, naming the facilitator and the kind of failure, and never the body it sent:
    # that was an HTML page, and logs hold no third party's content.
    (warning,) = skips(caplog)
    assert warning.facilitator == "https://dead.example"  # type: ignore[attr-defined]
    assert warning.error == type(failure).__name__  # type: ignore[attr-defined]
    assert "DOCTYPE" not in caplog.text
    assert "cloudflare" not in caplog.text


async def test_facilitators_serve_in_the_configured_order(settings: Settings) -> None:
    gone, second, third = dead(), live(), kit.FakeFacilitator(url="https://third.example")
    async with kit.running_app(settings, facilitators=[gone, second, third]) as h:
        paid = await h.paying.post("/v1/convert", json=CONVERT)
    assert paid.status_code == 200, paid.text
    assert (len(second.settled), len(third.settled)) == (1, 0)
    assert third.verified == []


async def test_a_facilitator_that_never_answers_is_skipped_after_the_deadline(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The SDK's HTTP client waits 90 s (its timeout for verify and settle too): the first paid
    # request of a cold start would wait that long, and two such hosts pass Modal's 150 s.
    release = threading.Event()

    class Hangs(kit.FakeFacilitator):
        def get_supported(self) -> Any:
            self.supported_calls += 1
            release.wait(20)
            return super().get_supported()

    monkeypatch.setattr(payments_module, "SUPPORTED_TIMEOUT_S", 0.2)
    hung, second = Hangs(url="https://hung.example"), live()
    payments = Payments(settings, [hung, second])
    started = time.monotonic()
    try:
        with caplog.at_level(logging.WARNING, logger="aimart.payments"):
            await payments.ensure_initialized()
        waited = time.monotonic() - started
    finally:
        release.set()
    assert waited < 10
    assert payments.is_initialized()
    assert payments.served == [(kit.BASE_SEPOLIA, kit.PAY_TO_EVM)]
    (warning,) = skips(caplog)
    assert warning.facilitator == "https://hung.example"  # type: ignore[attr-defined]
    assert warning.error == "TimeoutError"  # type: ignore[attr-defined]


# ------------------------------------------------------------------------------ INIT b
async def test_a_network_no_facilitator_offers_is_left_out_of_the_quote(
    make_settings: Callable[..., Settings], caplog: pytest.LogCaptureFixture
) -> None:
    # Two payees, so two accepted networks; the facilitator that would settle Solana is down.
    settings = make_settings(pay_to_svm=kit.PAY_TO_SVM)
    solana = kit.FakeFacilitator(
        url="https://dead.example",
        networks=(kit.SOLANA_DEVNET,),
        supported_error=RuntimeError("down"),
    )
    base = live(networks=(kit.BASE_SEPOLIA,))
    with caplog.at_level(logging.WARNING, logger="aimart.payments"):
        async with kit.running_app(settings, facilitators=[solana, base]) as h:
            rest = await h.http.post("/v1/inspect", json={"artifact_id": A})
            async with kit.mcp_over_http(h.app) as client:
                tool = await client.call_tool("inspect_parquet", {"artifact_id": A})
            paid = await h.paying.post("/v1/convert", json=CONVERT)
    # Before (with only `base` configured): {"error": "Internal Server Error"}, a bare 500.
    assert [a["network"] for a in kit.payment_required(rest)["accepts"]] == [kit.BASE_SEPOLIA]
    assert tool.is_error
    assert tool.structured_content is not None
    assert [a["network"] for a in tool.structured_content["accepts"]] == [kit.BASE_SEPOLIA]
    assert paid.status_code == 200, paid.text
    assert len(base.settled) == 1
    left_out = [r for r in skips(caplog) if getattr(r, "network", None) == kit.SOLANA_DEVNET]
    assert len(left_out) == 1


@pytest.mark.parametrize(
    "facilitators",
    [
        lambda: [dead(), dead(httpx.ConnectError("refused"))],
        # Answers, but offers nothing the service accepts: another network, or only x402 v1.
        lambda: [live(networks=(kit.SOLANA_DEVNET,))],
        lambda: [live(x402_version=1)],
        # The SDK gives a (network, scheme) to the first facilitator that lists it, in any
        # version: a later v2 offer is not used, so the network must not be quoted.
        lambda: [live(x402_version=1), kit.FakeFacilitator(url="https://third.example")],
    ],
    ids=["all-dead", "other-network", "v1-only", "v1-first"],
)
async def test_nothing_to_serve_is_the_typed_unpaid_503_on_rest_and_mcp(
    facilitators: Callable[[], list[kit.FakeFacilitator]], settings: Settings
) -> None:
    fakes = facilitators()
    async with kit.running_app(settings, facilitators=fakes) as h:
        rest = await h.http.post("/v1/convert", json=CONVERT)
        paying = await h.paying.post("/v1/convert", json=CONVERT)
        async with kit.mcp_over_http(h.app) as client:
            tool = await client.call_tool("inspect_parquet", {"artifact_id": A})
            probe = await client.call_tool("inspect_parquet", {})
    for response in (rest, paying):
        assert response.status_code == 503
        assert response.json() == UNAVAILABLE
        assert "payment-required" not in response.headers
    assert tool.is_error
    assert tool.structured_content == UNAVAILABLE
    assert probe.structured_content == UNAVAILABLE
    assert all(f.verified == [] and f.settled == [] for f in fakes)


async def test_after_a_503_the_next_request_asks_again_and_is_served(settings: Settings) -> None:
    first, second = dead(), dead()
    async with kit.running_app(settings, facilitators=[first, second]) as h:
        assert (await h.http.post("/v1/inspect", json={"artifact_id": A})).status_code == 503
        second.supported_error = None
        quote = await h.http.post("/v1/inspect", json={"artifact_id": A})
        paid = await h.paying.post("/v1/convert", json=CONVERT)
    assert kit.payment_required(quote)["accepts"][0]["network"] == kit.BASE_SEPOLIA
    assert paid.status_code == 200, paid.text
    assert len(second.settled) == 1
    assert (first.supported_calls, second.supported_calls) == (2, 2)


async def test_requests_that_waited_behind_a_failed_attempt_do_not_each_ask_again(
    settings: Settings,
) -> None:
    # Every waiter repeated the round of GETs as it got the lock: N requests, N rounds, each
    # holding the others up, against hosts that had just failed.
    only = dead()
    statuses: list[int] = []
    async with kit.running_app(settings, facilitators=[only]) as h:

        async def hit() -> None:
            r = await h.http.post("/v1/inspect", json={"artifact_id": A})
            statuses.append(r.status_code)

        async with anyio.create_task_group() as tg:
            for _ in range(8):
                tg.start_soon(hit)
        assert statuses == [503] * 8
        assert only.supported_calls == 1
        await hit()  # a request that arrives afterwards does ask
        assert only.supported_calls == 2


# ------------------------------------------------------------------------------ INIT c
async def test_a_skipped_facilitator_is_asked_again_after_the_cool_down_only(
    settings: Settings,
) -> None:
    preferred, fallback = dead(), live()
    async with kit.running_app(settings, facilitators=[preferred, fallback]) as h:
        assert h.payments is not None
        clock = h.payments._clock = Clock()
        for _ in range(3):
            r = await h.http.post("/v1/inspect", json={"artifact_id": A})
            assert r.status_code == 402
        # Not on every request: the dead host was asked once.
        assert (preferred.supported_calls, fallback.supported_calls) == (1, 1)

        preferred.supported_error = None  # it is back
        clock.now += payments_module.RETRY_SKIPPED_S - 1
        await h.http.post("/v1/inspect", json={"artifact_id": A})
        assert preferred.supported_calls == 1

        clock.now += 2
        # A request that carries a payment never waits for the round of GETs: its
        # authorization is already running out.
        r = await h.http.post(
            "/v1/inspect", json={"artifact_id": A}, headers={"PAYMENT-SIGNATURE": "bm90IGpzb24="}
        )
        assert r.status_code in {400, 402}
        assert preferred.supported_calls == 1
        # The next quote does, once, and the preferred facilitator serves again.
        r = await h.http.post("/v1/inspect", json={"artifact_id": A})
        assert r.status_code == 402
        assert (preferred.supported_calls, fallback.supported_calls) == (2, 2)
        paid = await h.paying.post("/v1/convert", json=CONVERT)
        clock.now += payments_module.RETRY_SKIPPED_S * 10
        await h.http.post("/v1/inspect", json={"artifact_id": A})
        # Nothing is skipped any more: nobody is asked again.
        assert (preferred.supported_calls, fallback.supported_calls) == (2, 2)
    assert paid.status_code == 200, paid.text
    assert (len(preferred.settled), len(fallback.settled)) == (1, 0)


async def test_a_retry_that_finds_less_keeps_what_was_working(settings: Settings) -> None:
    preferred, fallback = dead(), live()
    payments = Payments(settings, [preferred, fallback])
    clock = payments._clock = Clock()
    await payments.ensure_initialized()
    server = payments.server
    fallback.supported_error = RuntimeError("blip")  # its /supported fails; verify may not
    clock.now += payments_module.RETRY_SKIPPED_S + 1
    await payments.ensure_initialized()  # before the fix this path did not exist
    assert payments.is_initialized()
    assert payments.server is server
    assert payments.served == [(kit.BASE_SEPOLIA, kit.PAY_TO_EVM)]
    assert fallback.supported_calls == 2
    await payments.ensure_initialized()  # and the failed retry starts a new cool-down
    assert fallback.supported_calls == 2


async def test_mcp_asks_a_skipped_facilitator_again_but_not_with_a_payment(
    settings: Settings,
) -> None:
    preferred, fallback = dead(), live()
    async with (
        kit.running_app(settings, facilitators=[preferred, fallback]) as h,
        kit.mcp_over_http(h.app) as client,
    ):
        assert h.payments is not None
        clock = h.payments._clock = Clock()
        unpaid = await client.call_tool("inspect_parquet", {"artifact_id": A})
        assert unpaid.structured_content is not None
        meta = await kit.mcp_payment_meta(kit.paying_client(), unpaid.structured_content)
        preferred.supported_error = None
        clock.now += payments_module.RETRY_SKIPPED_S + 1
        paid = await client.call_tool("inspect_parquet", {"artifact_id": A}, meta=meta)
        # The unknown artifact fails after a verified payment: verified by the fallback, which
        # the quote came from, with no round of GETs in between.
        assert paid.is_error
        assert (len(fallback.verified), fallback.settled) == (1, [])
        assert preferred.supported_calls == 1
        await client.call_tool("inspect_parquet", {})  # a price probe
        assert preferred.supported_calls == 2


async def test_nothing_skipped_means_nobody_is_asked_again(settings: Settings) -> None:
    only = live()
    payments = Payments(settings, [only])
    clock = payments._clock = Clock()
    await payments.ensure_initialized()
    clock.now += payments_module.RETRY_SKIPPED_S * 100
    await payments.ensure_initialized()
    assert only.supported_calls == 1


# ------------------------------------------------------------------------------ INIT e
async def test_price_floors_are_checked_for_the_facilitators_that_answered(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    floor = live(kind_extra={kit.BASE_SEPOLIA: {"minPaymentAmountAtomic": "5000"}})
    payments = Payments(settings, [dead(), floor])
    with caplog.at_level(logging.ERROR, logger="aimart.payments"):
        await payments.ensure_initialized()
    below = {r.operation: r for r in caplog.records if r.levelno >= 40}  # type: ignore[attr-defined]
    assert set(below) == {"inspect", "preview"}
    assert below["inspect"].floor_atomic == 5000  # type: ignore[attr-defined]
    assert below["inspect"].network == kit.BASE_SEPOLIA  # type: ignore[attr-defined]
