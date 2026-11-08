"""All of a call's URL downloads share one ``fetch_timeout_s``, as the payment window assumes.

The startup check (``Payments._check_payment_window``) lets a server start only when
queue_wait_s + fetch_timeout_s + the longest operation timeout + 60 s fit in the 300 s a
payment authorization is valid for. But every ``fetch_url`` got its own fresh fetch_timeout_s,
so a REST query over `data` and 7 URL tables could download for 8 x 60 s inside the job slot:
the work ran past the authorization and could not be settled. Scaled down here (2 s), such a
query took 8 x 0.6 s and was answered 200 and settled; now its downloads stop at 2 s in all.

Only time spent downloading counts. The first shared allowance was a deadline from the moment
the slot was taken, so the artifact sweep that runs first inside it (on GCS, once every 6 h,
listing the bucket and deleting expired blobs one at a time) and the call's stored artifacts
fetched back from the bucket were charged to the downloads: a URL served at once was refused
with 422 fetch_failed "took longer than 60 s in all", a sale lost.
"""

from __future__ import annotations

import io
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import anyio
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import aimart_testkit as kit
from aimart_parquet import sources
from aimart_parquet.artifacts import build_store
from aimart_parquet.errors import FetchFailed
from aimart_parquet.service import Service, SourceRef

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

_buf = io.BytesIO()
pq.write_table(pa.table({"k": [1, 2], "v": ["a", "b"]}), _buf)
PARQUET = _buf.getvalue()
DRIP_S = 0.6  # each file takes this long to arrive, well inside any one fetch's deadline
# One call's download allowance in these tests: four drips. These tests gave 0.9 s (1.5 drips),
# and on a CI macOS runner one 0.6 s file took longer than that to arrive: the server's sleeps
# fire late on a loaded Mac (under background QoS here, about 0.2 s each), so the first,
# correct, download was refused. Every bound below now either has a second or more of slack
# or compares the test's own measurements, which a slow machine moves together.
ALLOW_S = 2.4


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Drip)
        self.released = threading.Event()  # set when the test ends: /hang/ answers stop waiting


