"""Artifact store fixes: the output budget (M1), deleting withheld outputs (M2), and actually
deleting expired GCS blobs at the TTL (G1), against a fake bucket that records every call."""

from __future__ import annotations

import datetime as dt
import time
from typing import TYPE_CHECKING, Any

import pytest

from aimart_parquet import artifacts
from aimart_parquet.artifacts import GcsArtifactStore, LocalArtifactStore, track_puts
from aimart_parquet.errors import AimartError, ArtifactNotFound, InputTooLarge

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
class NotFoundError(Exception):
    code = 404  # google.api_core.exceptions.NotFound carries the HTTP status as `code`


class FakeBlob:
    def __init__(self, bucket: FakeBucket, name: str) -> None:
        self.bucket = bucket
        self.name = name
        self.metadata: dict[str, str] | None = None
        self.data: bytes | None = None
        self.time_created: dt.datetime | None = None

    def upload_from_filename(self, filename: str, content_type: str | None = None) -> None:
        if self.bucket.fail_uploads:
            raise ConnectionError("gcs unreachable")
        with open(filename, "rb") as fh:  # noqa: PTH123 - mirrors the GCS client signature
            self._store(fh.read())

    def upload_from_string(self, data: bytes | str, content_type: str | None = None) -> None:
        self._store(data.encode() if isinstance(data, str) else data)

    def _store(self, data: bytes) -> None:
        self.bucket.calls.append(("insert", self.name))
        self.data = data
        self.time_created = dt.datetime.now(dt.UTC)
        self.bucket.blobs[self.name] = self

    def download_to_filename(self, filename: str) -> None:
        assert self.data is not None
        with open(filename, "wb") as fh:  # noqa: PTH123
            fh.write(self.data)


class FakeBucket:
    """Records operations by GCS billing class: list (class A), get (class B), delete (free)."""

    def __init__(self) -> None:
        self.blobs: dict[str, FakeBlob] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_uploads = False
        self.fail_deletes = False

    def blob(self, name: str) -> FakeBlob:
        return self.blobs.get(name) or FakeBlob(self, name)

    def get_blob(self, name: str) -> FakeBlob | None:
        self.calls.append(("get", name))
        return self.blobs.get(name)

    def delete_blob(self, name: str) -> None:
        self.calls.append(("delete", name))
        if self.fail_deletes:
            raise ConnectionError("gcs unreachable")
        if name not in self.blobs:
            raise NotFoundError(name)
        del self.blobs[name]

    def list_blobs(self, prefix: str | None = None, **_: Any) -> list[FakeBlob]:
        self.calls.append(("list", prefix or ""))
        return [b for n, b in list(self.blobs.items()) if n.startswith(prefix or "")]

    def ops(self, kind: str) -> list[str]:
        return [name for k, name in self.calls if k == kind]


class FakeClient:
    def __init__(self) -> None:
        self.the_bucket = FakeBucket()

    def bucket(self, _name: str) -> FakeBucket:
        return self.the_bucket


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


def key(artifact_id: str) -> str:
    return f"artifacts/{artifact_id}/data.parquet"


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
