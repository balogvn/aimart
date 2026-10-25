"""A fake google-cloud-storage client for GcsArtifactStore tests, shared by the money tests.

Imported as ``fixtures.money.fake_gcs`` (``tests/conftest.py`` puts ``tests/`` on sys.path).
The bucket records every operation by GCS billing class: list (class A), get (class B), delete
(free), so tests can assert what a sweep costs.
"""

from __future__ import annotations

import datetime as dt
from typing import Any


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
        self.bucket.calls.append(("download", self.name))
        with open(filename, "wb") as fh:  # noqa: PTH123
            fh.write(self.data)


class FakeBucket:
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


def key(artifact_id: str) -> str:
    return f"artifacts/{artifact_id}/data.parquet"