class Drip(BaseHTTPRequestHandler):
    """PARQUET at once under /fast/; under /hang/, half of it and then nothing more; otherwise
    half, a DRIP_S pause and the rest (one pause: each sleep is a timer a loaded machine fires
    late, and four of them made a 0.6 s file take 1.4 s)."""

    server: _Server

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(PARQUET)))
        self.end_headers()
        if self.path.startswith("/fast/"):
            self.wfile.write(PARQUET)
            return
        half = len(PARQUET) // 2
        self.wfile.write(PARQUET[:half])
        self.wfile.flush()
        if self.path.startswith("/hang/"):
            self.server.released.wait(60)  # only a deadline ends the download
            return
        time.sleep(DRIP_S)
        self.wfile.write(PARQUET[half:])

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def base(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    httpd = _Server()
    port = httpd.server_address[1]
    monkeypatch.setattr(sources, "ALLOWED_PORTS", frozenset({80, 443, port}))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.released.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


async def _fetch(url: str, dest: Path) -> sources.Fetched:
    """One URL input with a deadline of its own (30 s) that no test here comes near."""
    return await sources.fetch_url(url, dest, max_bytes=10**6, deadline_s=30, allow_private=True)


async def test_downloads_inside_one_deadline_share_it(base: str, tmp_path: Path) -> None:
    with sources.downloads_deadline(ALLOW_S):
        call = sources._CALL_DOWNLOADS.get()
        assert call is not None
        first = await _fetch(f"{base}/a.parquet", tmp_path / "a")
        left = call.remaining
        started = anyio.current_time()
        with pytest.raises(FetchFailed) as info:
            await _fetch(f"{base}/hang/b.parquet", tmp_path / "b")
        cut = anyio.current_time() - started
    assert first.size == len(PARQUET)
    assert left < ALLOW_S - DRIP_S + 0.01  # the first file's time came out of the allowance
    assert info.value.message == "Downloading this call's URL inputs took longer than 2.4 s in all."
    assert "artifact_id" in info.value.hint
    # The second download stopped when what the first left ran out. An allowance of its own
    # would have let it run ALLOW_S, and before, it ran DRIP_S and was served. The gap is the
    # first download's time, which a slow machine only makes longer.
    assert left - 0.01 < cut < ALLOW_S


async def test_time_not_spent_downloading_is_not_charged_to_the_downloads(
    base: str, tmp_path: Path
) -> None:
    with sources.downloads_deadline(ALLOW_S):
        call = sources._CALL_DOWNLOADS.get()
        assert call is not None
        # The sweep, or a stored artifact fetched from GCS. With the allowance running from the
        # slot, less than one file's time was left after this, and the first fetch failed.
        await anyio.sleep(ALLOW_S - DRIP_S / 2)
        assert call.remaining == ALLOW_S
        started = anyio.current_time()
        first = await _fetch(f"{base}/a.parquet", tmp_path / "a")
        took = anyio.current_time() - started
        # Charged the fetch's own time: no less than the file took to arrive, no more than the
        # test saw it take. (These asserted 0.33-0.72 s of 0.9 s, a bound on the machine.)
        assert ALLOW_S - took - 1e-6 <= call.remaining < ALLOW_S - DRIP_S + 0.01
        with pytest.raises(FetchFailed, match=r"took longer than 2\.4 s in all"):
            await _fetch(f"{base}/hang/b.parquet", tmp_path / "b")
    assert first.size == len(PARQUET)


async def test_a_slow_gcs_sweep_does_not_make_a_fast_url_fail(
    make_settings: Callable[..., Settings], base: str, tmp_path: Path
) -> None:
    # The reviewer's case, scaled down: a 1 s download allowance, and a listing sweep over 60
    # expired orphan blobs whose DELETEs take 25 ms each. Before: 422 fetch_failed.
    from aimart_parquet.artifacts import GcsArtifactStore
    from aimart_parquet.engine.models import ConvertOptions
    from fixtures.money.fake_gcs import FakeBlob, FakeClient

    settings = make_settings(allow_private_urls=True, fetch_timeout_s=1.0, queue_wait_s=0.0)
    client = FakeClient()
    bucket = client.the_bucket
    for i in range(60):  # written by an instance that is gone, expired an hour ago
        name = f"artifacts/orphan{i:018d}xx/data.parquet"
        blob = FakeBlob(bucket, name)
        blob.metadata = {"expires_at": str(time.time() - 3600)}
        blob.data = b"x"
        bucket.blobs[name] = blob
    delete = bucket.delete_blob

    def slow_delete(name: str) -> None:
        time.sleep(0.025)  # one GCS DELETE round trip
        delete(name)

    bucket.delete_blob = slow_delete  # type: ignore[method-assign]
    store = GcsArtifactStore(
        tmp_path / "gcs",
        bucket="b",
        ttl_s=3600,
        max_total_bytes=10**9,
        secret=b"s" * 32,
        client=client,
    )
    # Signed GCS links need real credentials; the link is not what this test is about.
    store.download_url = lambda base_url, art: f"https://storage.example/{art.id}"  # type: ignore[method-assign]
    service = Service(settings, store)
    started = time.monotonic()
    result = await service.convert(SourceRef(url=f"{base}/fast/sales.parquet"), ConvertOptions())
    assert result.report.rows == 2
    assert len(bucket.ops("delete")) == 60  # the sweep did run first, inside the call
    assert time.monotonic() - started > settings.fetch_timeout_s


async def test_without_a_call_deadline_each_fetch_keeps_its_own(base: str, tmp_path: Path) -> None:
    # Four files of DRIP_S, each fetch given 2 s: more than 2 s of downloads in all, which only
    # fits because each fetch has a deadline of its own. (Before: two files, 0.9 s each, and on
    # a CI macOS runner one file alone took longer than its 0.9 s.)
    own_s = 2.0
    started = anyio.current_time()
    for name in "abcd":
        got = await sources.fetch_url(
            f"{base}/{name}.parquet",
            tmp_path / name,
            max_bytes=10**6,
            deadline_s=own_s,
            allow_private=True,
        )
        assert got.size == len(PARQUET)
    assert anyio.current_time() - started > own_s


async def test_a_paid_query_over_url_tables_stops_downloading_at_fetch_timeout_s(
    make_settings: Callable[..., Settings], base: str
) -> None:
    settings = make_settings(
        allow_private_urls=True,
        fetch_timeout_s=2.0,
        query_timeout_s=0.5,
        convert_timeout_s=1.0,
        queue_wait_s=0.0,
        enable_quantum=False,
    )
    fake = kit.FakeFacilitator()
    body = {
        "url": f"{base}/data.parquet",
        "tables": {f"t{i}": {"url": f"{base}/t{i}.parquet"} for i in range(7)},
        "sql": "SELECT count(*) AS n FROM data",
    }
    async with kit.running_app(settings, facilitator=fake) as h:  # the window check accepted it
        started = time.monotonic()
        r = await h.paying.post("/v1/query", json=body)
        elapsed = time.monotonic() - started
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "fetch_failed"
    assert error["message"] == "Downloading this call's URL inputs took longer than 2 s in all."
    assert len(fake.verified) == 1
    assert fake.settled == []
    assert elapsed < settings.fetch_timeout_s + 1.5  # 8 x 0.6 s = 4.8 s before


async def test_the_queue_is_not_download_time_and_the_allowance_ends_with_the_call(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(max_concurrent_jobs=1, fetch_timeout_s=5.0, queue_wait_s=3.0)
    service = Service(settings, build_store(settings))
    left: list[float] = []

    async def fake_fetch(url: str, dest: Path, **_: Any) -> sources.Fetched:
        call = sources._CALL_DOWNLOADS.get()
        assert call is not None
        left.append(call.remaining)
        await anyio.Path(dest).write_bytes(PARQUET)
        return sources.Fetched(path=dest, filename="a.parquet", content_type=None, size=1)

    monkeypatch.setattr("aimart_parquet.service.fetch_url", fake_fetch)

    async def hold_the_slot(waited: anyio.Event) -> None:
        async with service._job(1):
            waited.set()
            await anyio.sleep(1.0)

    async with anyio.create_task_group() as tg:
        waited = anyio.Event()
        tg.start_soon(hold_the_slot, waited)
        await waited.wait()
        await service.inspect(SourceRef(url="https://e.example/a.parquet"))  # queued for ~1 s
    [remaining] = left
    assert remaining > 4.5  # the second in the queue did not come out of the download time
    assert sources._CALL_DOWNLOADS.get() is None
