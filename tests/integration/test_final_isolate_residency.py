"""A paid local output lives until its advertised expires_at: capacity never evicts it sooner.

Incident (final money review): ``LocalArtifactStore`` (any deployment without a bucket;
deploy.yml runs one with a 256 MiB budget) evicted a committed, paid output for a newer one once
it was ``MIN_RESIDENCY_S`` (600 s) old, about 49 minutes before the ``expires_at`` its response
advertised, which the agents guide calls "the exact time it becomes unreachable". The caller had
paid for an artifact_id and a link that 404'd early, and was told artifacts "expire after a
TTL". On the local store the folder is the artifact, so capacity now takes only what has
expired, and a store full of live outputs answers 503 ``busy`` (unpaid) instead. On GCS
eviction drops only the cached copy (the blob and its link stay valid), so it is unchanged.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit
from aimart_parquet import artifacts
from aimart_parquet.artifacts import GcsArtifactStore, LocalArtifactStore
from aimart_parquet.errors import Busy
from fixtures.money.fake_gcs import FakeClient

if TYPE_CHECKING:
    from pathlib import Path

DATA = b"x" * 1000
CSV = b"a,b\n" + b"".join(f"{i},{i * 7}\n".encode() for i in range(2000))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [time.time()]
    monkeypatch.setattr(artifacts.time, "time", lambda: now[0])
    return now


def src(tmp_path: Path, name: str) -> Path:
    path = tmp_path / "incoming" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(DATA)
    return path


def on_disk(root: Path) -> set[str]:
    return {p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")}


def test_a_live_local_output_is_never_evicted(tmp_path: Path, clock: list[float]) -> None:
    store = LocalArtifactStore(
        tmp_path / "store", ttl_s=3600, max_total_bytes=1500, secret=b"s" * 32
    )
    first = store.put(src(tmp_path, "1"), "1.parquet")  # committed: no payment block around it
    for age in (601, 3599):  # past the old 600 s residency, and a second before its expiry
        clock[0] = first.created_at + age
        aged = time.time() - age
        os.utime(store.root / first.id, (aged, aged))
        with pytest.raises(Busy) as info:
            store.put(src(tmp_path, f"2-{age}"), "2.parquet")
        assert info.value.http_status == 503  # >= 400: the second caller is not charged
        assert "not charged" in (info.value.hint or "")
        assert on_disk(store.root) == {first.id}
        store.get(first.id)  # still served
    # The hint says when to retry: the first output expires in a second, so about a minute.
    assert "Retry in about 1 minute" in (info.value.hint or "")
    assert info.value.details["retry_after_s"] == 1
    clock[0] = first.expires_at + 1  # expired: unreachable anyway, so it may make room
    second = store.put(src(tmp_path, "3"), "3.parquet")
    assert on_disk(store.root) == {second.id}


def test_the_retry_hint_counts_the_outputs_that_must_expire(
    tmp_path: Path, clock: list[float]
) -> None:
    store = LocalArtifactStore(
        tmp_path / "store", ttl_s=3600, max_total_bytes=3000, secret=b"s" * 32
    )
    start = clock[0]
    store.put(src(tmp_path, "a"), "a.parquet")
    clock[0] = start + 1200
    store.put(src(tmp_path, "b"), "b.parquet")
    clock[0] = start + 1800
    store.put(src(tmp_path, "c"), "c.parquet")
    big = tmp_path / "big"
    big.write_bytes(b"z" * 2000)
    with pytest.raises(Busy) as info:  # 2000 more bytes: a and b must both expire first
        store.put(big, "big.parquet")
    assert info.value.details["retry_after_s"] == 3600 + 1200 - 1800
    assert "Retry in about 50 minutes" in (info.value.hint or "")


def test_only_unsettled_outputs_holding_the_space_say_a_minute(
    tmp_path: Path, clock: list[float]
) -> None:
    store = LocalArtifactStore(
        tmp_path / "store", ttl_s=3600, max_total_bytes=1500, secret=b"s" * 32
    )
    with artifacts.track_puts():
        store.put(src(tmp_path, "p"), "p.parquet")  # settling: gone or committed in seconds
        clock[0] += 3601  # even past its TTL it stays until the payment layer decides
        with pytest.raises(Busy) as info:
            store.put(src(tmp_path, "q"), "q.parquet")
    assert "Retry in a minute" in (info.value.hint or "")
    assert "retry_after_s" not in info.value.details


def test_gcs_still_drops_a_fresh_cached_copy(tmp_path: Path) -> None:
    # Eviction there frees only the local cache: the blob, and so the link, stay valid.
    client = FakeClient()
    store = GcsArtifactStore(
        tmp_path / "cache",
        bucket="aimart-test",
        ttl_s=3600,
        max_total_bytes=1500,
        secret=b"s" * 32,
        client=client,
    )
    first = store.put(src(tmp_path, "1"), "1.parquet")
    second = store.put(src(tmp_path, "2"), "2.parquet")
    assert on_disk(store.root) == {second.id}
    assert len(client.the_bucket.blobs) == 2
    _, path = store.get(first.id)  # rehydrated from the bucket
    assert path.read_bytes() == DATA


@pytest.mark.anyio
async def test_a_paid_output_outlives_a_newer_paid_call(
    make_settings: Any, facilitator: kit.FakeFacilitator
) -> None:
    # The reviewer's sequence, on the wire: the first caller paid, its artifact is 601 s old,
    # and a second paid convert needs its room.
    settings = make_settings()
    async with kit.running_app(settings, facilitator=facilitator) as h:
        body = {"content_base64": kit.b64(CSV), "filename": "first.csv"}
        first = await h.paying.post("/v1/convert", json=body)
        assert first.status_code == 200
        art = first.json()["artifact"]
        store = h.app.state.service.store
        store.max_total_bytes = int(art["size_bytes"] * 1.5)  # room for one output this size
        aged = time.time() - 601
        os.utime(store.root / art["id"], (aged, aged))
        assert len(facilitator.settled) == 1

        second = await h.paying.post("/v1/convert", json={**body, "filename": "second.csv"})
        assert second.status_code == 503, second.text  # before: 200, and the first link died
        assert second.json()["error"]["code"] == "busy"
        assert "payment-response" not in second.headers
        assert len(facilitator.settled) == 1  # the second caller was not charged

        expires = dt.datetime.fromisoformat(art["expires_at"])
        assert expires > dt.datetime.now(dt.UTC) + dt.timedelta(minutes=55)
        link = await h.http.get(art["download_url"].removeprefix(kit.BASE_URL))
        assert link.status_code == 200
        assert link.content[:4] == b"PAR1"
        inspect = await h.paying.post("/v1/inspect", json={"artifact_id": art["id"]})
        assert inspect.status_code == 200, inspect.text
