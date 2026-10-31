"""All of a call's URL downloads share one ``fetch_timeout_s``, as the payment window assumes.

The startup check (``Payments._check_payment_window``) lets a server start only when
queue_wait_s + fetch_timeout_s + the longest operation timeout + 60 s fit in the 300 s a
payment authorization is valid for. But every ``fetch_url`` got its own fresh fetch_timeout_s,
so a REST query over `data` and 7 URL tables could download for 8 x 60 s inside the job slot:
the work ran past the authorization and could not be settled. Scaled down here (2 s), such a
query took 8 x 0.6 s and was answered 200 and settled; now its downloads stop at 2 s in all.
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


class Drip(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(PARQUET)))
        self.end_headers()
        step = len(PARQUET) // 4 + 1
        for i in range(0, len(PARQUET), step):
            time.sleep(DRIP_S / 4)
            self.wfile.write(PARQUET[i : i + step])
            self.wfile.flush()

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def base(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    monkeypatch.setattr(sources, "ALLOWED_PORTS", frozenset({80, 443, port}))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


async def test_downloads_inside_one_deadline_share_it(base: str, tmp_path: Path) -> None:
    started = time.monotonic()
    with sources.downloads_deadline(DRIP_S * 1.5):
        first = await sources.fetch_url(
            f"{base}/a.parquet", tmp_path / "a", max_bytes=10**6, deadline_s=30, allow_private=True
        )
        with pytest.raises(FetchFailed) as info:
            await sources.fetch_url(
                f"{base}/b.parquet",
                tmp_path / "b",
                max_bytes=10**6,
                deadline_s=30,
                allow_private=True,
            )
    assert first.size == len(PARQUET)
    assert info.value.message == "Downloading this call's URL inputs took longer than 0.9 s in all."
    assert "artifact_id" in info.value.hint
    assert time.monotonic() - started < DRIP_S * 1.5 + 0.5


async def test_without_a_call_deadline_each_fetch_keeps_its_own(base: str, tmp_path: Path) -> None:
    for name in ("a", "b"):
        got = await sources.fetch_url(
            f"{base}/{name}.parquet",
            tmp_path / name,
            max_bytes=10**6,
            deadline_s=DRIP_S * 1.5,
            allow_private=True,
        )
        assert got.size == len(PARQUET)


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


async def test_the_deadline_starts_with_the_slot_and_ends_with_the_call(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(max_concurrent_jobs=1, fetch_timeout_s=5.0, queue_wait_s=3.0)
    service = Service(settings, build_store(settings))
    left: list[float] = []

    async def fake_fetch(url: str, dest: Path, **_: Any) -> sources.Fetched:
        call = sources._CALL_DOWNLOADS.get()
        assert call is not None
        left.append(call.deadline - anyio.current_time())
        await anyio.Path(dest).write_bytes(PARQUET)
        return sources.Fetched(path=dest, filename="a.parquet", content_type=None, size=1)

    monkeypatch.setattr("aimart_parquet.service.fetch_url", fake_fetch)

    async def hold_the_slot(waited: anyio.Event) -> None:
        async with service._job():
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
