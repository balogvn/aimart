"""A queued call waits for a job slot as long as its payment window allows, not a fixed 10 s.

``queue_wait_s`` (10 s) became the bound on waiting for a slot so that a paid call could not
run its work after its signed authorization expired. But the window (300 s, minus 60 s to
verify and settle, minus one ``fetch_timeout_s`` for the call's downloads) leaves a convert
60 s and an inspect, preview or query 160-165 s at the defaults. With one slot held by a
convert whose URL took 15 s to answer, a paid 3-row CSV convert arriving 1 s later was a
503 busy at 10.0 s, after the facilitator had verified it (verified=2, settled=1); at 07fa450
it waited and was served, 200 and settled, after 14.3 s. A lost sale on a call that would have
settled well inside its window.

Now the wait is what the window leaves the call's own operation (``Settings.queue_wait_for``),
and a wait past that is still a 503 that is not settled. An explicit ``queue_wait_s`` still
sets it (``test_final_validate_queue.py``).
"""

from __future__ import annotations

import io
import time
from typing import TYPE_CHECKING, Any

import anyio
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import aimart_testkit as kit
from aimart_parquet.config import PAYMENT_WINDOW_S, SETTLE_MARGIN_S
from aimart_parquet.payments import Payments

if TYPE_CHECKING:
    from collections.abc import Callable

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

CSV = b"x,y\n1,2\n3,4\n"
_buf = io.BytesIO()
pq.write_table(pa.table({"k": [1, 2], "v": ["a", "b"]}), _buf)
PARQUET = _buf.getvalue()
HOLD_S = 1.5  # how long the first call keeps the only slot


def test_by_default_each_operation_waits_what_its_payment_window_leaves(
    make_settings: Callable[..., Settings],
) -> None:
    s = make_settings()
    assert s.queue_wait_s is None
    left = PAYMENT_WINDOW_S - SETTLE_MARGIN_S - s.fetch_timeout_s
    assert s.queue_wait_for(s.convert_timeout_s) == left - s.convert_timeout_s == 60
    assert s.queue_wait_for(s.query_timeout_s) == 165  # inspect, preview
    assert s.queue_wait_for(s.query_timeout_s + 5) == 160  # query's outer deadline
    assert s.queue_wait_for(s.convert_timeout_s) > 14.3  # the reviewer's queued convert
    Payments(s, [kit.FakeFacilitator()])  # and the startup check accepts the defaults
    assert make_settings(queue_wait_s=3.0).queue_wait_for(s.convert_timeout_s) == 3.0


async def test_a_queued_paid_call_waits_as_long_as_its_operation_may(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Scaled down: a convert's job may run 179 s, which leaves its queue 1 s of the window,
    # while a preview's 15 s job leaves it 165 s. The only slot is held for 1.5 s. Before: both
    # waited the fixed 10 s, so the convert was served, though its window had no room for it.
    settings = make_settings(max_concurrent_jobs=1, convert_timeout_s=179.0)
    assert settings.queue_wait_for(settings.convert_timeout_s) == 1.0
    fake = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitator=fake) as h:
        service = h.app.state.service
        real = service._execute
        release = anyio.Event()

        async def slow(*args: Any, **kwargs: Any) -> Any:
            await release.wait()
            return await real(*args, **kwargs)

        monkeypatch.setattr(service, "_execute", slow)
        results: dict[str, tuple[int, Any, float]] = {}

        async def call(name: str, path: str, body: dict[str, Any]) -> None:
            started = time.monotonic()
            r = await h.paying.post(path, json=body)
            results[name] = (r.status_code, r.json(), time.monotonic() - started)

        convert = {"content_base64": kit.b64(CSV), "filename": "b.csv"}
        async with anyio.create_task_group() as tg:
            tg.start_soon(call, "holder", "/v1/convert", convert)
            while service._limiter.statistics().borrowed_tokens == 0:  # noqa: ASYNC110
                await anyio.sleep(0.01)
            tg.start_soon(call, "convert", "/v1/convert", convert)
            tg.start_soon(call, "preview", "/v1/preview", {"content_base64": kit.b64(PARQUET)})
            await anyio.sleep(HOLD_S)
            release.set()

    status, body, waited = results["convert"]
    assert status == 503, body
    assert body["error"]["code"] == "busy"
    assert body["error"]["hint"] == "Retry in a few seconds; you were not charged."
    assert waited < HOLD_S  # it gave up at its own 1 s, not when the slot came free
    status, body, waited = results["preview"]
    assert status == 200, body  # waited out the 1.5 s hold inside its 165 s
    assert body["rows"] == [[1, "a"], [2, "b"]]
    assert waited >= HOLD_S
    assert results["holder"][0] == 200
    assert len(fake.verified) == 3
    assert len(fake.settled) == 2  # the holder and the preview; never the call that was busy
