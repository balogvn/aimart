"""Artifact store fixes: the output budget (M1), deleting withheld outputs (M2), and actually
deleting expired GCS blobs at the TTL (G1), against a fake bucket that records every call."""

from __future__ import annotations

import datetime as dt
import os
import time
from typing import TYPE_CHECKING, Any

import pytest

from aimart_parquet import artifacts
from aimart_parquet.artifacts import GcsArtifactStore, LocalArtifactStore, track_puts
from aimart_parquet.errors import AimartError, ArtifactNotFound, Busy, InputTooLarge
from fixtures.money.fake_gcs import FakeBlob, FakeClient, key

if TYPE_CHECKING:
    from pathlib import Path

SECRET = b"test-secret"
DATA = b"x" * 1000


def store_at(root: Path, *, ttl_s: int = 3600, max_total_bytes: int = 10**9) -> LocalArtifactStore:
    return LocalArtifactStore(root, ttl_s=ttl_s, max_total_bytes=max_total_bytes, secret=SECRET)


def src_file(tmp_path: Path, data: bytes = DATA, name: str = "src") -> Path:
    path = tmp_path / "incoming" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def folders(store: LocalArtifactStore) -> set[str]:
    return {p.name for p in store.root.iterdir() if p.is_dir() and not p.name.startswith(".")}


def delivered_long_ago(store: LocalArtifactStore, *ids: str) -> None:
    """Age artifacts past the minimum residency, so capacity may evict them."""
    then = time.time() - artifacts.MIN_RESIDENCY_S - 60
    for artifact_id in ids:
        os.utime(store.root / artifact_id, (then, then))


# ------------------------------------------------------------------------------ M1: the budget
def test_an_output_bigger_than_the_whole_budget_is_refused_before_storing(tmp_path: Path) -> None:
    # Before: put() stored it, enforce_capacity() evicted it (and every older artifact), and the
    # caller was charged for a download_url that 404'd.
    store = store_at(tmp_path / "store", max_total_bytes=5000)
    older = store.put(src_file(tmp_path, b"a" * 100, "older"), "older.parquet")
    big = src_file(tmp_path, b"b" * 5001, "big")
    with track_puts() as produced, pytest.raises(InputTooLarge) as info:
        store.put(big, "big.parquet")
    assert info.value.http_status == 413  # >= 400: the caller is not charged
    assert info.value.code == "input_too_large"
    assert "output exceeds the artifact budget" in info.value.message
    assert info.value.hint
    assert "not charged" in info.value.hint
    assert folders(store) == {older.id}  # nothing stored, nothing evicted to make room
    assert produced == []
    store.get(older.id)


def test_capacity_never_evicts_the_artifact_just_written(tmp_path: Path) -> None:
    store = store_at(tmp_path / "store")
    first = store.put(src_file(tmp_path, DATA, "1"), "1.parquet")
    delivered_long_ago(store, first.id)
    # The data fits the budget but data + meta.json does not: before, the new artifact was
    # evicted right after the older one, and put() still returned it.
    store.max_total_bytes = len(DATA) + 10
    second = store.put(src_file(tmp_path, DATA, "2"), "2.parquet")
    got, path = store.get(second.id)
    assert got == second
    assert path.read_bytes() == DATA
    assert folders(store) == {second.id}
    assert first.id not in folders(store)


def test_enforce_capacity_keeps_the_named_artifact(tmp_path: Path) -> None:
    store = store_at(tmp_path / "store")
    a = store.put(src_file(tmp_path, DATA, "a"), "a.parquet")
    b = store.put(src_file(tmp_path, DATA, "b"), "b.parquet")
    delivered_long_ago(store, a.id, b.id)
    store.max_total_bytes = 1
    store.enforce_capacity(keep=a.id)
    assert folders(store) == {a.id}
    assert b.id not in folders(store)


# ------------------------------------------------------------------------------ M2: delete
def test_delete_removes_an_artifact_and_tolerates_unknown_ids(tmp_path: Path) -> None:
    store = store_at(tmp_path / "store")
    art = store.put(src_file(tmp_path), "x.parquet")
    store.delete(art.id)
    assert folders(store) == set()
    with pytest.raises(ArtifactNotFound):
        store.get(art.id)
    store.delete(art.id)  # already gone: no error
    store.delete("../../etc")  # malformed: never touches the filesystem
    assert (tmp_path / "store").exists()


