"""Artifact stores: TTL, sweeping, capacity, ids, signed links, and the GCS backend (faked)."""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import TYPE_CHECKING, Any

import pytest

from aimart_parquet import artifacts
from aimart_parquet.artifacts import GcsArtifactStore, LocalArtifactStore, build_store
from aimart_parquet.errors import ArtifactNotFound

if TYPE_CHECKING:
    from pathlib import Path

SECRET = b"test-secret"


def store_at(root: Path, *, ttl_s: int = 3600, max_total_bytes: int = 10**9) -> LocalArtifactStore:
    return LocalArtifactStore(root, ttl_s=ttl_s, max_total_bytes=max_total_bytes, secret=SECRET)


def src_file(tmp_path: Path, data: bytes = b"PAR1-fake-parquet-PAR1", name: str = "src") -> Path:
    path = tmp_path / "incoming" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def age(folder: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(folder, (then, then))


# ------------------------------------------------------------------------------ put / get
def test_put_moves_hashes_and_records_metadata(tmp_path: Path) -> None:
    store = store_at(tmp_path / "store", ttl_s=600)
    data = b"some parquet bytes"
    src = src_file(tmp_path, data)
    before = time.time()
    art = store.put(src, "sales.parquet")

    assert not src.exists()  # moved, not copied
    assert len(art.id) >= 22
    assert art.filename == "sales.parquet"
    assert art.size_bytes == len(data)
    assert art.sha256 == hashlib.sha256(data).hexdigest()
    assert before <= art.created_at <= time.time()
    assert art.expires_at == pytest.approx(art.created_at + 600)
    assert art.expires_iso.endswith("+00:00")
    assert "." not in art.expires_iso  # whole seconds

    got, path = store.get(art.id)
    assert got == art
    assert path.read_bytes() == data
    meta = json.loads((store.root / art.id / "meta.json").read_text())
    assert meta["sha256"] == art.sha256
    assert not (store.root / art.id / "meta.json.tmp").exists()


def test_ids_are_unique_and_unguessable(tmp_path: Path) -> None:
    store = store_at(tmp_path / "store")
    ids = {store.put(src_file(tmp_path, name=f"s{i}"), "x.parquet").id for i in range(20)}
    assert len(ids) == 20
    assert all(artifacts._ID.match(i) for i in ids)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "short",
        "../../etc/passwd",
        "a/b" * 10,
        "x" * 65,
        "has space" * 4,
        "..%2f" * 6,
        "Zz" * 11 + ".",
    ],
)
def test_malformed_ids_are_rejected_without_touching_disk(tmp_path: Path, bad: str) -> None:
    store = store_at(tmp_path / "store")
    with pytest.raises(ArtifactNotFound, match="Malformed"):
        store.get(bad)


def test_unknown_id_is_not_found(tmp_path: Path) -> None:
    store = store_at(tmp_path / "store")
    with pytest.raises(ArtifactNotFound, match="Unknown or expired"):
        store.get("Zz" * 12)


def test_corrupt_metadata_is_not_found(tmp_path: Path) -> None:
    store = store_at(tmp_path / "store")
    art = store.put(src_file(tmp_path), "x.parquet")
    (store.root / art.id / "meta.json").write_text("{not json")
    with pytest.raises(ArtifactNotFound, match="Unknown or expired"):
        store.get(art.id)
    (store.root / art.id / "meta.json").write_text('{"id": "x"}')  # wrong shape
    with pytest.raises(ArtifactNotFound):
        store.get(art.id)


def test_expired_artifact_is_removed_on_read(tmp_path: Path) -> None:
    store = store_at(tmp_path / "store", ttl_s=-1)
    art = store.put(src_file(tmp_path), "x.parquet")
    with pytest.raises(ArtifactNotFound, match="expired"):
        store.get(art.id)
    assert not (store.root / art.id).exists()


# ------------------------------------------------------------------------------ sweep
def test_sweep_removes_expired_and_stale_half_written_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "store"
    live = store_at(root, ttl_s=3600)
    dead = store_at(root, ttl_s=-1)
    keep = live.put(src_file(tmp_path, name="a"), "keep.parquet")
    gone = dead.put(src_file(tmp_path, name="b"), "gone.parquet")

    stale = root / ("S" * 22)  # crashed mid-put: no meta.json, old
    stale.mkdir()
    (stale / "data.parquet").write_bytes(b"x")
    age(stale, 7200)
    fresh = root / ("F" * 22)  # being written right now: no meta.json yet, new
    fresh.mkdir()
    workdir = root / ".work"  # not an artifact id: never touched
    workdir.mkdir()
    age(workdir, 10**6)

    assert live.sweep() == 2
    assert (root / keep.id).exists()
    assert not (root / gone.id).exists()
    assert not stale.exists()
    assert fresh.exists()
    assert workdir.exists()
    assert live.sweep() == 0


