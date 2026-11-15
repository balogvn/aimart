"""Which facilitator a network is routed to, and when that may change.

Three findings of the review of the mainnet facilitators (2026-11-15), each reproduced:

1. A retry round moved a network off the facilitator that was serving it: one failed GET
   /supported from that facilitator, while a later one in the list answered, handed the
   network to the later one for the next 300 s. Now a facilitator that answered once keeps its
   last answer, in its place, through ``KEPT_ROUNDS`` failed rounds; after those it is dropped
   until it answers again, so one that died for good stops being quoted.
2. Base could be routed to a facilitator whose only v2 'exact' kind there says
   ``assetTransferMethod: permit2`` (Dexter), while the 402 asks for an EIP-3009
   authorization: a quote nobody was shown to settle. Such a kind is now ignored, and with
   nobody else the answer is the typed, unpaid 503.
3. A price floor that is not a whole number (``"0.78"``) raised between swapping the SDK
   server in and marking it initialized: every paid route a 503, every request a new round.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest

import aimart_parquet.payments as payments_module
import aimart_testkit as kit
from aimart_parquet.payments import Payments, PaymentsUnavailable

if TYPE_CHECKING:
    from collections.abc import Callable

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

PAYEE = "0xa206bb756b0F3F08e8d31BB46e7F7C5145A883Aa"  # the owner's public address, not a key
BASE = kit.BASE_MAINNET
SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
A = "Zz" * 12
ROUND = payments_module.RETRY_SKIPPED_S + 1
# Dexter's v2 'exact' kind on Base, as it listed it on 2026-11-15.
PERMIT2 = {"name": "USD Coin", "version": "2", "assetTransferMethod": "permit2"}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def fake(name: str, *networks: str, **kwargs: Any) -> kit.FakeFacilitator:
    return kit.FakeFacilitator(
        url=f"https://{name}.example", networks=networks or (BASE,), **kwargs
    )


def down() -> RuntimeError:
    return RuntimeError("Facilitator get_supported failed (502): <html>cloudflare</html>")


def route(payments: Payments, network: str = BASE) -> Any:
    return payments.server._facilitator_clients_map[network]["exact"].client


def warnings(caplog: pytest.LogCaptureFixture, message: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "aimart.payments" and message in r.message]


@pytest.fixture
def mainnet(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings(network_mode="mainnet", pay_to_evm=PAYEE, facilitator_urls=None)


@pytest.fixture
def both(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings(
        network_mode="mainnet", pay_to_evm=PAYEE, pay_to_svm=kit.PAY_TO_SVM, facilitator_urls=None
    )


# ------------------------------------------------------------------------------ MR1
async def test_one_failed_supported_in_a_retry_round_does_not_move_the_network(
    mainnet: Settings,
) -> None:
    serving, unneeded, later = fake("serving"), fake("unneeded"), fake("later")
    unneeded.supported_error = down()  # so a retry round runs every RETRY_SKIPPED_S
    payments = Payments(mainnet, [serving, unneeded, later])
    clock = payments._clock = Clock()
    await payments.ensure_initialized()
    assert route(payments) is serving

    clock.now += ROUND
    serving.supported_error = down()  # one 502; its verify and settle are untouched
    await payments.ensure_initialized()
    # Before: `later`, for at least the next 300 s, though `serving` was healthy again.
    assert route(payments) is serving
    assert payments.served == [(BASE, PAYEE)]
    assert (serving.supported_calls, later.supported_calls) == (2, 2)

    serving.supported_error = None
    clock.now += ROUND
    await payments.ensure_initialized()
    assert route(payments) is serving


async def test_two_failing_at_once_do_not_hand_the_network_to_one_that_came_back(
    mainnet: Settings,
) -> None:
    first, second, third = fake("first"), fake("second"), fake("third")
    third.supported_error = down()
    payments = Payments(mainnet, [first, second, third])
    clock = payments._clock = Clock()
    await payments.ensure_initialized()
    clock.now += ROUND
    first.supported_error = second.supported_error = down()
    third.supported_error = None
    await payments.ensure_initialized()
    assert route(payments) is first
    # And when the one in front fails for good the network falls to the next in order, not
    # past it to the last.
    second.supported_error = None
    for _ in range(payments_module.KEPT_ROUNDS):
        clock.now += ROUND
        await payments.ensure_initialized()
    assert route(payments) is second


async def test_a_paid_call_after_such_a_round_is_settled_by_the_same_facilitator(
    settings: Settings,
) -> None:
    # On testnet: the test wallet signs for Base Sepolia only.
    serving, unneeded, later = (fake(name, kit.BASE_SEPOLIA) for name in ("a", "b", "c"))
    unneeded.supported_error = down()
    async with kit.running_app(settings, facilitators=[serving, unneeded, later]) as h:
        assert h.payments is not None
        clock = h.payments._clock = Clock()
        assert (await h.http.post("/v1/inspect", json={"artifact_id": A})).status_code == 402
        clock.now += ROUND
        serving.supported_error = down()
        assert (await h.http.post("/v1/inspect", json={"artifact_id": A})).status_code == 402
        paid = await h.paying.post(
            "/v1/convert", json={"content_base64": kit.b64(b"a,b\n1,2\n"), "filename": "x.csv"}
        )
    assert paid.status_code == 200, paid.text
    assert (len(serving.verified), len(serving.settled)) == (1, 1)
    assert (later.verified, later.settled) == ([], [])


async def test_a_facilitator_silent_for_too_long_is_dropped_and_returns_when_it_answers(
    mainnet: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    serving, later = fake("serving"), fake("later")
    later.supported_error = down()
    payments = Payments(mainnet, [serving, later])
    clock = payments._clock = Clock()
    await payments.ensure_initialized()
    serving.supported_error, later.supported_error = down(), None
    with caplog.at_level(logging.WARNING, logger="aimart.payments"):
        for _ in range(payments_module.KEPT_ROUNDS):
            clock.now += ROUND
            await payments.ensure_initialized()
            assert route(payments) is serving  # kept: /supported can fail while verify works
        clock.now += ROUND
        await payments.ensure_initialized()
    # Before: quoted until the process restarted, because every later round "found less".
    assert route(payments) is later
    (dropped,) = warnings(caplog, "facilitator dropped")
    assert dropped.facilitator == "https://serving.example"  # type: ignore[attr-defined]
    assert "cloudflare" not in caplog.text

    serving.supported_error = None
    clock.now += ROUND
    await payments.ensure_initialized()
    assert route(payments) is serving
    clock.now += ROUND * 10
    await payments.ensure_initialized()  # everybody answered: nobody is asked again
    assert serving.supported_calls == payments_module.KEPT_ROUNDS + 3


async def test_the_only_facilitator_dropped_means_the_typed_unpaid_503_until_it_answers(
    mainnet: Settings,
) -> None:
    only, never = fake("only"), fake("never", supported_error=down())
    async with kit.running_app(mainnet, facilitators=[only, never]) as h:
        assert h.payments is not None
        clock = h.payments._clock = Clock()
        assert (await h.http.post("/v1/inspect", json={"artifact_id": A})).status_code == 402
        only.supported_error = down()
        for _ in range(payments_module.KEPT_ROUNDS):
            clock.now += ROUND
            assert (await h.http.post("/v1/inspect", json={"artifact_id": A})).status_code == 402
        clock.now += ROUND
        refused = await h.http.post("/v1/inspect", json={"artifact_id": A})
        paying = await h.paying.post("/v1/inspect", json={"artifact_id": A})
        assert not h.payments.is_initialized()
        only.supported_error = None
        back = await h.http.post("/v1/inspect", json={"artifact_id": A})
    # A quote for a facilitator that has not answered in 15 minutes is a quote that can only
    # fail: refused before anything is signed.
    assert (refused.status_code, paying.status_code) == (503, 503)
    assert refused.json()["error"]["code"] == "payments_unavailable"
    assert only.verified == []
    assert back.status_code == 402


async def test_a_facilitator_that_stops_listing_the_network_loses_it_at_once(
    mainnet: Settings,
) -> None:
    serving, unneeded, later = fake("serving"), fake("unneeded"), fake("later")
    unneeded.supported_error = down()
    payments = Payments(mainnet, [serving, unneeded, later])
    clock = payments._clock = Clock()
    await payments.ensure_initialized()
    serving.networks = (SOLANA,)  # it answered, and no longer offers Base: that is not a blip
    clock.now += ROUND
    await payments.ensure_initialized()
    assert route(payments) is later


async def test_an_unexpected_failure_in_a_retry_round_is_not_repeated_by_every_request(
    mainnet: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    serving, unneeded = fake("serving"), fake("unneeded", supported_error=down())
    payments = Payments(mainnet, [serving, unneeded])
    clock = payments._clock = Clock()
    await payments.ensure_initialized()
    server = payments.server

    def broken(_: Any) -> Any:
        raise RuntimeError("the SDK refused something")

    monkeypatch.setattr(payments, "_new_server", broken)
    clock.now += ROUND
    with pytest.raises(RuntimeError):
        await payments.ensure_initialized()
    # The round is over for this cool-down; what was serving still serves.
    await payments.ensure_initialized()
    assert serving.supported_calls == 2
    assert payments.is_initialized()
    assert payments.server is server


# ------------------------------------------------------------------------------ MR2
async def test_base_is_never_routed_to_a_facilitator_that_lists_it_as_permit2(
    mainnet: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    xpay, primer = fake("xpay", supported_error=down()), fake("primer", supported_error=down())
    dexter = fake("dexter", BASE, SOLANA, kind_extra={BASE: PERMIT2})
    with caplog.at_level(logging.INFO, logger="aimart.payments"):
        async with (
            kit.running_app(mainnet, facilitators=[xpay, primer, dexter]) as h,
            kit.mcp_over_http(h.app) as client,
        ):
            rest = await h.http.post("/v1/convert", json={})
            paying = await h.paying.post("/v1/inspect", json={"artifact_id": A})
            tool = await client.call_tool("inspect_parquet", {"artifact_id": A})
    # Before: a 402 asking for an EIP-3009 authorization, routed to Dexter, whose kind says
    # Permit2: rejected (unpaid) or, worse, verified and not settled after the work ran.
    assert (rest.status_code, paying.status_code) == (503, 503)
    assert rest.json()["error"]["code"] == "payments_unavailable"
    assert tool.is_error
    assert tool.structured_content == rest.json()
    assert (dexter.verified, dexter.settled) == ([], [])
    # Said once per round, and with nothing to serve every request is one.
    ignored = warnings(caplog, "kind ignored")
    assert {(r.facilitator, r.network, r.method) for r in ignored} == {  # type: ignore[attr-defined]
        ("https://dexter.example", BASE, "permit2")
    }


async def test_with_every_default_answering_dexter_is_not_a_reason_to_ask_again(
    mainnet: Settings,
) -> None:
    xpay, primer = fake("xpay"), fake("primer")
    dexter = fake("dexter", BASE, SOLANA, kind_extra={BASE: PERMIT2})
    payments = Payments(mainnet, [xpay, primer, dexter])
    clock = payments._clock = Clock()
    await payments.ensure_initialized()
    assert route(payments) is xpay
    clock.now += ROUND * 10
    await payments.ensure_initialized()
    assert [f.supported_calls for f in (xpay, primer, dexter)] == [1, 1, 1]


async def test_solana_is_still_dexters_when_base_has_nobody(both: Settings) -> None:
    xpay, primer = fake("xpay", supported_error=down()), fake("primer", supported_error=down())
    dexter = fake("dexter", BASE, SOLANA, kind_extra={BASE: PERMIT2})
    async with kit.running_app(both, facilitators=[xpay, primer, dexter]) as h:
        assert h.payments is not None
        rest = await h.http.post("/v1/convert", json={})
        assert route(h.payments, SOLANA) is dexter
        assert h.payments.served == [(SOLANA, kit.PAY_TO_SVM)]
    assert [a["network"] for a in kit.payment_required(rest)["accepts"]] == [SOLANA]


async def test_a_permit2_listing_in_front_does_not_hide_an_eip3009_one_behind_it(
    mainnet: Settings,
) -> None:
    permit2 = fake("permit2", kind_extra={BASE: PERMIT2})
    explicit = fake("explicit", kind_extra={BASE: {"assetTransferMethod": "eip3009"}})
    async with kit.running_app(mainnet, facilitators=[permit2, explicit]) as h:
        assert h.payments is not None
        rest = await h.http.post("/v1/convert", json={})
        assert route(h.payments) is explicit
    (requirement,) = kit.payment_required(rest)["accepts"]
    # What the payer is asked to sign is what the facilitator behind it says it settles.
    assert "assetTransferMethod" not in requirement["extra"]
    assert (requirement["asset"], requirement["payTo"]) == (kit.USDC_BASE_MAINNET, PAYEE)


# ------------------------------------------------------------------------------ MR3
@pytest.mark.parametrize("floor", ["0.78", "1e3", 0.78, "abc", True, ["783"]], ids=repr)
async def test_a_price_floor_that_is_not_a_whole_number_is_ignored_with_one_warning(
    floor: Any, mainnet: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    odd = fake("odd", kind_extra={BASE: {"minPaymentAmountAtomic": floor}})
    second = fake("second")
    with caplog.at_level(logging.WARNING, logger="aimart.payments"):
        async with kit.running_app(mainnet, facilitators=[odd, second]) as h:
            codes = [
                (await h.http.post("/v1/inspect", json={"artifact_id": A})).status_code
                for _ in range(3)
            ]
            assert h.payments is not None
            assert h.payments.is_initialized()
    # Before: [503, 503, 503], and both facilitators asked again by each request.
    assert codes == [402, 402, 402]
    assert (odd.supported_calls, second.supported_calls) == (1, 1)
    (warning,) = warnings(caplog, "price floor ignored")
    assert warning.facilitator == "https://odd.example"  # type: ignore[attr-defined]
    assert warning.network == BASE  # type: ignore[attr-defined]
    assert not warnings(caplog, "price below facilitator minimum")


@pytest.mark.parametrize("floor", ["5000", 5000])
async def test_a_whole_number_floor_is_still_checked(
    floor: Any, mainnet: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    payments = Payments(
        mainnet, [fake("floor", kind_extra={BASE: {"minPaymentAmountAtomic": floor}})]
    )
    with caplog.at_level(logging.WARNING, logger="aimart.payments"):
        await payments.ensure_initialized()
    below = warnings(caplog, "price below facilitator minimum")
    assert {r.operation for r in below} == {"inspect", "preview"}  # type: ignore[attr-defined]
    assert {r.floor_atomic for r in below} == {5000}  # type: ignore[attr-defined]


async def test_nothing_to_serve_at_the_start_is_still_asked_again_by_the_next_request(
    mainnet: Settings,
) -> None:
    only = fake("only", supported_error=down())
    payments = Payments(mainnet, [only])
    for _ in range(2):
        with pytest.raises(PaymentsUnavailable):
            await payments.ensure_initialized()
    assert only.supported_calls == 2
