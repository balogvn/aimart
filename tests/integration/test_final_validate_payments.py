"""Payment-side failures a caller can meet before any work: typed, unpaid, and in time.

Before: a facilitator that was down when MCP first needed it became a JSON-RPC -32603 protocol
error, where REST answers a typed 503 that says "you were not charged". And ``queue_wait_s`` was
never read: a paid call waited for a job slot as long as the calls ahead of it took, past the
300 s its signed authorization is valid for, so its work ran and could not settle.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import anyio
import pytest

import aimart_testkit as kit
from aimart_parquet.payments import PAYMENT_WINDOW_S, Payments

if TYPE_CHECKING:
    from collections.abc import Callable

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

A = "Zz" * 12
CSV = b"a,b\n1,2\n3,4\n"


# ------------------------------------------------------------------------------ facilitator down
async def test_mcp_facilitator_down_is_the_typed_error_rest_sends(
    make_settings: Callable[..., Settings],
) -> None:
    fake = kit.FakeFacilitator(supported_error=RuntimeError("facilitator down"))
    async with kit.running_app(make_settings(), facilitator=fake) as h:
        rest = await h.http.post("/v1/inspect", json={"artifact_id": A})
        async with kit.mcp_over_http(h.app) as client:
            result = await client.call_tool("inspect_parquet", {"artifact_id": A})
            probe = await client.call_tool("inspect_parquet", {})
    assert rest.status_code == 503
    expected = {
        "error": {
            "code": "payments_unavailable",
            "message": "The payment facilitator is unreachable.",
            "hint": "Retry shortly; you were not charged.",
        }
    }
    assert rest.json() == expected
    # Before: MCPError(-32603, 'Internal server error') on the client, no code or hint.
    assert result.is_error
    assert result.structured_content == expected
    assert probe.structured_content == expected
    assert fake.verified == []


# ------------------------------------------------------------------------------ queue_wait_s
async def test_a_queued_paid_call_is_busy_after_queue_wait_s_and_not_charged(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(queue_wait_s=0.2, max_concurrent_jobs=1)
    fake = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitator=fake) as h:
        service = h.app.state.service
        real = service._execute
        release = anyio.Event()

        async def slow(*args: Any, **kwargs: Any) -> Any:
            await release.wait()
            return await real(*args, **kwargs)

        monkeypatch.setattr(service, "_execute", slow)
        body = {"content_base64": kit.b64(CSV), "filename": "x.csv"}
        results: list[tuple[int, Any]] = []

        async def call() -> None:
            r = await h.paying.post("/v1/convert", json=body)
            results.append((r.status_code, r.json()))

        async with anyio.create_task_group() as tg:
            tg.start_soon(call)
            while service._limiter.statistics().borrowed_tokens == 0:  # noqa: ASYNC110
                await anyio.sleep(0.01)
            started = time.monotonic()
            with anyio.fail_after(10):
                await call()  # before: waited for the first call, however long it took
            waited = time.monotonic() - started
            release.set()
    assert results[0][0] == 503
    assert results[0][1]["error"]["code"] == "busy"
    assert results[1][0] == 200
    assert waited < 5
    assert len(fake.settled) == 1  # only the call that ran


def test_the_payment_window_fits_the_default_timeouts(settings: Settings) -> None:
    # A verified payment must still settle: the signed authorization is valid for
    # PAYMENT_WINDOW_S, and the queue, the download and the job all happen inside it.
    Payments(settings, [kit.FakeFacilitator()])
    assert PAYMENT_WINDOW_S == 300


@pytest.mark.parametrize(
    "overrides",
    [{"convert_timeout_s": 250.0}, {"queue_wait_s": 200.0}, {"fetch_timeout_s": 230.0}],
)
def test_timeouts_that_outlast_the_payment_window_refuse_to_start(
    overrides: dict[str, float], make_settings: Callable[..., Settings]
) -> None:
    with pytest.raises(ValueError, match="payment authorization"):
        Payments(make_settings(**overrides), [kit.FakeFacilitator()])


def test_routes_and_tools_quote_the_same_payment_window(settings: Settings) -> None:
    payments = Payments(settings, [kit.FakeFacilitator()])
    for config in payments.routes().values():
        assert {option.max_timeout_seconds for option in config.accepts} == {PAYMENT_WINDOW_S}