def test_maybe_sweep_is_throttled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = store_at(tmp_path / "store")
    calls: list[int] = []
    monkeypatch.setattr(store, "sweep", lambda: calls.append(1) or 0)
    store.maybe_sweep()
    store.maybe_sweep()
    store.maybe_sweep(interval_s=3600)
    assert len(calls) == 1
    store.maybe_sweep(interval_s=0)
    assert len(calls) == 2


# ------------------------------------------------------------------------------ capacity
def test_capacity_evicts_oldest_first(tmp_path: Path) -> None:
    store = store_at(tmp_path / "store")
    first = store.put(src_file(tmp_path, b"a" * 1000, "1"), "1.parquet")
    one_size = sum(p.stat().st_size for p in (store.root / first.id).iterdir())
    second = store.put(src_file(tmp_path, b"b" * 1000, "2"), "2.parquet")
    # Older than the minimum residency: a just-delivered output is never evicted.
    age(store.root / first.id, artifacts.MIN_RESIDENCY_S + 300)
    age(store.root / second.id, artifacts.MIN_RESIDENCY_S + 200)

    store.max_total_bytes = 2 * one_size + 10  # room for two, not three
    third = store.put(src_file(tmp_path, b"c" * 1000, "3"), "3.parquet")

    assert not (store.root / first.id).exists()
    assert (store.root / second.id).exists()
    assert (store.root / third.id).exists()
    store.get(third.id)


def test_capacity_never_evicts_within_budget(tmp_path: Path) -> None:
    store = store_at(tmp_path / "store")
    ids = [store.put(src_file(tmp_path, b"z" * 100, str(i)), "x.parquet").id for i in range(5)]
    assert all((store.root / i).exists() for i in ids)


# ------------------------------------------------------------------------------ signed links
def test_signed_links_verify_and_reject_tampering(tmp_path: Path) -> None:
    store = store_at(tmp_path / "store")
    art = store.put(src_file(tmp_path), "sales.parquet")
    url = store.download_url("https://h.example", art)
    exp = int(art.expires_at)
    sig = store.sign(art.id, exp)
    assert url == f"https://h.example/v1/artifacts/{art.id}/sales.parquet?exp={exp}&sig={sig}"
    assert len(sig) == 32

    assert store.verify(art.id, exp, sig)
    assert not store.verify(art.id, exp + 1, sig)  # extended expiry
    assert not store.verify(art.id[:-1] + ("A" if art.id[-1] != "A" else "B"), exp, sig)
    assert not store.verify(art.id, exp, sig[:-1] + ("0" if sig[-1] != "0" else "1"))
    assert not store.verify(art.id, exp, "")

    other = LocalArtifactStore(tmp_path / "other", ttl_s=60, max_total_bytes=10**9, secret=b"x")
    assert not other.verify(art.id, exp, sig)  # another deployment's key


def test_expired_signatures_do_not_verify(tmp_path: Path) -> None:
    store = store_at(tmp_path / "store")
    past = int(time.time()) - 5
    assert not store.verify("Zz" * 12, past, store.sign("Zz" * 12, past))


# ------------------------------------------------------------------------------ GCS, faked client
class FakeBlob:
    def __init__(self, bucket: FakeBucket, key: str) -> None:
        self.bucket = bucket
        self.key = key
        self.metadata: dict[str, str] | None = None
        self.data: bytes | None = None
        self.content_type: str | None = None

    def upload_from_filename(self, filename: str, content_type: str | None = None) -> None:
        with open(filename, "rb") as fh:  # noqa: PTH123 - mirrors the GCS client signature
            self.data = fh.read()
        self.content_type = content_type
        self.bucket.blobs[self.key] = self

    def download_to_filename(self, filename: str) -> None:
        assert self.data is not None
        with open(filename, "wb") as fh:  # noqa: PTH123
            fh.write(self.data)
        self.bucket.downloads.append(self.key)


class FakeBucket:
    def __init__(self, name: str) -> None:
        self.name = name
        self.blobs: dict[str, FakeBlob] = {}
        self.downloads: list[str] = []

    def blob(self, key: str) -> FakeBlob:
        return self.blobs.get(key) or FakeBlob(self, key)

    def get_blob(self, key: str) -> FakeBlob | None:
        return self.blobs.get(key)