def test_track_puts_records_only_inside_the_block(tmp_path: Path) -> None:
    store = store_at(tmp_path / "store")
    outside = store.put(src_file(tmp_path, name="o"), "o.parquet")
    with track_puts() as produced:
        inside = store.put(src_file(tmp_path, name="i"), "i.parquet")
        with track_puts() as nested:
            deeper = store.put(src_file(tmp_path, name="d"), "d.parquet")
    after = store.put(src_file(tmp_path, name="a"), "a.parquet")
    assert produced == [inside.id, deeper.id]  # an inner block reports to the outer one too
    assert nested == [deeper.id]
    assert outside.id not in produced
    assert after.id not in produced


def test_provisional_outputs_count_and_are_never_evicted_until_committed(
    tmp_path: Path,
) -> None:
    # A paid call's output is stored before settlement. Round 1 left it out of the count (the
    # RAM disk then held the budget plus every unsettled output); it counts, and is never
    # evicted until the payment layer commits it.
    store = store_at(tmp_path / "store")
    store.max_total_bytes = len(DATA) + 200  # room for one artifact
    with track_puts() as produced:
        pending = store.put(src_file(tmp_path, DATA, "q"), "q.parquet")
    delivered_long_ago(store, pending.id)
    with pytest.raises(Busy):  # not evicted for a later output, however old
        store.put(src_file(tmp_path, DATA, "l"), "l.parquet")
    assert folders(store) == {pending.id}
    store.commit(produced)  # settled: an old output like any other
    later = store.put(src_file(tmp_path, DATA, "l2"), "l2.parquet")
    assert folders(store) == {later.id}


def test_a_deleted_provisional_output_is_forgotten(tmp_path: Path) -> None:
    store = store_at(tmp_path / "store")
    with track_puts() as produced:
        art = store.put(src_file(tmp_path), "x.parquet")
    store.delete(art.id)
    store.commit(produced)  # a late commit of a withheld output stores nothing
    assert folders(store) == set()
    assert store._provisional == set()


def test_commit_never_fails_a_settled_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # commit() runs after settlement: raising there turned a paid 200 into a 500. It is
    # bookkeeping only (room was made when the output was stored), so no disk error reaches it.
    store = store_at(tmp_path / "store")
    with track_puts() as produced:
        art = store.put(src_file(tmp_path), "x.parquet")

    def broken(*_: Any) -> None:
        raise OSError("disk hiccup")

    monkeypatch.setattr(store, "_folders", broken)
    monkeypatch.setattr(store, "_evict", broken)
    store.commit(produced)
    assert store._provisional == set()
    store.get(art.id)


def test_enforce_capacity_tolerates_a_folder_removed_meanwhile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_at(tmp_path / "store")
    a = store.put(src_file(tmp_path, DATA, "a"), "a.parquet")
    b = store.put(src_file(tmp_path, DATA, "b"), "b.parquet")
    delivered_long_ago(store, a.id, b.id)
    ghost = store.root / ("G" * 24)  # listed, then swept by another thread before its stat
    real = store._folders()
    monkeypatch.setattr(store, "_folders", lambda: [ghost, *real])
    store.max_total_bytes = len(DATA) + 200
    store.enforce_capacity(keep=b.id)
    assert folders(store) == {b.id}
    assert a.id not in folders(store)


def test_maybe_sweep_never_fails_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = store_at(tmp_path / "store")

    def broken() -> int:
        raise OSError("disk hiccup")

    monkeypatch.setattr(store, "sweep", broken)
    store.maybe_sweep()  # the paid request it rides on must not become a 500
    assert any("sweep failed" in r.message for r in caplog.records)


