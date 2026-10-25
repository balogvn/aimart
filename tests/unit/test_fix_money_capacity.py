"""The artifact budget, round 2: what a running call reads, a delivered output, and an output
whose payment has not settled yet are never evicted, and they all count.

RV1: on GCS, rehydrating a query's second table evicted the cached copy of its first, so a
join of artifacts that together exceed the local budget always failed (FileNotFoundError).
RV2: a new output evicted one delivered milliseconds earlier: a charged 200 whose
download_url already 404'd. Delivered outputs now stay at least MIN_RESIDENCY_S, and an output
that cannot fit without evicting one sooner is refused (503 busy, not charged).
RV3: unsettled outputs were left out of the count, so the RAM-backed disk held the budget plus
every output waiting for settlement.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import pytest

import aimart_testkit as kit
from aimart_parquet import artifacts
from aimart_parquet.artifacts import GcsArtifactStore, LocalArtifactStore, track_puts
from aimart_parquet.errors import Busy
from aimart_parquet.service import Service, SourceRef
from fixtures.money.fake_gcs import FakeClient

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from aimart_parquet.config import Settings

SECRET = b"test-secret"
DATA = b"x" * 1000


def store_at(root: Path, *, max_total_bytes: int = 1500, ttl_s: int = 3600) -> LocalArtifactStore:
    return LocalArtifactStore(root, ttl_s=ttl_s, max_total_bytes=max_total_bytes, secret=SECRET)


def gcs_at(root: Path, client: FakeClient, *, max_total_bytes: int = 10**9) -> GcsArtifactStore:
    return GcsArtifactStore(
        root,
        bucket="aimart-test",
        ttl_s=3600,
        max_total_bytes=max_total_bytes,
        secret=SECRET,
        client=client,
    )


def src_file(tmp_path: Path, name: str, data: bytes = DATA) -> Path:
    path = tmp_path / "incoming" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def on_disk(store: LocalArtifactStore) -> dict[str, int]:
    """Artifact id -> bytes of its data file, as the budget counts them."""
    return {
        p.name: (p / "data.parquet").stat().st_size
        for p in store.root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and (p / "data.parquet").exists()
    }


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [time.time()]
    monkeypatch.setattr(artifacts.time, "time", lambda: now[0])
    return now


# ------------------------------------------------------------------------------ RV2
def test_a_delivered_output_is_not_evicted_for_a_newer_one(
    tmp_path: Path, clock: list[float]
) -> None:
    # Before: the second put evicted the first, whose caller had just paid for its link.
    store = store_at(tmp_path / "store")
    first = store.put(src_file(tmp_path, "1"), "1.parquet")
    with pytest.raises(Busy) as info:
        store.put(src_file(tmp_path, "2"), "2.parquet")
    assert info.value.http_status == 503  # >= 400: the second caller is not charged
    assert info.value.hint
    assert "not charged" in info.value.hint
    assert set(on_disk(store)) == {first.id}
    store.get(first.id)

    clock[0] += artifacts.MIN_RESIDENCY_S + 1  # delivered long enough ago: it may make room
    second = store.put(src_file(tmp_path, "3"), "3.parquet")
    assert set(on_disk(store)) == {second.id}


def test_the_residency_never_outlasts_the_ttl(tmp_path: Path, clock: list[float]) -> None:
    store = store_at(tmp_path / "store", ttl_s=60)
    store.put(src_file(tmp_path, "1"), "1.parquet")
    clock[0] += 61  # expired: it was going to be swept anyway
    second = store.put(src_file(tmp_path, "2"), "2.parquet")
    assert set(on_disk(store)) == {second.id}


def test_a_refused_output_evicts_nothing(tmp_path: Path, clock: list[float]) -> None:
    # Evicting the old artifact would not have made room either: nothing may go for nothing.
    store = store_at(tmp_path / "store", max_total_bytes=2500)
    old = store.put(src_file(tmp_path, "old"), "old.parquet")
    clock[0] += artifacts.MIN_RESIDENCY_S + 1
    young = store.put(src_file(tmp_path, "young"), "young.parquet")
    os.utime(store.root / young.id, (clock[0], clock[0]))  # stored "now" on the moved clock
    with pytest.raises(Busy):
        store.put(src_file(tmp_path, "big", b"z" * 2000), "big.parquet")
    assert set(on_disk(store)) == {old.id, young.id}


# ------------------------------------------------------------------------------ RV3
def test_unsettled_outputs_count_toward_the_budget(tmp_path: Path, clock: list[float]) -> None:
    # Before: provisional outputs were not counted, so concurrent paid calls waiting for their
    # settlement held a multiple of the budget on the RAM-backed disk.
    store = store_at(tmp_path / "store")
    with track_puts():
        pending = store.put(src_file(tmp_path, "p"), "p.parquet")
    with track_puts() as produced, pytest.raises(Busy):
        store.put(src_file(tmp_path, "q"), "q.parquet")
    assert produced == []
    assert set(on_disk(store)) == {pending.id}
    assert sum(on_disk(store).values()) <= store.max_total_bytes


def test_unsettled_outputs_are_never_evicted(tmp_path: Path, clock: list[float]) -> None:
    store = store_at(tmp_path / "store")
    with track_puts() as produced:
        pending = store.put(src_file(tmp_path, "p"), "p.parquet")
    clock[0] += artifacts.MIN_RESIDENCY_S + 1  # old enough, but its call has not settled
    with pytest.raises(Busy):
        store.put(src_file(tmp_path, "l"), "l.parquet")
    assert set(on_disk(store)) == {pending.id}
    store.commit(produced)  # settled: from now on an old artifact like any other
    later = store.put(src_file(tmp_path, "l2"), "l2.parquet")
    assert set(on_disk(store)) == {later.id}


def test_an_output_makes_room_before_it_is_stored(tmp_path: Path, clock: list[float]) -> None:
    # The disk never holds more than the budget, not even between storing and evicting.
    store = store_at(tmp_path / "store")
    old = store.put(src_file(tmp_path, "old"), "old.parquet")
    clock[0] += artifacts.MIN_RESIDENCY_S + 1
    peaks: list[int] = []
    real = store._store_local

    def measured(src: Path, filename: str) -> artifacts.Artifact:
        peaks.append(sum(on_disk(store).values()) + src.stat().st_size)
        return real(src, filename)

    store._store_local = measured  # type: ignore[method-assign]
    with track_puts():
        new = store.put(src_file(tmp_path, "new"), "new.parquet")
    assert peaks == [len(DATA)]
    assert set(on_disk(store)) == {new.id}
    assert old.id not in on_disk(store)


# ------------------------------------------------------------------------------ RV1
def test_artifacts_a_running_call_reads_are_never_evicted(tmp_path: Path) -> None:
    client = FakeClient()
    writer = gcs_at(tmp_path / "writer", client)
    a = writer.put(src_file(tmp_path, "a"), "a.parquet")
    b = writer.put(src_file(tmp_path, "b"), "b.parquet")
    cold = gcs_at(tmp_path / "cold", client, max_total_bytes=1500)
    with cold.hold():
        _, a_path = cold.get(a.id)
        _, b_path = cold.get(b.id)  # before: evicted a's cached copy, which the call was using
        assert a_path.read_bytes() == DATA
        assert b_path.read_bytes() == DATA
    # Released: the cache is trimmed back to the budget, and the blobs are untouched.
    assert sum(on_disk(cold).values()) <= cold.max_total_bytes
    assert len(client.the_bucket.blobs) == 2
    _, again = cold.get(a.id)
    assert again.read_bytes() == DATA


def test_an_expired_artifact_in_use_is_swept_after_the_call(
    tmp_path: Path, clock: list[float]
) -> None:
    store = store_at(tmp_path / "store", max_total_bytes=10**6, ttl_s=60)
    art = store.put(src_file(tmp_path, "a"), "a.parquet")
    with store.hold():
        _, path = store.get(art.id)
        clock[0] += 61
        assert store.sweep() == 0  # a running query still reads it by path
        assert path.exists()
    assert store.sweep() == 1


def test_holds_are_counted_per_call(tmp_path: Path) -> None:
    client = FakeClient()
    writer = gcs_at(tmp_path / "writer", client)
    a = writer.put(src_file(tmp_path, "a"), "a.parquet")
    b = writer.put(src_file(tmp_path, "b"), "b.parquet")
    cold = gcs_at(tmp_path / "cold", client, max_total_bytes=1500)
    with cold.hold():
        cold.get(a.id)
        with cold.hold():  # a second call (e.g. a concurrent task) reads the same artifact
            cold.get(a.id)
        cold.get(b.id)  # the first call still reads a: it must stay
        assert set(on_disk(cold)) == {a.id, b.id}
    assert cold._pins == {}


@pytest.mark.anyio
async def test_gcs_query_joining_artifacts_bigger_than_the_cache_together(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    # The reviewer's case: two artifacts of about 60% of the local budget each, written by
    # another instance (or evicted from this one's cache), joined in one paid query.
    client = FakeClient()
    writer = gcs_at(tmp_path / "writer", client)
    ids = {}
    for name in ("a", "b"):
        path = kit.write_parquet(
            tmp_path / f"{name}.parquet", {"k": list(range(4000)), name: list(range(4000))}
        )
        ids[name] = writer.put(path, f"{name}.parquet")
    biggest = max(art.size_bytes for art in ids.values())
    settings = make_settings(artifact_max_total_bytes=int(biggest * 1.6))
    store = gcs_at(settings.artifact_dir, client, max_total_bytes=settings.artifact_max_total_bytes)
    service = Service(settings, store)
    result = await service.query(
        {"data": SourceRef(artifact_id=ids["a"].id), "b": SourceRef(artifact_id=ids["b"].id)},
        "SELECT count(*) FROM data JOIN b USING (k)",
        limit=5,
        fmt="json",
    )
    assert result.rows == [[4000]]
    assert sum(on_disk(store).values()) <= settings.artifact_max_total_bytes


def test_a_failed_trim_after_a_call_never_fails_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The trim runs as the call's job ends, after its result exists: a disk error is logged.
    store = store_at(tmp_path / "store", max_total_bytes=10**6)
    art = store.put(src_file(tmp_path, "a"), "a.parquet")

    def broken(**_: object) -> None:
        raise OSError("disk hiccup")

    monkeypatch.setattr(store, "enforce_capacity", broken)
    with store.hold():
        store.get(art.id)
    assert store._pins == {}
    assert any("capacity enforcement failed" in r.message for r in caplog.records)
