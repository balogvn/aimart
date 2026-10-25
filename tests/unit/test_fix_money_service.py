"""Service fixes: caller Parquet is decoded in the isolated worker (S1), and URL downloads share
one byte budget per call and, like uploads, wait for a job slot before they start (S3)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio
import pytest

import aimart_testkit as kit
from aimart_parquet import service as service_module
from aimart_parquet.artifacts import build_store
from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.errors import Busy, InputTooLarge
from aimart_parquet.service import Service, SourceRef
from aimart_parquet.sources import Fetched, _too_large

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

CSV = b"region,units\nNorth,10\nSouth,20\nEast,30\n"


def make_service(settings: Settings) -> Service:
    return Service(settings, build_store(settings))


def parquet_bytes(tmp_path: Path) -> bytes:
    return kit.write_parquet(tmp_path / "t.parquet", {"a": [1, 2, 3]}).read_bytes()


# ------------------------------------------------------------------------------ S1
async def test_preview_and_inspect_decode_in_the_isolated_worker(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Before: preview (and inspect) ran with isolate=False, so a 258-byte Parquet dictionary
    # bomb decoded inside the API process and took the only instance over its memory limit.
    ran: list[str] = []
    caps: list[int | None] = []

    def recording_run_isolated(
        fn: Callable[..., Any],
        *args: Any,
        timeout_s: float,
        memory_limit_mb: int | None = None,
        **kw: Any,
    ) -> Any:
        ran.append(fn.__name__)
        caps.append(memory_limit_mb)
        return fn(*args, **kw)

    monkeypatch.setattr(service_module, "run_isolated", recording_run_isolated)
    service = make_service(make_settings(isolate_jobs=True, job_memory_limit_mb=640))
    art = (
        await service.convert(
            SourceRef(content_base64=kit.b64(CSV), filename="s.csv"), ConvertOptions()
        )
    ).artifact
    src = SourceRef(artifact_id=art.id)
    preview = await service.preview(src, columns=None, offset=0, limit=2, fmt="json")
    inspected = await service.inspect(src)
    queried = await service.query({"data": src}, "SELECT count(*) FROM data", limit=5, fmt="json")
    assert ran == ["convert_file", "preview", "inspect_parquet", "query"]
    assert caps == [640] * 4  # the instance's own setting, not the 700 MB field default
    assert preview.rows == [["North", 10], ["South", 20]]
    assert inspected.rows == 3
    assert queried.rows == [[3]]


async def test_isolated_preview_and_inspect_round_trip(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    # The real forkserver child: results (and typed errors) cross the process boundary.
    service = make_service(make_settings(isolate_jobs=True))
    src = SourceRef(content_base64=kit.b64(parquet_bytes(tmp_path)), filename="t.parquet")
    preview = await service.preview(src, columns=["a"], offset=1, limit=5, fmt="csv")
    assert preview.text == "a\n2\n3\n"
    assert (await service.inspect(src)).rows == 3
    from aimart_parquet.errors import InvalidInput, InvalidRequest

    with pytest.raises(InvalidRequest, match="Unknown columns"):
        await service.preview(src, columns=["nope"], offset=0, limit=1, fmt="json")
    junk = SourceRef(content_base64=kit.b64(b"not parquet"), filename="x.parquet")
    with pytest.raises(InvalidInput):
        await service.inspect(junk)


# ------------------------------------------------------------------------------ S3
def fake_fetcher(sizes: dict[str, int], data: bytes, seen: list[tuple[str, int]]) -> Any:
    """A fetch_url stand-in that honours max_bytes like the real one and reports ``sizes[url]``
    as the downloaded size (the file written is always the same small valid Parquet)."""

    async def fetch(url: str, dest: Path, *, max_bytes: int, **_: Any) -> Fetched:
        seen.append((url, max_bytes))
        size = sizes[url]
        if size > max_bytes:
            raise _too_large(max_bytes)
        await anyio.Path(dest).write_bytes(data)
        return Fetched(path=dest, filename=url.rsplit("/", 1)[-1], content_type=None, size=size)

    return fetch


async def test_url_inputs_of_one_call_share_one_byte_budget(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Before: each of up to 8 URLs could be max_input_bytes, all of it on RAM-backed /tmp.
    data = parquet_bytes(tmp_path)
    limit = 10 * len(data)
    sizes = {
        "https://e.example/a.parquet": 6 * len(data),
        "https://e.example/b.parquet": 3 * len(data),
    }
    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(service_module, "fetch_url", fake_fetcher(sizes, data, seen))
    service = make_service(make_settings(max_input_bytes=limit))
    tables = {
        "data": SourceRef(url="https://e.example/a.parquet"),
        "b": SourceRef(url="https://e.example/b.parquet"),
    }
    result = await service.query(tables, "SELECT count(*) FROM data, b", limit=1, fmt="json")
    assert result.rows == [[9]]
    assert seen == [
        ("https://e.example/a.parquet", limit),
        ("https://e.example/b.parquet", limit - 6 * len(data)),
    ]


async def test_inputs_over_the_shared_budget_are_413_with_a_concrete_hint(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = parquet_bytes(tmp_path)
    limit = 10 * len(data)
    urls = [f"https://e.example/{n}.parquet" for n in "abc"]
    sizes = dict.fromkeys(urls, 4 * len(data))  # each fits alone; together 12 > 10
    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(service_module, "fetch_url", fake_fetcher(sizes, data, seen))
    service = make_service(make_settings(max_input_bytes=limit))
    tables = {f"t{i}": SourceRef(url=u) for i, u in enumerate(urls)}
    with pytest.raises(InputTooLarge) as info:
        await service.query(tables, "SELECT 1", limit=1, fmt="json")
    assert info.value.http_status == 413
    assert "together" in info.value.message
    assert info.value.hint
    assert "artifact_id" in info.value.hint
    assert len(seen) == 3
    # Nothing downloaded for the refused call is left behind.
    work = service._workroot
    assert list(work.iterdir()) == []


async def test_busy_is_answered_before_any_download(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Before: up to 8 x 50 MB were downloaded, and only then did the job limiter say "busy".
    seen: list[tuple[str, int]] = []
    url = "https://e.example/a.parquet"
    monkeypatch.setattr(service_module, "fetch_url", fake_fetcher({url: 10}, b"x" * 10, seen))
    service = make_service(make_settings(max_concurrent_jobs=1))
    release = anyio.Event()
    holding = anyio.Event()

    async def hold() -> None:
        async with service._limiter:
            holding.set()
            await release.wait()

    async def wait_in_line() -> None:
        async with service._limiter:
            pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(hold)
        await holding.wait()
        for _ in range(4):
            tg.start_soon(wait_in_line)
        while service._limiter.statistics().tasks_waiting < 4:  # noqa: ASYNC110 - no event exists
            await anyio.sleep(0.001)
        for call in (
            service.convert(SourceRef(url=url), ConvertOptions()),
            service.inspect(SourceRef(url=url)),
            service.preview(SourceRef(url=url), columns=None, offset=0, limit=1, fmt="json"),
            service.query({"data": SourceRef(url=url)}, "SELECT 1", limit=1, fmt="json"),
        ):
            with pytest.raises(Busy):
                await call
        release.set()
    assert seen == []


async def test_downloads_wait_for_a_job_slot(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = parquet_bytes(tmp_path)
    url = "https://e.example/a.parquet"
    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(service_module, "fetch_url", fake_fetcher({url: len(data)}, data, seen))
    service = make_service(make_settings(max_concurrent_jobs=1))
    release = anyio.Event()
    holding = anyio.Event()
    results: list[int] = []

    async def hold() -> None:
        async with service._limiter:
            holding.set()
            await release.wait()

    async def query() -> None:
        result = await service.query(
            {"data": SourceRef(url=url)}, "SELECT count(*) FROM data", limit=1, fmt="json"
        )
        results.append(result.rows[0][0])

    async with anyio.create_task_group() as tg:
        tg.start_soon(hold)
        await holding.wait()
        tg.start_soon(query)
        while service._limiter.statistics().tasks_waiting < 1:  # noqa: ASYNC110 - no event exists
            await anyio.sleep(0.001)
        await anyio.sleep(0.05)
        assert seen == []  # queued for a slot, not downloading into RAM meanwhile
        release.set()
    assert seen == [(url, service.settings.max_input_bytes)]
    assert results == [3]


async def test_a_failed_call_gives_its_job_slot_back(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The slot now spans the whole call, so every exit (a refused download, a timeout) must
    # release it, or one bad request would wedge a single-slot server for good.
    import threading

    from aimart_parquet.errors import Timeout

    url = "https://e.example/a.parquet"
    monkeypatch.setattr(service_module, "fetch_url", fake_fetcher({url: 10}, b"x" * 10, []))
    service = make_service(make_settings(max_concurrent_jobs=1, max_input_bytes=5))
    with pytest.raises(InputTooLarge):
        await service.query({"data": SourceRef(url=url)}, "SELECT 1", limit=1, fmt="json")
    finish = threading.Event()
    with pytest.raises(Timeout):
        async with service._job():
            await service._execute(finish.wait, 5, timeout_s=0.05, isolate=False)
    with anyio.fail_after(2):
        async with service._job():
            assert await service._execute(lambda: 7, timeout_s=1, isolate=False) == 7
    finish.set()
    assert service._limiter.statistics().borrowed_tokens == 0


class Upload:
    """A multipart upload as the route sees it: counts how much was read from it."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.read_bytes = 0

    async def read(self, size: int) -> bytes:
        chunk = self.data[self.read_bytes : self.read_bytes + size]
        self.read_bytes += len(chunk)
        return chunk