# ------------------------------------------------------------------------------ G1: GCS, faked
def gcs_store(
    root: Path, client: FakeClient, *, ttl_s: int = 3600, max_total_bytes: int = 10**9
) -> GcsArtifactStore:
    return GcsArtifactStore(
        root,
        bucket="aimart-test",
        ttl_s=ttl_s,
        max_total_bytes=max_total_bytes,
        secret=SECRET,
        client=client,
    )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Wall-clock time the stores see; tests move it forward by assigning now[0]."""
    now = [time.time()]
    monkeypatch.setattr(artifacts.time, "time", lambda: now[0])
    return now


def test_gcs_sweep_deletes_expired_blobs_not_just_the_local_copy(
    tmp_path: Path, clock: list[float]
) -> None:
    # Before: the sweep removed the local folder only; the blob (and the published promise
    # "deleted 60 minutes after creation") waited for the bucket's 1-day lifecycle rule.
    client = FakeClient()
    store = gcs_store(tmp_path / "cache", client, ttl_s=3600)
    old = store.put(src_file(tmp_path, b"old", "old"), "old.parquet")
    clock[0] += 1800
    young = store.put(src_file(tmp_path, b"young", "young"), "young.parquet")
    clock[0] += 1801  # old is past its TTL, young is not

    assert store.sweep() >= 1
    bucket = client.the_bucket
    assert key(old.id) not in bucket.blobs
    assert key(young.id) in bucket.blobs
    assert (store.root / young.id).exists()
    assert not (store.root / old.id).exists()
    with pytest.raises(ArtifactNotFound):
        gcs_store(tmp_path / "fresh", client).get(old.id)


def test_gcs_request_path_sweep_reaches_the_bucket(tmp_path: Path, clock: list[float]) -> None:
    client = FakeClient()
    store = gcs_store(tmp_path / "cache", client, ttl_s=60)
    art = store.put(src_file(tmp_path), "x.parquet")
    clock[0] += 61
    store.maybe_sweep(interval_s=0)
    assert key(art.id) not in client.the_bucket.blobs


def test_gcs_listing_sweep_catches_blobs_whose_local_record_was_lost(
    tmp_path: Path, clock: list[float]
) -> None:
    client = FakeClient()
    bucket = client.the_bucket
    # The previous instance wrote these, then Cloud Run recycled it (its RAM disk went with it).
    before = gcs_store(tmp_path / "instance-a", client, ttl_s=3600)
    expired = before.put(src_file(tmp_path, b"1", "1"), "1.parquet")
    clock[0] += 3000
    alive = before.put(src_file(tmp_path, b"2", "2"), "2.parquet")
    no_meta = FakeBlob(bucket, key("N" * 24))  # an upload without metadata: aged by creation
    no_meta.upload_from_string(b"x")
    assert no_meta.time_created is not None
    no_meta.time_created -= dt.timedelta(hours=2)
    clock[0] += 1000  # expired is 4000 s old (TTL 3600); alive is 1000 s old

    after = gcs_store(tmp_path / "instance-b", client, ttl_s=3600)
    assert after.sweep() == 2
    assert key(expired.id) not in bucket.blobs
    assert key("N" * 24) not in bucket.blobs
    assert key(alive.id) in bucket.blobs
    assert len(bucket.ops("list")) == 1


def test_gcs_listing_runs_at_most_every_six_hours_across_restarts(
    tmp_path: Path, clock: list[float]
) -> None:
    # Listing is a class-A operation (free tier: 5,000 a month, shared with uploads); the bucket
    # itself remembers when anyone last listed, so a cold start does not list again.
    client = FakeClient()
    bucket = client.the_bucket
    first = gcs_store(tmp_path / "a", client)
    first.sweep()
    first.sweep()
    assert len(bucket.ops("list")) == 1

    for n in range(5):  # five cold starts within the six hours
        clock[0] += 3600
        gcs_store(tmp_path / f"cold{n}", client).sweep()
    assert len(bucket.ops("list")) == 1

    clock[0] += 3601  # six hours since the listing
    survivor = gcs_store(tmp_path / "late", client)
    survivor.sweep()
    assert len(bucket.ops("list")) == 2
    gets_before = len(bucket.ops("get"))
    for _ in range(10):  # a long-lived instance does not even re-read the marker every minute
        clock[0] += 60
        survivor.sweep()
    assert len(bucket.ops("get")) == gets_before
    assert len(bucket.ops("list")) == 2


def test_gcs_get_of_an_expired_blob_deletes_it(tmp_path: Path, clock: list[float]) -> None:
    client = FakeClient()
    art = gcs_store(tmp_path / "a", client, ttl_s=60).put(src_file(tmp_path), "x.parquet")
    clock[0] += 61
    with pytest.raises(ArtifactNotFound, match="expired"):
        gcs_store(tmp_path / "b", client).get(art.id)
    assert key(art.id) not in client.the_bucket.blobs


def test_gcs_delete_removes_the_blob_and_logs_failures(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = FakeClient()
    store = gcs_store(tmp_path / "cache", client)
    art = store.put(src_file(tmp_path), "x.parquet")
    store.delete(art.id)
    assert key(art.id) not in client.the_bucket.blobs
    assert not (store.root / art.id).exists()
    store.delete(art.id)  # NotFound is "already deleted", not an error
    assert not [r for r in caplog.records if r.levelname in {"WARNING", "ERROR"}]

    other = store.put(src_file(tmp_path, name="o"), "o.parquet")
    client.the_bucket.fail_deletes = True
    store.delete(other.id)  # logged, never raised: cleanup must not fail the caller's request
    assert not (store.root / other.id).exists()
    assert any("could not delete" in r.message for r in caplog.records)


def test_gcs_put_never_uploads_from_an_evicted_folder(tmp_path: Path) -> None:
    # Before: the capacity check evicted the folder just written and the upload then crashed
    # with FileNotFoundError (a bare 500).
    client = FakeClient()
    store = gcs_store(tmp_path / "cache", client, max_total_bytes=len(DATA) + 10)
    art = store.put(src_file(tmp_path), "x.parquet")
    assert client.the_bucket.blobs[key(art.id)].data == DATA


def test_gcs_put_refuses_an_output_bigger_than_the_budget(tmp_path: Path) -> None:
    client = FakeClient()
    store = gcs_store(tmp_path / "cache", client, max_total_bytes=10)
    with pytest.raises(InputTooLarge):
        store.put(src_file(tmp_path), "x.parquet")
    assert client.the_bucket.blobs == {}
    assert folders(store) == set()


def test_gcs_upload_failure_is_a_structured_error_and_leaves_nothing(tmp_path: Path) -> None:
    client = FakeClient()
    client.the_bucket.fail_uploads = True
    store = gcs_store(tmp_path / "cache", client)
    with track_puts() as produced, pytest.raises(AimartError) as info:
        store.put(src_file(tmp_path), "x.parquet")
    assert info.value.http_status >= 500
    assert info.value.hint
    assert "not charged" in info.value.hint
    assert folders(store) == set()
    assert produced == []


def test_gcs_signed_link_never_outlives_the_artifact(
    tmp_path: Path, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    store = gcs_store(tmp_path / "cache", client, ttl_s=3600)
    art = store.put(src_file(tmp_path), "x.parquet")
    seen: dict[str, Any] = {}

    class Credentials:
        token = "t"
        service_account_email = "sa@example.iam.gserviceaccount.com"

        def refresh(self, _request: Any) -> None:
            pass

    def signed_url(**kwargs: Any) -> str:
        seen.update(kwargs)
        return "https://storage.example/signed"

    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda: (Credentials(), "p"))
    monkeypatch.setattr(
        FakeBlob, "generate_signed_url", lambda _self, **kw: signed_url(**kw), raising=False
    )
    clock[0] = art.expires_at - 5  # five seconds left
    assert store.download_url("https://h", art) == "https://storage.example/signed"
    assert dt.timedelta(0) < seen["expiration"] <= dt.timedelta(seconds=5)
    assert seen["response_disposition"] == 'attachment; filename="x.parquet"'


def test_gcs_read_failure_is_a_structured_error(tmp_path: Path) -> None:
    # A GCS blip while rehydrating on a cold instance used to escape as a bare 500.
    client = FakeClient()
    art = gcs_store(tmp_path / "a", client).put(src_file(tmp_path), "x.parquet")
    blob = client.the_bucket.blobs[key(art.id)]

    def broken(_filename: str) -> None:
        raise ConnectionError("gcs unreachable")

    blob.download_to_filename = broken  # type: ignore[method-assign]
    cold = gcs_store(tmp_path / "b", client)
    with pytest.raises(AimartError) as info:
        cold.get(art.id)
    assert info.value.http_status == 503
    assert info.value.hint
    assert "not charged" in info.value.hint
    assert folders(cold) == set()  # no half-downloaded cache entry left behind


def test_gcs_rehydration_respects_the_local_budget(tmp_path: Path) -> None:
    client = FakeClient()
    writer = gcs_store(tmp_path / "a", client)
    first = writer.put(src_file(tmp_path, DATA, "1"), "1.parquet")
    second = writer.put(src_file(tmp_path, DATA, "2"), "2.parquet")
    cold = gcs_store(tmp_path / "b", client, max_total_bytes=len(DATA) + 400)
    cold.get(first.id)
    cold.get(second.id)  # the cache holds one: the older copy goes, the blob stays
    assert folders(cold) == {second.id}
    assert key(first.id) in client.the_bucket.blobs
    got, path = cold.get(first.id)
    assert got.id == first.id
    assert path.read_bytes() == DATA


def test_gcs_blob_of_an_evicted_cache_copy_is_deleted_at_its_ttl(
    tmp_path: Path, clock: list[float]
) -> None:
    # Capacity eviction drops only the cached copy. Before, the request-path sweep found
    # expired artifacts by their folders, so such a blob waited for the six-hourly listing.
    client = FakeClient()
    bucket = client.the_bucket
    store = gcs_store(tmp_path / "cache", client, ttl_s=3600, max_total_bytes=len(DATA) + 400)
    first = store.put(src_file(tmp_path, DATA, "1"), "1.parquet")
    clock[0] += 10
    second = store.put(src_file(tmp_path, DATA, "2"), "2.parquet")
    assert folders(store) == {second.id}  # first's cached copy made room...
    assert key(first.id) in bucket.blobs  # ...and its blob is still served until the TTL
    store.sweep()  # the listing sweep runs now, then not again for six hours
    assert key(first.id) in bucket.blobs

    clock[0] += 3595  # first is past its TTL, second (10 s younger) is not
    assert store.sweep() == 1
    assert key(first.id) not in bucket.blobs
    assert key(second.id) in bucket.blobs
    assert len(bucket.ops("list")) == 1  # found without listing the bucket again
    assert store._uncached == {}


def test_gcs_a_recached_or_deleted_artifact_is_no_longer_tracked_as_evicted(
    tmp_path: Path,
) -> None:
    client = FakeClient()
    store = gcs_store(tmp_path / "cache", client, max_total_bytes=len(DATA) + 400)
    first = store.put(src_file(tmp_path, DATA, "1"), "1.parquet")
    second = store.put(src_file(tmp_path, DATA, "2"), "2.parquet")
    assert set(store._uncached) == {first.id}
    store.get(first.id)  # rehydrated: its folder is back (and second's copy made room)
    assert set(store._uncached) == {second.id}
    store.delete(second.id)
    assert store._uncached == {}
    assert key(second.id) not in client.the_bucket.blobs


def test_a_failed_local_write_is_a_structured_error_and_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # e.g. the RAM-backed disk is full: before, a bare OSError (a 500) and a half-written folder.
    import errno

    store = store_at(tmp_path / "store")

    def full_disk(*_: Any) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(artifacts.shutil, "move", full_disk)
    with track_puts() as produced, pytest.raises(AimartError) as info:
        store.put(src_file(tmp_path), "x.parquet")
    assert info.value.http_status == 503
    assert info.value.hint
    assert "not charged" in info.value.hint
    assert folders(store) == set()
    assert produced == []


def test_sweep_tolerates_a_folder_removed_meanwhile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    # The background sweeper and the request-path sweep run in different threads.
    store = store_at(tmp_path / "store", ttl_s=60)
    art = store.put(src_file(tmp_path), "x.parquet")
    ghost = store.root / ("G" * 24)  # listed, then removed before its meta.json was read
    real = store._folders()
    monkeypatch.setattr(store, "_folders", lambda: [ghost, *real])
    clock[0] += 61
    assert store.sweep() == 1
    assert art.id not in folders(store)
