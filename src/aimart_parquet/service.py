"""One implementation of every operation, shared by the REST API and the MCP tools.

Keeping a single code path matters for money: the HTTP route and the MCP tool must agree on
what counts as success, because success is what triggers settlement. Both call these methods,
and both map ``AimartError`` to a >= 400 response that cancels payment.
"""

from __future__ import annotations

import functools
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import anyio
import anyio.to_thread

from aimart_parquet.engine.convert import convert_file
from aimart_parquet.engine.inspect import inspect_parquet
from aimart_parquet.engine.isolate import run_isolated
from aimart_parquet.engine.limits import Limits
from aimart_parquet.engine.models import (
    ArtifactRef,
    ConvertOptions,
    ConvertResult,
    InspectResult,
    Provenance,
    TabularResult,
)
from aimart_parquet.engine.tabular import OutputFormat, preview, query
from aimart_parquet.errors import AimartError, Busy, InvalidRequest, Timeout
from aimart_parquet.provenance import ALGORITHM, build_manifest
from aimart_parquet.provenance import verify as verify_signature
from aimart_parquet.sources import decode_base64, fetch_url, safe_filename

if TYPE_CHECKING:
    from collections.abc import Callable

    from aimart_parquet.artifacts import ArtifactStore
    from aimart_parquet.config import Settings
    from aimart_parquet.provenance import Signer
    from aimart_parquet.quantum.models import (
        PortfolioOptions,
        PortfolioResult,
        QuantumCompressOptions,
        QuantumCompressResult,
    )

T = TypeVar("T")
_QUEUE_FACTOR = 4  # callers allowed to wait per worker slot before we answer "busy"


class QuantumUnavailable(AimartError):
    code = "quantum_unavailable"
    http_status = 501


@dataclass(frozen=True)
class SourceRef:
    """Exactly one field must be set. ``path`` and ``upload`` are never exposed remotely."""

    url: str | None = None
    content_base64: str | None = None
    artifact_id: str | None = None
    filename: str | None = None
    path: str | None = None
    upload: Path | None = None

    def kinds(self) -> list[str]:
        return [
            k
            for k in ("url", "content_base64", "artifact_id", "path", "upload")
            if getattr(self, k) is not None
        ]