async def test_upload_is_copied_only_once_it_holds_a_job_slot(
    make_settings: Callable[..., Settings],
) -> None:
    # Before: the route copied the whole upload onto the RAM-backed artifact disk and only then
    # asked for a slot, so a busy server took up to 50 MB per caller before answering "busy".
    service = make_service(make_settings(max_concurrent_jobs=1))
    release = anyio.Event()
    holding = anyio.Event()
    queued = Upload(CSV)
    refused = Upload(CSV)
    results: list[int] = []

    async def hold() -> None:
        async with service._limiter:
            holding.set()
            await release.wait()

    async def wait_in_line() -> None:
        async with service._limiter:
            pass

    async def convert() -> None:
        result = await service.convert_upload(queued.read, "q.csv", ConvertOptions())
        results.append(result.report.rows)

    async with anyio.create_task_group() as tg:
        tg.start_soon(hold)
        await holding.wait()
        tg.start_soon(convert)
        while service._limiter.statistics().tasks_waiting < 1:  # noqa: ASYNC110 - no event exists
            await anyio.sleep(0.001)
        assert queued.read_bytes == 0  # waiting for a slot, not copying meanwhile
        for _ in range(4):
            tg.start_soon(wait_in_line)
        while service._limiter.statistics().tasks_waiting < 5:  # noqa: ASYNC110 - no event exists
            await anyio.sleep(0.001)
        with pytest.raises(Busy):
            await service.convert_upload(refused.read, "r.csv", ConvertOptions())
        assert refused.read_bytes == 0
        release.set()
    assert results == [3]
    assert queued.read_bytes == len(CSV)
    assert list(service._workroot.iterdir()) == []


async def test_upload_over_the_input_limit_is_413_and_leaves_nothing(
    make_settings: Callable[..., Settings],
) -> None:
    service = make_service(make_settings(max_input_bytes=2 * 1024 * 1024))
    big = Upload(b"a,b\n" + b"1,2\n" * (1024 * 1024))  # 4 MB, read 1 MB at a time
    with pytest.raises(InputTooLarge) as info:
        await service.convert_upload(big.read, "big.csv", ConvertOptions())
    assert info.value.http_status == 413
    assert info.value.hint == "Split the file or pass a url."
    assert big.read_bytes < len(big.data)  # stopped at the cap, not after reading it all
    assert list(service._workroot.iterdir()) == []
    assert service._limiter.statistics().borrowed_tokens == 0
