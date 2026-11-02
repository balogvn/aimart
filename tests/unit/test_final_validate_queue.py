"""A call waiting for a job slot gives up after ``queue_wait_s``, unpaid.

``queue_wait_s`` was never read: a paid call waited for a job slot as long as the calls ahead
of it took (up to 4 x 180 s), past the 300 s its payment authorization is valid for, so its
work ran and could not settle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio
import pytest

from aimart_parquet.artifacts import build_store
from aimart_parquet.errors import Busy
from aimart_parquet.service import Service

if TYPE_CHECKING:
    from collections.abc import Callable

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio


def make_service(settings: Settings, **kwargs: Any) -> Service:
    return Service(settings, build_store(settings), **kwargs)


# ------------------------------------------------------------------------------ queue_wait_s
async def test_a_queued_call_gives_up_after_queue_wait_s(
    make_settings: Callable[..., Settings],
) -> None:
    service = make_service(make_settings(max_concurrent_jobs=1, queue_wait_s=0.1))
    release = anyio.Event()
    holding = anyio.Event()

    async def hold() -> None:
        async with service._job(1):
            holding.set()
            await release.wait()

    async with anyio.create_task_group() as tg:
        tg.start_soon(hold)
        await holding.wait()
        with anyio.fail_after(5):  # before: waited until the slot was released (never, here)
            with pytest.raises(Busy) as info:
                async with service._job(1):
                    pytest.fail("a job slot was granted while another job held the only one")
        assert info.value.hint == "Retry in a few seconds; you were not charged."
        assert service._limiter.statistics().tasks_waiting == 0  # left the queue
        release.set()
    async with service._job(1):  # the slot is free again, and not leaked by the timeout
        assert service._limiter.statistics().borrowed_tokens == 1


async def test_a_free_slot_is_taken_even_with_no_wait(
    make_settings: Callable[..., Settings],
) -> None:
    service = make_service(make_settings(queue_wait_s=0))
    async with service._job(1):
        assert service._limiter.statistics().borrowed_tokens == 1
    assert service._limiter.statistics().borrowed_tokens == 0


async def test_a_queued_call_still_runs_when_the_slot_frees_in_time(
    make_settings: Callable[..., Settings],
) -> None:
    service = make_service(make_settings(max_concurrent_jobs=1, queue_wait_s=5))
    holding = anyio.Event()
    ran: list[str] = []

    async def hold() -> None:
        async with service._job(1):
            holding.set()
            await anyio.sleep(0.05)

    async def queued() -> None:
        async with service._job(1):
            ran.append("queued")

    async with anyio.create_task_group() as tg:
        tg.start_soon(hold)
        await holding.wait()
        tg.start_soon(queued)
    assert ran == ["queued"]
