"""Ephemeral store for converted files, addressed by unguessable IDs.

Conversions return a reference, not the bytes: base64 Parquet in a tool result would cost the
agent thousands of tokens to carry around, defeating the point of Parquet. The agent instead
gets an ``artifact_id`` it can pass to inspect/preview/query, plus a signed download link.

Two backends behind one interface:

* ``LocalArtifactStore`` — disk with a TTL. Free everywhere; not durable across restarts.
* ``GcsArtifactStore`` — Google Cloud Storage, for Cloud Run, whose disk is RAM and vanishes
  when the service scales to zero (a link handed out a minute earlier would 404). Downloads
  are V4 signed URLs served by GCS itself: 100 GB/month of free egress versus Cloud Run's 1 GB.

Retention is a published promise (/pricing, llms.txt, the privacy policy): every artifact
becomes unreachable at its TTL. Reads check the expiry and signed links expire with the
artifact; the request-path sweep (at most once a minute) deletes expired files on disk and
their GCS blobs; blobs whose local record was lost with an instance are found by a bucket
listing at most every six hours; the bucket's 1-day lifecycle rule is the final backstop.

Money: ``put`` raising an ``AimartError`` is what keeps a caller from paying for an output
that could not be kept. The budget (``max_total_bytes``) counts every stored output, and three
kinds are never evicted to make room: an output whose paid call has not settled yet
(provisional, inside ``track_puts``; the payment layer then commits or deletes it), an output
delivered less than ``MIN_RESIDENCY_S`` ago, and an artifact a running call is reading
(``hold``). An output that cannot fit without evicting one of those is refused before it is
stored (503 busy), so nobody pays for a link that dies a moment later.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import secrets
import shutil
import threading
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Protocol

from aimart_parquet.errors import AimartError, ArtifactNotFound, Busy, InputTooLarge

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

log = logging.getLogger("aimart.artifacts")

_ID = re.compile(r"^[A-Za-z0-9_-]{22,64}$")
# How long a delivered output is safe from capacity eviction. Two concurrent paid converts
# that each fit the budget, but not together: the second one's commit evicted the first, whose
# caller had been charged a moment earlier for a download_url that now 404'd.
MIN_RESIDENCY_S = 600
# Listing is a class-A GCS operation (free tier: 5,000 a month, shared with every upload), so
# the sweep for orphaned blobs is rationed across all instances by a marker in the bucket.
LISTING_INTERVAL_S = 6 * 3600
_LISTING_MARKER = "sweeps/last-listing"  # outside artifacts/: never listed, never swept


class StorageUnavailable(AimartError):
    code = "storage_unavailable"
    http_status = 503


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
    def hold(self) -> contextlib.AbstractContextManager[None]: ...
    def commit(self, artifact_ids: Iterable[str]) -> None: ...
    def delete(self, artifact_id: str) -> None: ...
    def download_url(self, base_url: str, art: Artifact) -> str: ...
    def verify(self, artifact_id: str, expires_at: int, signature: str) -> bool: ...
    def sweep(self) -> int: ...
    def maybe_sweep(self) -> None: ...


# ---------------------------------------------------------------------- outputs of one call
_PUTS: contextvars.ContextVar[tuple[list[str], ...]] = contextvars.ContextVar(
    "aimart_puts", default=()
)


@contextlib.contextmanager
def track_puts() -> Iterator[list[str]]:
    """Collect the ids of artifacts stored inside the block, including by tasks it starts.

    The payment layers wrap a paid call in this. What is stored inside stays provisional (it
    counts toward the budget but is never evicted) until they call ``store.commit(ids)`` (paid)
    or ``discard_withheld`` (not paid): an output whose settlement then failed used to stay in
    the budget until its TTL, evicting paying customers' artifacts. An inner block reports to
    the outer ones too.
    """
    produced: list[str] = []
    token = _PUTS.set((*_PUTS.get(), produced))
    try:
        yield produced
    finally:
        _PUTS.reset(token)


def outputs_of(store: ArtifactStore | None) -> contextlib.AbstractContextManager[list[str]]:
    """``track_puts()`` for a paid call whose outputs will be committed to or deleted from
    ``store``. Without a store nothing would ever commit them, and a provisional artifact is
    never evicted, so nothing is tracked at all."""
    return track_puts() if store is not None else contextlib.nullcontext([])


# The store and the ids a running call has read (see LocalArtifactStore.hold).
_HELD: contextvars.ContextVar[tuple[LocalArtifactStore, list[str]] | None] = contextvars.ContextVar(
    "aimart_held", default=None
)


def _record(artifact_id: str) -> bool:
    """Report a stored artifact to the enclosing blocks; True if it is provisional."""
    blocks = _PUTS.get()
    for produced in blocks:
        produced.append(artifact_id)
    return bool(blocks)


def discard_withheld(store: ArtifactStore, artifact_ids: Iterable[str], *, reason: str) -> None:
    """Delete outputs whose result was not delivered. Logs, never raises: the caller is owed a
    clean error response (and no charge), not a 500 about our cleanup."""
    for artifact_id in artifact_ids:
        try:
            store.delete(artifact_id)
        except Exception:
            log.exception(
                "could not delete a withheld output",
                extra={"artifact_id": artifact_id, "reason": reason},
            )
        else:
            log.info(
                "withheld output deleted", extra={"artifact_id": artifact_id, "reason": reason}
            )


def _human(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB" if n >= 1024 * 1024 else f"{n} bytes"


class LocalArtifactStore:
    # Evicting a local artifact deletes it for good (see MIN_RESIDENCY_S).
    min_residency_s: float = MIN_RESIDENCY_S

    def __init__(self, root: Path, *, ttl_s: int, max_total_bytes: int, secret: bytes) -> None:
        self.root = root
        self.ttl_s = ttl_s
        self.max_total_bytes = max_total_bytes
        self._secret = secret
        self._last_sweep = 0.0
        # Stored during a paid call whose settlement is not known yet (see track_puts).
        self._provisional: set[str] = set()
        # id -> how many running calls are reading it (see hold).
        self._pins: dict[str, int] = {}
        root.mkdir(parents=True, exist_ok=True)

    def maybe_sweep(self, interval_s: float = 60.0) -> None:
        """Sweep at most once per interval, on the request path.

        Request-billed hosts (Cloud Run) throttle CPU between requests, so a background
        sweeper may never run; cleanup must also ride on real traffic. It must never fail
        the paid request it rides on, so errors are logged and the request goes on.
        """
        now = time.monotonic()
        if now - self._last_sweep >= interval_s:
            self._last_sweep = now
            try:
                self.sweep()
            except Exception:
                log.exception("artifact sweep failed")

    # ------------------------------------------------------------------ lifecycle
    def new_id(self) -> str:
        return secrets.token_urlsafe(18)

    def put(self, src: Path, filename: str) -> Artifact:
        size = src.stat().st_size
        if size > self.max_total_bytes:
            # Storing it anyway made enforce_capacity() evict it (and every older artifact)
            # straight away: the caller got a 200, paid, and held a download_url that 404'd.
            raise InputTooLarge(
                f"The output exceeds the artifact budget: {_human(size)}, and this server "
                f"keeps at most {_human(self.max_total_bytes)} of outputs.",
                hint="Send a smaller extract (fewer rows or columns, or split the file) so "
                "the output fits; you were not charged.",
                output_bytes=size,
                limit_bytes=self.max_total_bytes,
            )
        # Room is made before the file lands, and counting every output (unsettled ones too):
        # leaving those out let concurrent paid calls hold a multiple of the RAM disk's budget.
        if not self._make_room(size, strict=True):
            raise Busy(
                "The server's output storage is full right now: recent outputs, and outputs "
                "of calls still settling, are kept for their callers.",
                hint="Retry in a few minutes, or send a smaller extract; you were not charged.",
                output_bytes=size,
                limit_bytes=self.max_total_bytes,
            )
        art = self._store_local(src, filename)
        self._persist(art)
        if _record(art.id):
            self._provisional.add(art.id)  # never evicted until the payment layer commits it
        return art

    def commit(self, artifact_ids: Iterable[str]) -> None:
        """The call that stored these was paid for: from now on they age like any output.
        Bookkeeping only (their bytes were counted when stored), so it cannot fail a paid call."""
        self._provisional.difference_update(artifact_ids)

    @contextlib.contextmanager
    def hold(self) -> Iterator[None]:
        """Keep every artifact ``get`` returns inside the block on disk until the block exits.

        A call reads its inputs by path, after it has fetched all of them (a query names up to
        8). On GCS, rehydrating a query's second table evicted the cached copy of its first, so
        a join of artifacts that together exceed the local budget always failed. A held artifact
        is neither evicted nor swept; the cache may exceed the budget while a call needs more,
        and is trimmed back when the call ends.
        """
        held: list[str] = []
        token = _HELD.set((self, held))
        try:
            yield
        finally:
            _HELD.reset(token)
            for artifact_id in held:
                left = self._pins.get(artifact_id, 0) - 1
                if left > 0:
                    self._pins[artifact_id] = left
                else:
                    self._pins.pop(artifact_id, None)
            if held:
                try:
                    self.enforce_capacity()
                except Exception:  # the call's own result must still go out
                    log.exception("capacity enforcement failed")

    def _held(self, artifact_id: str) -> None:
        """Pin an artifact just read, if a running call (``hold``) is reading it."""
        block = _HELD.get()
        if block is not None and block[0] is self:
            self._pins[artifact_id] = self._pins.get(artifact_id, 0) + 1
            block[1].append(artifact_id)

    def _store_local(self, src: Path, filename: str) -> Artifact:
        artifact_id = self.new_id()
        folder = self.root / artifact_id
        try:
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
            self._write_meta(folder, art)
        except OSError as exc:
            # A full (RAM) disk or a failed write: a typed error that cancels the payment, not
            # a bare 500 and a half-written folder counted against the budget.
            self._remove(folder)
            log.exception("storing an output failed", extra={"artifact_id": artifact_id})
            raise StorageUnavailable(
                "Could not store the output file.",
                hint="Retry in a minute; you were not charged.",
            ) from exc
        return art

    @staticmethod
    def _write_meta(folder: Path, art: Artifact) -> None:
        tmp = folder / "meta.json.tmp"
        tmp.write_text(json.dumps(asdict(art)))
        tmp.replace(folder / "meta.json")

    def _persist(self, art: Artifact) -> None:
        """Make a freshly stored artifact durable. Local disk already is (until restart)."""

    def get(self, artifact_id: str) -> tuple[Artifact, Path]:
        self._check_id(artifact_id)
        art = self._local_record(artifact_id)
        if art is None:
            raise ArtifactNotFound(
                "Unknown or expired artifact_id.",
                hint="Artifacts expire after a TTL; convert the file again.",
            )
        self._unexpired(art)
        self._held(artifact_id)
        return art, self.root / artifact_id / "data.parquet"

    @staticmethod
    def _check_id(artifact_id: str) -> None:
        if not _ID.match(artifact_id):
            raise ArtifactNotFound("Malformed artifact_id.", hint="Use the id returned by convert.")

    def _local_record(self, artifact_id: str) -> Artifact | None:
        try:
            return Artifact(**json.loads((self.root / artifact_id / "meta.json").read_text()))
        except (OSError, ValueError, TypeError):
            return None

    def _unexpired(self, art: Artifact) -> Artifact:
        if art.expires_at < time.time():
            if not self._pins.get(art.id):  # else a running call reads it: the sweep follows
                self.delete(art.id)
            raise ArtifactNotFound(
                "The artifact has expired.", hint="Convert the file again to get a new artifact_id."
            )
        return art

    def delete(self, artifact_id: str) -> None:
        """Remove an artifact now. Unknown or malformed ids are a no-op."""
        self._provisional.discard(artifact_id)
        if _ID.match(artifact_id):
            self._remove(self.root / artifact_id)

    def sweep(self) -> int:
        """Delete expired artifacts. Returns how many were removed."""
        removed = 0
        now = time.time()
        for folder in self._folders():
            try:
                expires = json.loads((folder / "meta.json").read_text())["expires_at"]
            except (OSError, ValueError, KeyError):
                try:
                    expires = folder.stat().st_mtime + self.ttl_s  # half-written: by mtime
                except OSError:  # removed meanwhile: the other sweeper (threads) or a delete
                    continue
            if expires < now and not self._pins.get(folder.name):  # held: the next sweep
                self.delete(folder.name)
                removed += 1
        return removed

    def enforce_capacity(self, *, keep: str | Iterable[str] = ()) -> None:
        """Evict oldest artifacts until total size fits the budget (free disks are small).

        Never evicts ``keep``, an unsettled output, a running call's input, or an output
        delivered less than ``min_residency_s`` ago, so it may leave the total over budget.
        """
        self._make_room(0, keep={keep} if isinstance(keep, str) else set(keep), strict=False)

    def _make_room(self, need: int, *, keep: set[str] | None = None, strict: bool) -> bool:
        """Evict the oldest evictable artifacts until ``need`` more bytes fit the budget.

        Returns whether they fit. ``strict``: when evicting everything evictable would not be
        enough, evict nothing (an output refused anyway must not cost anyone their artifact).
        Counts data files only; a meta.json is a few hundred bytes.
        """
        entries: list[tuple[float, int, str]] = []
        for folder in self._folders():
            try:
                stamp = folder.stat().st_mtime
                entries.append((stamp, (folder / "data.parquet").stat().st_size, folder.name))
            except OSError:  # removed meanwhile (a sweep in a worker thread), or half-written
                continue
        total = need + sum(size for _, size, _ in entries)
        if total <= self.max_total_bytes:
            return True
        now = time.time()
        spared = keep or set()
        victims = sorted(
            (stamp, size, artifact_id)
            for stamp, size, artifact_id in entries
            if artifact_id not in spared
            and artifact_id not in self._provisional
            and not self._pins.get(artifact_id)
            and now - stamp >= min(self.min_residency_s, self.ttl_s)
        )
        if strict and total - sum(size for _, size, _ in victims) > self.max_total_bytes:
            return False
        for _, size, artifact_id in victims:
            if total <= self.max_total_bytes:
                break
            total -= size
            self._evict(self.root / artifact_id)
        return total <= self.max_total_bytes

    def _evict(self, folder: Path) -> None:
        """Free disk for the budget. Here the folder is the artifact, so it is gone for good."""
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

    The local folder is a cache: capacity eviction drops only the local copy (the blob stays
    valid until its TTL), while expiry and ``delete`` remove the blob too. GCS deletes are free
    operations, so expired blobs are deleted as soon as a sweep sees them, including blobs whose
    cached copy was evicted (remembered in ``_uncached``, as no folder is left to find them by).
    """

    # Evicting drops only the cached copy: the blob, and so the download link, stays valid.
    min_residency_s: float = 0

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
        self._next_listing = 0.0  # this instance's own throttle on reading the bucket marker
        self._listing = threading.Lock()  # sweeps run in worker threads; list once at a time
        # id -> expires_at of blobs whose cached copy capacity eviction dropped. Without it they
        # outlived the TTL by up to six hours (the listing sweep) although we knew their expiry.
        self._uncached: dict[str, float] = {}

    @staticmethod
    def _key(artifact_id: str) -> str:
        return f"artifacts/{artifact_id}/data.parquet"

    def _persist(self, art: Artifact) -> None:
        blob = self._bucket.blob(self._key(art.id))
        blob.metadata = {k: str(v) for k, v in asdict(art).items()}
        try:
            blob.upload_from_filename(
                str(self.root / art.id / "data.parquet"),
                content_type="application/vnd.apache.parquet",
            )
        except Exception as exc:
            # Without the blob the download link (served by GCS) would 404: refuse the output,
            # which cancels the payment, rather than hand out a link to nothing.
            self._remove(self.root / art.id)
            log.exception("artifact upload failed", extra={"artifact_id": art.id})
            raise StorageUnavailable(
                "Could not store the output file.",
                hint="Retry in a minute; you were not charged.",
            ) from exc

    def get(self, artifact_id: str) -> tuple[Artifact, Path]:
        self._check_id(artifact_id)
        local = self._local_record(artifact_id)
        if local is not None:
            self._unexpired(local)
            self._held(artifact_id)
            return local, self.root / artifact_id / "data.parquet"
        try:
            got = self._rehydrate(artifact_id)
            self._held(artifact_id)
            return got
        except AimartError:
            raise
        except Exception as exc:  # a GCS blip on a paid read: a typed error, never a bare 500
            log.exception("artifact download failed", extra={"artifact_id": artifact_id})
            self._remove(self.root / artifact_id)
            raise StorageUnavailable(
                "Could not read the stored artifact.",
                hint="Retry in a minute; you were not charged.",
            ) from exc

    def _rehydrate(self, artifact_id: str) -> tuple[Artifact, Path]:
        """Fetch an artifact another instance stored (Cloud Run's disk died with it)."""
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
            self._delete_blob(self._key(artifact_id))
            raise ArtifactNotFound("The artifact has expired.", hint="Convert the file again.")
        # The cache shares the RAM disk's budget: make room first. Never at the cost of what a
        # running call reads (hold), so a join of artifacts that together exceed the budget
        # still runs, over budget until it ends.
        self._make_room(art.size_bytes, strict=False)
        folder = self.root / artifact_id
        folder.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(folder / "data.parquet"))
        self._write_meta(folder, art)
        self._uncached.pop(artifact_id, None)  # cached again: the local sweep finds it
        return art, folder / "data.parquet"

    def delete(self, artifact_id: str) -> None:
        if _ID.match(artifact_id):
            self._uncached.pop(artifact_id, None)
            super().delete(artifact_id)
            self._delete_blob(self._key(artifact_id))

    def _evict(self, folder: Path) -> None:
        # Only the cached copy goes; the blob stays downloadable until its TTL, when the sweep
        # must still delete it.
        art = self._local_record(folder.name)
        if art is not None:
            self._uncached[art.id] = art.expires_at
        self._remove(folder)

    def _delete_blob(self, key: str) -> None:
        try:
            self._bucket.delete_blob(key)
        except Exception as exc:  # noqa: BLE001 - logged below; cleanup must never fail a call
            if getattr(exc, "code", None) == 404:  # google.api_core NotFound: already gone
                return
            # The listing sweep and the bucket's lifecycle rule retry it; never fail a caller.
            log.warning("could not delete an artifact blob", extra={"key": key, "error": str(exc)})

    def sweep(self) -> int:
        """Delete expired artifacts locally and in the bucket, then (rationed) list the bucket
        for expired blobs this instance has no record of: written by an instance that is gone."""
        removed = super().sweep()
        now = time.time()
        for artifact_id, expires in list(self._uncached.items()):
            if expires < now and self._uncached.pop(artifact_id, None) is not None:
                self._delete_blob(self._key(artifact_id))
                removed += 1
        return removed + self._sweep_bucket()

    def _sweep_bucket(self) -> int:
        now = time.time()
        if now < self._next_listing or not self._listing.acquire(blocking=False):
            return 0
        try:
            # Read the marker once per interval only: a long-lived instance never re-reads it
            # on its once-a-minute sweeps, and a cold start reads it once (a class-B get).
            self._next_listing = now + LISTING_INTERVAL_S
            last = self._last_listing()
            if last is not None and now - last < LISTING_INTERVAL_S:
                self._next_listing = last + LISTING_INTERVAL_S
                return 0
            removed = 0
            for blob in self._bucket.list_blobs(prefix="artifacts/"):
                if self._blob_expired(blob, now):
                    parts = str(blob.name).split("/")
                    if len(parts) > 1 and _ID.match(parts[1]):
                        if self._pins.get(parts[1]):
                            continue  # a running call reads it; the request-path sweep follows
                        self._uncached.pop(parts[1], None)  # or a later sweep counts it again
                        super().delete(parts[1])
                    self._delete_blob(str(blob.name))
                    removed += 1
            marker = self._bucket.blob(_LISTING_MARKER)
            marker.metadata = {"listed_at": str(now)}
            marker.upload_from_string(b"", content_type="text/plain")
            return removed
        except Exception:
            # The next attempt is already rationed above; the lifecycle rule is the backstop.
            log.exception("bucket sweep failed")
            return 0
        finally:
            self._listing.release()

    def _last_listing(self) -> float | None:
        marker = self._bucket.get_blob(_LISTING_MARKER)
        try:
            return float((marker.metadata or {})["listed_at"]) if marker is not None else None
        except (KeyError, TypeError, ValueError):
            return None

    def _blob_expired(self, blob: Any, now: float) -> bool:
        try:
            expires = float((blob.metadata or {})["expires_at"])
        except (KeyError, TypeError, ValueError):
            created = getattr(blob, "time_created", None)
            if created is None:
                return False
            expires = created.timestamp() + self.ttl_s  # no metadata: age out by creation
        return expires < now

    def download_url(self, base_url: str, art: Artifact) -> str:
        import datetime as _dt

        import google.auth
        from google.auth.transport import requests as grequests

        # The link must not outlive the artifact: GCS serves it without asking us, so its own
        # expiry is what makes the file unreachable at the TTL (a 60 s floor here outlived it).
        remaining = max(1, int(art.expires_at - time.time()))
        try:
            credentials, _ = google.auth.default()
            credentials.refresh(grequests.Request())  # type: ignore[no-untyped-call]
            blob = self._bucket.blob(self._key(art.id))
            return str(
                blob.generate_signed_url(
                    version="v4",
                    expiration=_dt.timedelta(seconds=remaining),
                    method="GET",
                    service_account_email=getattr(credentials, "service_account_email", None),
                    access_token=credentials.token,
                    response_disposition=f'attachment; filename="{art.filename}"',
                )
            )
        except Exception as exc:
            log.exception("signing a download link failed", extra={"artifact_id": art.id})
            raise StorageUnavailable(
                "Could not create the download link.",
                hint="Retry in a minute; you were not charged.",
            ) from exc


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