class Service:
    def __init__(
        self,
        settings: Settings,
        store: ArtifactStore,
        *,
        local_mode: bool = False,
        signer: Signer | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.local_mode = local_mode
        self.signer = signer
        self.limits = Limits.from_settings(settings)
        self._limiter = anyio.CapacityLimiter(max(1, settings.max_concurrent_jobs))
        self._workroot = settings.artifact_dir / ".work"
        self._workroot.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ plumbing
    def _workdir(self) -> Path:
        return Path(tempfile.mkdtemp(dir=self._workroot))

    async def _execute(
        self, fn: Callable[..., T], *args: Any, timeout_s: float, isolate: bool, **kwargs: Any
    ) -> T:
        stats = self._limiter.statistics()
        if (
            stats.borrowed_tokens >= stats.total_tokens
            and stats.tasks_waiting >= _QUEUE_FACTOR * stats.total_tokens
        ):
            raise Busy(
                "The server is at capacity.", hint="Retry in a few seconds; you were not charged."
            )
        if isolate:
            # The child enforces the deadline (and is killed at it), so no outer timeout here.
            call = functools.partial(run_isolated, fn, *args, timeout_s=timeout_s, **kwargs)
            return await anyio.to_thread.run_sync(call, limiter=self._limiter)
        try:
            with anyio.fail_after(timeout_s):
                return await anyio.to_thread.run_sync(
                    functools.partial(fn, *args, **kwargs),
                    abandon_on_cancel=True,
                    limiter=self._limiter,
                )
        except TimeoutError as exc:
            raise Timeout(
                f"The operation did not finish within {timeout_s:g} seconds.",
                hint="Send a smaller file, or fewer rows; you were not charged.",
            ) from exc

    async def _materialize(self, src: SourceRef, workdir: Path) -> tuple[Path, str | None]:
        await anyio.to_thread.run_sync(self.store.maybe_sweep)
        kinds = src.kinds()
        if len(kinds) != 1:
            allowed = "url, content_base64, artifact_id" + (", path" if self.local_mode else "")
            raise InvalidRequest(
                f"Provide exactly one input; got {len(kinds)} ({', '.join(kinds) or 'none'}).",
                hint=f"Set one of: {allowed}.",
            )
        kind = kinds[0]
        name = safe_filename(src.filename) if src.filename else None
        if kind == "upload":
            assert src.upload is not None
            return src.upload, name
        if kind == "url":
            assert src.url is not None
            fetched = await fetch_url(
                src.url,
                workdir / "download",
                max_bytes=self.settings.max_input_bytes,
                deadline_s=self.settings.fetch_timeout_s,
                allow_private=self.settings.allow_private_urls,
            )
            return fetched.path, name or fetched.filename
        if kind == "content_base64":
            assert src.content_base64 is not None
            dest = workdir / "inline"
            decode_base64(src.content_base64, dest, max_bytes=self.settings.max_input_bytes)
            return dest, name
        if kind == "artifact_id":
            assert src.artifact_id is not None
            art, path = self.store.get(src.artifact_id)
            return path, art.filename
        if not self.local_mode:
            raise InvalidRequest(
                "path inputs are only available in local mode.", hint="Pass url or content_base64."
            )
        assert src.path is not None
        local = Path(src.path).expanduser()  # noqa: ASYNC240 - pure path arithmetic, no I/O
        if not await anyio.Path(local).is_file():
            raise InvalidRequest(
                f"No such file: {local}", hint="Pass an absolute path to an existing file."
            )
        return local, src.filename or local.name

    # ------------------------------------------------------------------ operations
    async def convert(
        self, src: SourceRef, options: ConvertOptions, *, output_path: str | None = None
    ) -> ConvertResult:
        workdir = self._workdir()
        try:
            in_path, name = await self._materialize(src, workdir)
            out = workdir / "out.parquet"
            report = await self._execute(
                convert_file,
                in_path,
                out,
                filename=name,
                options=options,
                workdir=workdir,
                limits=self.limits,
                timeout_s=self.settings.convert_timeout_s,
                isolate=self.settings.isolate_jobs,
            )
            stem = Path(name).name.split(".")[0] if name else "data"
            ref = self._publish(src, out, f"{stem or 'data'}.parquet", "convert", output_path)
            hint = None
            if self.local_mode and self.settings.hosted_url:
                hint = (
                    f"Premium tools (quantum_compress_features, quantum_optimize_portfolio) and "
                    f"hosted conversion with signed provenance: {self.settings.hosted_url}"
                )
            return ConvertResult(artifact=ref, report=report, hint=hint)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _publish(
        self, src: SourceRef, out: Path, filename: str, operation: str, output_path: str | None
    ) -> ArtifactRef:
        """Store (or, locally, place) an output file and describe it. One path for every tool."""
        filename = safe_filename(filename)
        if self.local_mode:
            target = self._local_target(src, output_path, filename)
            shutil.move(out, target)
            return ArtifactRef(
                id="local",
                filename=target.name,
                size_bytes=target.stat().st_size,
                sha256="",
                expires_at="never",
                path=str(target),
            )
        art = self.store.put(out, filename)
        provenance = None
        if self.signer is not None:
            manifest = build_manifest(
                artifact_id=art.id,
                sha256=art.sha256,
                size_bytes=art.size_bytes,
                filename=art.filename,
                operation=operation,
            )
            provenance = Provenance(
                algorithm=ALGORITHM,
                manifest=manifest,
                signature=self.signer.sign(manifest),
                public_key_url=f"{self.settings.public_base_url}/.well-known/aimart-signing-key",
            )
        return ArtifactRef(
            id=art.id,
            filename=art.filename,
            size_bytes=art.size_bytes,
            sha256=art.sha256,
            expires_at=art.expires_iso,
            download_url=self.store.download_url(self.settings.public_base_url, art),
            provenance=provenance,
        )

    def _local_target(self, src: SourceRef, output_path: str | None, filename: str) -> Path:
        if output_path:
            target = Path(output_path).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            return target
        folder = Path(src.path).expanduser().parent if src.path else Path.cwd()
        target = folder / filename
        n = 1
        while target.exists():  # never overwrite a file the caller did not name
            target = folder / f"{Path(filename).stem}.{n}.parquet"
            n += 1
        return target

    async def _parquet_path(self, src: SourceRef, workdir: Path) -> Path:
        path, _ = await self._materialize(src, workdir)
        return path

    async def inspect(self, src: SourceRef) -> InspectResult:
        workdir = self._workdir()
        try:
            path = await self._parquet_path(src, workdir)
            return await self._execute(
                inspect_parquet, path, timeout_s=self.settings.query_timeout_s, isolate=False
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def preview(
        self,
        src: SourceRef,
        *,
        columns: list[str] | None,
        offset: int,
        limit: int,
        fmt: OutputFormat,
    ) -> TabularResult:
        limit = max(0, min(limit, self.settings.max_preview_rows))
        workdir = self._workdir()
        try:
            path = await self._parquet_path(src, workdir)
            return await self._execute(
                preview,
                path,
                columns=columns,
                offset=max(0, offset),
                limit=limit,
                fmt=fmt,
                max_chars=self.settings.max_cell_chars,
                timeout_s=self.settings.query_timeout_s,
                isolate=False,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def query(
        self, tables: dict[str, SourceRef], sql: str, *, limit: int, fmt: OutputFormat
    ) -> TabularResult:
        if not tables:
            raise InvalidRequest(
                "No tables to query.", hint="Pass an input; it is available as table `data`."
            )
        limit = max(1, min(limit, self.settings.max_query_rows))
        workdir = self._workdir()
        try:
            paths: dict[str, Path] = {}
            for i, (name, ref) in enumerate(tables.items()):
                sub = workdir / f"t{i}"
                sub.mkdir()
                paths[name] = await self._parquet_path(ref, sub)
            # query() interrupts DuckDB itself at query_timeout_s; the outer deadline is a backstop.
            return await self._execute(
                query,
                paths,
                sql,
                limit=limit,
                fmt=fmt,
                query_timeout=self.settings.query_timeout_s,
                max_chars=self.settings.max_cell_chars,
                memory_limit=self.settings.duckdb_memory_limit,
                threads=self.settings.duckdb_threads,
                timeout_s=self.settings.query_timeout_s + 5,
                isolate=self.settings.isolate_jobs,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # ------------------------------------------------------------------ premium quantum tier
    def _require_quantum(self) -> None:
        from aimart_parquet import quantum

        if not quantum.available():
            raise QuantumUnavailable(
                "The quantum tier is not installed on this server.",
                hint="Install the `quantum` extra (pip install 'aimart-parquet[quantum]').",
            )
        if not self.settings.enable_quantum:
            raise QuantumUnavailable(
                "The quantum tier is not enabled on this server.",
                hint="Set AIMART_ENABLE_QUANTUM=true to serve it.",
            )

    async def quantum_compress(
        self, src: SourceRef, options: QuantumCompressOptions, *, output_path: str | None = None
    ) -> QuantumCompressResult:
        self._require_quantum()
        from aimart_parquet.quantum.kernel_pca import compress
        from aimart_parquet.quantum.models import SIMULATOR_NOTE, QuantumCompressResult

        workdir = self._workdir()
        try:
            path, name = await self._materialize(src, workdir)
            out = workdir / "quantum.parquet"
            fields = await self._execute(
                compress,
                path,
                out,
                options,
                timeout_s=self.settings.quantum_timeout_s,
                isolate=self.settings.isolate_jobs,
            )
            stem = Path(name).name.split(".")[0] if name else "data"
            ref = self._publish(
                src, out, f"{stem or 'data'}_qpca.parquet", "quantum_compress", output_path
            )
            extra_notes = fields.pop("notes", [])
            # The simulator disclaimer is part of the honesty contract: it is always first.
            return QuantumCompressResult(
                artifact=ref, **fields, notes=[SIMULATOR_NOTE, *extra_notes]
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def quantum_portfolio(self, src: SourceRef, options: PortfolioOptions) -> PortfolioResult:
        self._require_quantum()
        from aimart_parquet.quantum.models import SIMULATOR_NOTE, PortfolioResult
        from aimart_parquet.quantum.portfolio import optimize

        workdir = self._workdir()
        try:
            path, _ = await self._materialize(src, workdir)
            fields = await self._execute(
                optimize,
                path,
                options,
                timeout_s=self.settings.quantum_timeout_s,
                isolate=self.settings.isolate_jobs,
            )
            extra_notes = fields.pop("notes", [])
            return PortfolioResult(**fields, notes=[SIMULATOR_NOTE, *extra_notes])
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # ------------------------------------------------------------------ provenance
    async def verify_provenance(
        self, manifest: dict[str, Any], signature: str, *, artifact_id: str | None = None
    ) -> dict[str, Any]:
        """Free check: is this manifest signed by us, and (optionally) does the file match it?"""
        if self.signer is None:
            raise InvalidRequest(
                "This server does not sign artifacts.", hint="Use the hosted service."
            )
        problems: list[str] = []
        signature_valid = verify_signature(self.signer.public_key_b64(), manifest, signature)
        if not signature_valid:
            problems.append("signature does not match the manifest and this server's public key")
        file_matches: bool | None = None
        if artifact_id is not None:
            art, _ = self.store.get(artifact_id)
            file_matches = (
                art.id == manifest.get("artifact_id")
                and art.sha256 == manifest.get("sha256")
                and art.size_bytes == manifest.get("size_bytes")
            )
            if not file_matches:
                problems.append("the stored artifact's id, hash or size differs from the manifest")
        return {
            "valid": signature_valid and file_matches is not False,
            "signature_valid": signature_valid,
            "file_matches": file_matches,
            "algorithm": ALGORITHM,
            "problems": problems,
        }