class FakeClient:
    def __init__(self) -> None:
        self.buckets: dict[str, FakeBucket] = {}

    def bucket(self, name: str) -> FakeBucket:
        return self.buckets.setdefault(name, FakeBucket(name))


def gcs_store(root: Path, client: FakeClient, *, ttl_s: int = 3600) -> GcsArtifactStore:
    return GcsArtifactStore(
        root, bucket="aimart-test", ttl_s=ttl_s, max_total_bytes=10**9, secret=SECRET, client=client
    )


def test_gcs_put_uploads_with_metadata(tmp_path: Path) -> None:
    client = FakeClient()
    store = gcs_store(tmp_path / "cache", client)
    art = store.put(src_file(tmp_path, b"bytes!"), "s.parquet")
    blob = client.buckets["aimart-test"].blobs[f"artifacts/{art.id}/data.parquet"]
    assert blob.data == b"bytes!"
    assert blob.content_type == "application/vnd.apache.parquet"
    assert blob.metadata == {
        "id": art.id,
        "filename": "s.parquet",
        "size_bytes": "6",
        "sha256": art.sha256,
        "created_at": str(art.created_at),
        "expires_at": str(art.expires_at),
    }


def test_gcs_get_prefers_the_local_cache(tmp_path: Path) -> None:
    client = FakeClient()
    store = gcs_store(tmp_path / "cache", client)
    art = store.put(src_file(tmp_path), "s.parquet")
    got, _ = store.get(art.id)
    assert got == art
    assert client.buckets["aimart-test"].downloads == []


def test_gcs_get_rehydrates_after_the_instance_restarts(tmp_path: Path) -> None:
    # Cloud Run's disk is RAM: a new instance has an empty cache but the bucket remembers.
    client = FakeClient()
    art = gcs_store(tmp_path / "cache-a", client).put(src_file(tmp_path, b"durable"), "s.parquet")
    fresh = gcs_store(tmp_path / "cache-b", client)
    got, path = fresh.get(art.id)
    assert got.id == art.id
    assert got.sha256 == art.sha256
    assert got.size_bytes == 7
    assert got.expires_at == pytest.approx(art.expires_at)
    assert path.read_bytes() == b"durable"
    assert client.buckets["aimart-test"].downloads == [f"artifacts/{art.id}/data.parquet"]
    fresh.get(art.id)  # now cached
    assert len(client.buckets["aimart-test"].downloads) == 1


def test_gcs_unknown_and_metadata_less_blobs_are_not_found(tmp_path: Path) -> None:
    client = FakeClient()
    store = gcs_store(tmp_path / "cache", client)
    with pytest.raises(ArtifactNotFound, match="Unknown or expired"):
        store.get("Zz" * 12)
    bucket = client.bucket("aimart-test")
    orphan = FakeBlob(bucket, f"artifacts/{'Yy' * 12}/data.parquet")
    orphan.data = b"x"
    bucket.blobs[orphan.key] = orphan
    with pytest.raises(ArtifactNotFound, match="Unknown or expired"):
        store.get("Yy" * 12)


def test_gcs_expired_blob_is_not_served(tmp_path: Path) -> None:
    client = FakeClient()
    art = gcs_store(tmp_path / "a", client, ttl_s=-1).put(src_file(tmp_path), "s.parquet")
    with pytest.raises(ArtifactNotFound, match="expired"):
        gcs_store(tmp_path / "b", client).get(art.id)
    assert client.buckets["aimart-test"].downloads == []


def test_gcs_malformed_id_never_reaches_the_bucket(tmp_path: Path) -> None:
    client = FakeClient()
    store = gcs_store(tmp_path / "cache", client)

    def boom(_: str) -> Any:
        raise AssertionError("bucket queried for a malformed id")

    client.bucket("aimart-test").get_blob = boom  # type: ignore[method-assign]
    with pytest.raises(ArtifactNotFound, match="Malformed"):
        store.get("../../secrets")


# ------------------------------------------------------------------------------ factory
def test_build_store_picks_the_backend(make_settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    local = build_store(make_settings())
    assert type(local) is LocalArtifactStore
    assert local.ttl_s == 3600

    clients: list[FakeClient] = []

    def fake_client() -> FakeClient:
        clients.append(FakeClient())
        return clients[-1]

    from google.cloud import storage

    monkeypatch.setattr(storage, "Client", fake_client)
    gcs = build_store(make_settings(gcs_bucket="my-bucket", artifact_ttl_s=60))
    assert isinstance(gcs, GcsArtifactStore)
    assert gcs.ttl_s == 60
    assert list(clients[0].buckets) == ["my-bucket"]
