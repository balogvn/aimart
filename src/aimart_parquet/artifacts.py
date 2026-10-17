"""Ephemeral store for converted files, addressed by unguessable IDs.

Conversions return a reference, not the bytes: base64 Parquet in a tool result would cost the
agent thousands of tokens to carry around, defeating the point of Parquet. The agent instead
gets an ``artifact_id`` it can pass to inspect/preview/query, plus a signed download link.

Two backends behind one interface:

* ``LocalArtifactStore`` — disk with a TTL. Free everywhere; not durable across restarts.
* ``GcsArtifactStore`` — Google Cloud Storage, for Cloud Run, whose disk is RAM and vanishes
  when the service scales to zero (a link handed out a minute earlier would 404). Downloads
  are V4 signed URLs served by GCS itself: 100 GB/month of free egress versus Cloud Run's 1 GB.
  A bucket lifecycle rule (1 day) is the backstop for cleanup.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
import shutil
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Protocol

from aimart_parquet.errors import ArtifactNotFound

if TYPE_CHECKING:
    from pathlib import Path

_ID = re.compile(r"^[A-Za-z0-9_-]{22,64}$")


@dataclass(frozen=True)
class Artifact:
    id: str
    filename: str
    size_bytes: int
    sha256: str
    created_at: float
    expires_at: float

    @property
    def expires_iso(self) -> str:
        return dt.datetime.fromtimestamp(self.expires_at, dt.UTC).replace(microsecond=0).isoformat()


class ArtifactStore(Protocol):
    def put(self, src: Path, filename: str) -> Artifact: ...
    def get(self, artifact_id: str) -> tuple[Artifact, Path]: ...
    def download_url(self, base_url: str, art: Artifact) -> str: ...
    def verify(self, artifact_id: str, expires_at: int, signature: str) -> bool: ...
    def sweep(self) -> int: ...
    def maybe_sweep(self) -> None: ...


class LocalArtifactStore:
    def __init__(self, root: Path, *, ttl_s: int, max_total_bytes: int, secret: bytes) -> None:
        self.root = root
        self.ttl_s = ttl_s
        self.max_total_bytes = max_total_bytes
        self._secret = secret
        self._last_sweep = 0.0
        root.mkdir(parents=True, exist_ok=True)

    def maybe_sweep(self, interval_s: float = 60.0) -> None:
        """Sweep at most once per interval, on the request path.

        Request-billed hosts (Cloud Run) throttle CPU between requests, so a background
        sweeper may never run; cleanup must also ride on real traffic.
        """
        now = time.monotonic()
        if now - self._last_sweep >= interval_s:
            self._last_sweep = now
            self.sweep()

    # ------------------------------------------------------------------ lifecycle
    def new_id(self) -> str:
        return secrets.token_urlsafe(18)

    def put(self, src: Path, filename: str) -> Artifact:
        artifact_id = self.new_id()
        folder = self.root / artifact_id
        folder.mkdir(parents=True)
        dst = folder / "data.parquet"
        shutil.move(src, dst)
        digest = hashlib.sha256()
        with dst.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        now = time.time()
        art = Artifact(
            id=artifact_id,
            filename=filename,
            size_bytes=dst.stat().st_size,
            sha256=digest.hexdigest(),
            created_at=now,
            expires_at=now + self.ttl_s,
        )
        tmp = folder / "meta.json.tmp"
        tmp.write_text(json.dumps(asdict(art)))
        tmp.replace(folder / "meta.json")
        self.enforce_capacity()
        return art

    def get(self, artifact_id: str) -> tuple[Artifact, Path]:
        if not _ID.match(artifact_id):
            raise ArtifactNotFound("Malformed artifact_id.", hint="Use the id returned by convert.")
        folder = self.root / artifact_id
        try:
            art = Artifact(**json.loads((folder / "meta.json").read_text()))
        except (OSError, ValueError, TypeError) as exc:
            raise ArtifactNotFound(
                "Unknown or expired artifact_id.",
                hint="Artifacts expire after a TTL; convert the file again.",
            ) from exc
        if art.expires_at < time.time():
            self._remove(folder)
            raise ArtifactNotFound(
                "The artifact has expired.", hint="Convert the file again to get a new artifact_id."
            )
        return art, folder / "data.parquet"

    def sweep(self) -> int:
        """Delete expired artifacts. Returns how many were removed."""
        removed = 0
        now = time.time()
        for folder in self._folders():
            try:
                expires = json.loads((folder / "meta.json").read_text())["expires_at"]
            except (OSError, ValueError, KeyError):
                expires = folder.stat().st_mtime + self.ttl_s  # half-written: age out by mtime
            if expires < now:
                self._remove(folder)
                removed += 1
        return removed

    def enforce_capacity(self) -> None:
        """Evict oldest artifacts until total size fits the budget (free disks are small)."""
        folders = sorted(self._folders(), key=lambda f: f.stat().st_mtime)
        sizes = {f: sum(p.stat().st_size for p in f.iterdir() if p.is_file()) for f in folders}
        total = sum(sizes.values())
        for folder in folders:
            if total <= self.max_total_bytes:
                break
            total -= sizes[folder]
            self._remove(folder)

    def _folders(self) -> list[Path]:
        return [p for p in self.root.iterdir() if p.is_dir() and _ID.match(p.name)]

    @staticmethod
    def _remove(folder: Path) -> None:
        shutil.rmtree(folder, ignore_errors=True)

    # ------------------------------------------------------------------ signed links
    def sign(self, artifact_id: str, expires_at: float) -> str:
        msg = f"{artifact_id}.{int(expires_at)}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()[:32]

    def verify(self, artifact_id: str, expires_at: int, signature: str) -> bool:
        if expires_at < time.time():
            return False
        return hmac.compare_digest(self.sign(artifact_id, expires_at), signature)

    def download_url(self, base_url: str, art: Artifact) -> str:
        exp = int(art.expires_at)
        return f"{base_url}/v1/artifacts/{art.id}/{art.filename}?exp={exp}&sig={self.sign(art.id, exp)}"


class GcsArtifactStore(LocalArtifactStore):
    """Local cache + GCS durability. Requires the ``gcs`` extra and, for signed URLs, that the
    runtime service account may sign as itself (roles/iam.serviceAccountTokenCreator on itself).
    """

    def __init__(
        self,
        root: Path,
        *,
        bucket: str,
        ttl_s: int,
        max_total_bytes: int,
        secret: bytes,
        client: Any = None,
    ) -> None:
        super().__init__(root, ttl_s=ttl_s, max_total_bytes=max_total_bytes, secret=secret)
        if client is None:
            from google.cloud import storage  # optional dependency: aimart-parquet[gcs]

            client = storage.Client()
        self._bucket = client.bucket(bucket)

    @staticmethod
    def _key(artifact_id: str) -> str:
        return f"artifacts/{artifact_id}/data.parquet"

    def put(self, src: Path, filename: str) -> Artifact:
        art = super().put(src, filename)
        blob = self._bucket.blob(self._key(art.id))
        blob.metadata = {k: str(v) for k, v in asdict(art).items()}
        blob.upload_from_filename(
            str(self.root / art.id / "data.parquet"), content_type="application/vnd.apache.parquet"
        )
        return art

    def get(self, artifact_id: str) -> tuple[Artifact, Path]:
        try:
            return super().get(artifact_id)
        except ArtifactNotFound:
            if not _ID.match(artifact_id):
                raise
        blob = self._bucket.get_blob(self._key(artifact_id))
        if blob is None or not blob.metadata:
            raise ArtifactNotFound(
                "Unknown or expired artifact_id.", hint="Convert the file again."
            )
        meta = blob.metadata
        art = Artifact(
            id=artifact_id,
            filename=meta["filename"],
            size_bytes=int(meta["size_bytes"]),
            sha256=meta["sha256"],
            created_at=float(meta["created_at"]),
            expires_at=float(meta["expires_at"]),
        )
        if art.expires_at < time.time():
            raise ArtifactNotFound("The artifact has expired.", hint="Convert the file again.")
        folder = self.root / artifact_id
        folder.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(folder / "data.parquet"))
        tmp = folder / "meta.json.tmp"
        tmp.write_text(json.dumps(asdict(art)))
        tmp.replace(folder / "meta.json")
        return art, folder / "data.parquet"

    def download_url(self, base_url: str, art: Artifact) -> str:
        import datetime as _dt

        import google.auth
        from google.auth.transport import requests as grequests

        credentials, _ = google.auth.default()
        credentials.refresh(grequests.Request())  # type: ignore[no-untyped-call]
        blob = self._bucket.blob(self._key(art.id))
        return str(
            blob.generate_signed_url(
                version="v4",
                expiration=_dt.timedelta(seconds=max(60, int(art.expires_at - time.time()))),
                method="GET",
                service_account_email=getattr(credentials, "service_account_email", None),
                access_token=credentials.token,
                response_disposition=f'attachment; filename="{art.filename}"',
            )
        )


def build_store(settings: Any) -> ArtifactStore:
    secret = settings.signing_secret.get_secret_value().encode()
    if getattr(settings, "gcs_bucket", None):
        return GcsArtifactStore(
            settings.artifact_dir,
            bucket=settings.gcs_bucket,
            ttl_s=settings.artifact_ttl_s,
            max_total_bytes=settings.artifact_max_total_bytes,
            secret=secret,
        )
    return LocalArtifactStore(
        settings.artifact_dir,
        ttl_s=settings.artifact_ttl_s,
        max_total_bytes=settings.artifact_max_total_bytes,
        secret=secret,
    )
