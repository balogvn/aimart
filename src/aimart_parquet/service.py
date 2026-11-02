"""One implementation of every operation, shared by the REST API and the MCP tools.

Keeping a single code path matters for money: the HTTP route and the MCP tool must agree on
what counts as success, because success is what triggers settlement. Both call these methods,
and both map ``AimartError`` to a >= 400 response that cancels payment.

Capacity: every operation holds one job slot (``_job``) from before its inputs are fetched
until its result is built. URL inputs land on the artifact disk, which on Cloud Run is RAM, so
downloads must count toward capacity and share one byte budget per call.

Before a price is quoted, REST and MCP ask ``check_input`` / ``check_parquet_input`` /
``check_query`` what the service would refuse about a request from the request alone, so an
agent never signs for a call that can only fail. ``internal_error`` is the one answer, on both
transports, for an exception that is not an ``AimartError``.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import re
import shutil
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import anyio
import anyio.to_thread

from aimart_parquet.artifacts import discard_withheld
from aimart_parquet.demand import DemandSignals
from aimart_parquet.engine.convert import convert_file
from aimart_parquet.engine.detect import format_from_name
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
from aimart_parquet.engine.tabular import OutputFormat, preview, query, validate_select
from aimart_parquet.errors import (
    AimartError,
    Busy,
    InputTooLarge,
    InternalError,
    InvalidInput,
    InvalidRequest,
    Timeout,
    UnsupportedFormat,
)
from aimart_parquet.provenance import ALGORITHM, build_manifest
from aimart_parquet.provenance import verify as verify_signature
from aimart_parquet.sources import (
    check_base64,
    decode_base64,
    downloads_deadline,
    fetch_url,
    safe_filename,
    url_filename,
    validate_url,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping

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
log = logging.getLogger("aimart")
_QUEUE_FACTOR = 4  # callers allowed to wait per worker slot before we answer "busy"
MAX_QUERY_TABLES = 8
TABLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
# SQL longer than this is not parsed before the quote, only where the query runs (in the job's
# child process, with a deadline). DuckDB's parser holds the GIL and is cubic on runs of '+'
# and '-': 1,600 characters took 1.3 s, and the 20,000 the schema allows would stall the API
# process for most of an hour. At 256 the worst found is ~7 ms, under what parsing the JSON
# body of a large request already costs.
PRECHECK_SQL_CHARS = 256


def _not_parquet(raw: bytes) -> str | None:
    """Why pyarrow cannot open ``raw`` as Parquet, judged from its last 8 bytes as pyarrow
    judges them first, in pyarrow's words; None when only the engine can tell.

    inspect, preview and query open every input with ``pq.ParquetFile`` (engine/inspect.py,
    engine/tabular.py). Inline bytes it can only refuse, a CSV sent to preview before it was
    converted most often, were quoted and, paid, verified before that 422. Only the footer is
    judged: pyarrow never reads the leading 'PAR1', so a file without it still opens.
    """
    if len(raw) < 8:
        return (
            f"Parquet file size is {len(raw)} bytes, smaller than the minimum file footer (8 bytes)"
        )
    if raw[-4:] == b"PARE":  # an encrypted footer: this server never holds the keys
        return "Could not read encrypted metadata, no decryption found in reader's properties"
    if raw[-4:] != b"PAR1":
        return (
            "Parquet magic bytes not found in footer. Either the file is corrupted or this is "
            "not a parquet file."
        )
    return None


class QuantumUnavailable(AimartError):
    code = "quantum_unavailable"
    http_status = 501


# The inputs a caller can name, in the order a refusal names them (``upload`` is the REST form's
# file part, never named).
INPUT_KINDS = ("url", "content_base64", "artifact_id", "path")


def inputs_among(names: Iterable[str]) -> tuple[str, ...]:
    """The input kinds among a route's or tool's parameter names, in the order hints name them."""
    present = set(names)
    return tuple(kind for kind in INPUT_KINDS if kind in present)


@dataclass(frozen=True)
class SourceRef:
    """Exactly one input field must be set. ``path`` and ``upload`` are never exposed remotely.

    ``takes`` is what the route or tool that built the ref accepts (``inputs_among`` of its
    parameters), for the hints of its refusals; None means every input this mode serves. One
    list for every surface told MCP agents to "Set one of: url, content_base64, artifact_id."
    where convert_to_parquet took no artifact_id and inspect_parquet no content_base64, so an
    agent that followed the hint was refused again.
    """

    url: str | None = None
    content_base64: str | None = None
    artifact_id: str | None = None
    filename: str | None = None
    path: str | None = None
    upload: Path | None = None
    takes: tuple[str, ...] | None = field(default=None, compare=False)

    def kinds(self) -> list[str]:
        return [
            k
            for k in ("url", "content_base64", "artifact_id", "path", "upload")
            if getattr(self, k) is not None
        ]


@dataclass
class _InputBudget:
    """Bytes one call may still download or decode, across all of its inputs.

    A query names up to 8 inputs. Each URL alone was capped at max_input_bytes, so one paid
    call could put 8 x 50 MB on Cloud Run's RAM-backed disk, and four concurrent calls 1.6 GB.
    Stored artifacts do not count: they are already inside the artifact budget.
    """

    total: int
    used: int = field(default=0)

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.used)

    def exceeded(self) -> InputTooLarge:
        return InputTooLarge(
            f"The inputs of this call together exceed the {self.total // (1024 * 1024)} MB "
            "limit per call.",
            hint="Convert each file first and pass the artifact_ids instead (stored artifacts "
            "do not count), or send smaller extracts; you were not charged.",
            limit_bytes=self.total,
        )


def internal_error(exc: BaseException, **context: Any) -> InternalError:
    """The typed 500 for an exception that is not an ``AimartError``, logged without its text.

    The text can quote server paths or the caller's data, so the caller gets only the type and
    the log gets the type and the frames. Raised on a paid call, it is never settled (REST
    settles below 400, MCP only a result that is not an error).
    """
    frames = [
        f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_tb(exc.__traceback__)
    ]
    log.error(
        "internal error",
        extra={"error_type": type(exc).__name__, "frames": frames[-30:], **context},
    )
    return InternalError.of(type(exc).__name__)


def empty_upload() -> InvalidRequest:
    """An upload with no bytes: refused before the quote (``_form_check``) and by the route."""
    return InvalidRequest(
        "The uploaded file is empty.",
        hint="Upload the bytes of a non-empty file; you were not charged.",
    )


class Service:
    def __init__(
        self,
        settings: Settings,
        store: ArtifactStore,
        *,
        local_mode: bool = False,
        signer: Signer | None = None,
        demand: DemandSignals | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.local_mode = local_mode
        self.signer = signer
        self.demand = demand or DemandSignals()
        self.limits = Limits.from_settings(settings)
        self._limiter = anyio.CapacityLimiter(max(1, settings.max_concurrent_jobs))
        self._workroot = settings.artifact_dir / ".work"
        self._workroot.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ plumbing
    def _workdir(self) -> Path:
        return Path(tempfile.mkdtemp(dir=self._workroot))

    @contextlib.asynccontextmanager
    async def _job(self, work_s: float) -> AsyncIterator[None]:
        """Hold one job slot for a whole operation: fetching inputs, the engine, the output.

        Taken before any input is materialized. The slot used to be taken only around the
        engine call, so a query naming 8 URLs downloaded up to 400 MB onto the RAM disk and
        only then learned the server was busy. The stored artifacts the operation reads stay on
        disk until it ends (``store.hold``): on GCS, fetching a query's second table evicted
        the cached copy of its first, and the join failed on a missing file. ``work_s`` is the
        timeout the operation's engine call gets, which decides how long it may queue.
        """
        busy = Busy(
            "The server is at capacity.", hint="Retry in a few seconds; you were not charged."
        )
        stats = self._limiter.statistics()
        if (
            stats.borrowed_tokens >= stats.total_tokens
            and stats.tasks_waiting >= _QUEUE_FACTOR * stats.total_tokens
        ):
            raise busy
        # A paid call's signed authorization expires (config.PAYMENT_WINDOW_S): waiting for a
        # slot as long as the calls ahead took (up to 4 x 180 s) ran work that could no longer
        # settle. The wait is bounded by what the window leaves this operation, not a fixed
        # 10 s: that turned away, after verifying its payment, a convert queued 14 s behind a
        # slow download that would have settled well inside the window. A free slot is taken
        # even when the bound is 0.
        acquired = False
        try:
            self._limiter.acquire_nowait()
            acquired = True
        except anyio.WouldBlock:
            with anyio.move_on_after(self.settings.queue_wait_for(work_s)):
                await self._limiter.acquire()
                acquired = True
        if not acquired:
            raise busy
        try:
            # One download allowance for all of the call's URL inputs, as the payment-window
            # check at startup counts the queue wait, then one fetch_timeout_s, per call. Only
            # time spent downloading draws on it (not the sweep, nor stored artifacts fetched).
            with self.store.hold(), downloads_deadline(self.settings.fetch_timeout_s):
                yield
        finally:
            self._limiter.release()

    async def _execute(
        self,
        fn: Callable[..., T],
        *args: Any,
        timeout_s: float,
        isolate: bool,
        job_dirs: tuple[Path, ...] = (),
        **kwargs: Any,
    ) -> T:
        """Run one engine call. Callers hold the job slot (``async with self._job(timeout_s)``).

        ``job_dirs`` are the call's work directories: the isolated job's memory cap counts the
        bytes in them, because on Cloud Run they are RAM too. A 1.5 MB gzip CSV decompressed to
        501 MiB in the work directory while the cap, counting only the child's own memory, saw
        nothing; two such calls at once were enough to take the instance down, unpaid.
        """
        if isolate:
            # The child enforces the deadline (and is killed at it), so no outer timeout here.
            call = functools.partial(
                run_isolated,
                fn,
                *args,
                timeout_s=timeout_s,
                memory_limit_mb=self.settings.job_memory_limit_mb,
                job_dirs=job_dirs,
                **kwargs,
            )
            return await anyio.to_thread.run_sync(call)
        try:
            with anyio.fail_after(timeout_s):
                return await anyio.to_thread.run_sync(
                    functools.partial(fn, *args, **kwargs), abandon_on_cancel=True
                )
        except TimeoutError as exc:
            raise Timeout(
                f"The operation did not finish within {timeout_s:g} seconds.",
                hint="Send a smaller file, or fewer rows; you were not charged.",
            ) from exc

    def _budget(self) -> _InputBudget:
        return _InputBudget(self.settings.max_input_bytes)

    # ------------------------------------------------------------------ before the quote
    # What the operations below would refuse about a request, decided from the request alone
    # (no download, no file, no DuckDB run), with the same error. REST's _body_check and MCP's
    # argument checker call these before a price is quoted: without them an agent signed for,
    # and the facilitator verified, calls that could only fail. The operations check again.
    def check_input(self, src: SourceRef, *, table: str | None = None) -> bytes | None:
        """Exactly one input, of a kind this mode serves; a URL the fetcher could fetch; base64
        that decodes to a file. Returns the bytes ``content_base64`` holds, if that is it."""
        self._one_input(src, table=table)
        if src.url is not None:
            validate_url(src.url, allow_private=self.settings.allow_private_urls)
        if src.content_base64 is not None:
            # The one base64 input a call can have, so the whole budget is its limit, as when
            # _materialize decodes it.
            return check_base64(src.content_base64, max_bytes=self.settings.max_input_bytes)
        return None

    def check_convert(self, src: SourceRef, options: ConvertOptions) -> None:
        """``check_input`` for convert, then ``check_options`` for the format its input names."""
        self.check_input(src)
        name = src.filename  # the name _materialize gives the engine, where it has one
        if name is None and src.url is not None:
            name = url_filename(src.url)
        if name is None and src.path is not None:
            name = Path(src.path).name
        self.check_options(options, name)

    @staticmethod
    def check_options(options: ConvertOptions, filename: str | None) -> None:
        """An option value the reader of the request's format can never use, as it refuses it.

        The format is the one the request names: ``options.format``, else the extension of the
        caller's filename (of a url's path when there is none). Bytes win over names in
        ``detect``, so a mislabelled file may still read otherwise; with neither, the format is
        not known before the file is, and the reader refuses the value, unpaid.
        """
        refusal = options.refusal(options.format or format_from_name(filename))
        if refusal is not None:
            raise refusal

    def _takes(self, src: SourceRef) -> tuple[str, ...]:
        """The inputs a refusal's hint may name: what the calling route or tool accepts."""
        if src.takes is not None:
            return src.takes
        return tuple(kind for kind in INPUT_KINDS if self.local_mode or kind != "path")

    def _one_input(self, src: SourceRef, *, table: str | None = None) -> None:
        """Exactly one input, and a path only in local mode; hints name only ``_takes``."""
        kinds = src.kinds()
        if len(kinds) != 1:
            got = f"got {len(kinds)} ({', '.join(kinds) or 'none'})"
            if table is not None:
                raise InvalidRequest(
                    f"Table {table!r}: provide exactly one input; {got}.",
                    hint="Give every entry of `tables` one artifact_id (or url).",
                )
            raise InvalidRequest(
                f"Provide exactly one input; {got}.",
                hint=f"Set one of: {', '.join(self._takes(src))}.",
            )
        if src.path is not None and not self.local_mode:
            remote = [kind for kind in self._takes(src) if kind != "path"]
            raise InvalidRequest(
                "path inputs are only available in local mode.",
                hint=f"Set one of: {', '.join(remote)}.",
            )

    def check_parquet_input(self, src: SourceRef) -> None:
        """``check_input`` for inspect and preview, which read their input as Parquet: inline
        bytes that can never open as Parquet are refused as the engine refuses them."""
        raw = self.check_input(src)
        reason = None if raw is None else _not_parquet(raw)
        if reason is not None:
            raise InvalidInput(
                f"Not a readable Parquet file: {reason}", hint="Convert the file first."
            )

    def check_tables(self, tables: Mapping[str, SourceRef]) -> None:
        """The table map itself: at least one table, at most ``MAX_QUERY_TABLES``, valid names.

        ``query`` runs it first and ``check_query`` before the quote: the two held copies of
        these rules, which a test had to pin to the same messages.
        """
        if not tables:
            raise InvalidRequest(
                "No tables to query.", hint="Pass an input; it is available as table `data`."
            )
        if len(tables) > MAX_QUERY_TABLES:
            raise InvalidRequest(
                f"{len(tables)} tables requested; the limit is {MAX_QUERY_TABLES}.",
                hint="Join fewer files per query, or pre-aggregate them.",
            )
        bad = [name for name in tables if not TABLE_NAME.match(name)]
        if bad:
            raise InvalidRequest(
                f"Invalid table names: {bad}.",
                hint="Use lowercase names of letters, digits and underscores, e.g. 'gl' or 'bank_2025'.",
            )

    def check_query(self, tables: Mapping[str, SourceRef], sql: str) -> None:
        """The table map ``query`` takes, each table's input, and the SQL (when short)."""
        self.check_tables(tables)
        inline = {
            name: self.check_input(ref, table=None if name == "data" else name)
            for name, ref in tables.items()
        }
        if len(sql) <= PRECHECK_SQL_CHARS:
            validate_select(sql)  # the engine's own rule: one read-only SELECT
        for name, raw in inline.items():  # then, as the engine does, each table is opened
            if raw is not None and _not_parquet(raw) is not None:
                raise InvalidInput(
                    f"Table {name!r} is not a readable Parquet file.",
                    hint="Convert it first (/v1/convert or the convert tool) and pass the "
                    "returned artifact_id.",
                )

    async def _materialize(
        self, src: SourceRef, workdir: Path, budget: _InputBudget
    ) -> tuple[Path, str | None]:
        await anyio.to_thread.run_sync(self.store.maybe_sweep)
        self._one_input(src)
        kind = src.kinds()[0]
        name = safe_filename(src.filename) if src.filename else None
        if kind == "upload":
            assert src.upload is not None
            return src.upload, name
        if kind == "url":
            assert src.url is not None
            try:
                fetched = await fetch_url(
                    src.url,
                    workdir / "download",
                    max_bytes=budget.remaining,
                    deadline_s=self.settings.fetch_timeout_s,
                    allow_private=self.settings.allow_private_urls,
                )
            except InputTooLarge as exc:
                if not budget.used:
                    raise
                raise budget.exceeded() from exc
            budget.used += fetched.size
            return fetched.path, name or fetched.filename
        if kind == "content_base64":
            assert src.content_base64 is not None
            dest = workdir / "inline"
            try:
                budget.used += decode_base64(src.content_base64, dest, max_bytes=budget.remaining)
            except InputTooLarge as exc:
                if not budget.used:
                    raise
                raise budget.exceeded() from exc
            return dest, name
        if kind == "artifact_id":
            assert src.artifact_id is not None
            art, path = self.store.get(src.artifact_id)
            return path, art.filename
        assert src.path is not None  # and self.local_mode (_one_input)
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
        async with self._job(self.settings.convert_timeout_s):
            return await self._convert(src, options, output_path)

    async def convert_upload(
        self,
        read: Callable[[int], Awaitable[bytes]],
        filename: str | None,
        options: ConvertOptions,
    ) -> ConvertResult:
        """Convert a multipart upload, copied to disk inside the job slot like a URL download.

        The route used to copy all 50 MB onto the RAM-backed artifact disk first and only then
        ask for a slot, so a busy server still took the copy before answering "busy".
        """
        async with self._job(self.settings.convert_timeout_s):
            workdir = self._workdir()
            try:
                dest = workdir / "upload"
                size = 0
                with dest.open("wb") as out:
                    while chunk := await read(1024 * 1024):
                        size += len(chunk)
                        if size > self.settings.max_input_bytes:
                            raise InputTooLarge(
                                f"Upload exceeds {self.settings.max_input_bytes // (1024 * 1024)} "
                                "MB.",
                                hint="Split the file or pass a url.",
                            )
                        out.write(chunk)
                if not size:  # it was a 422 "The file contains no rows." after the payment
                    raise empty_upload()
                # Into the directory the convert works in, so the upload is on the job's disk
                # once. In a directory of its own it was outside the engine's, which copies such
                # an input in (DuckDB reads nothing else), and the job cap counted both copies:
                # a 50 MB upload took 100 MB of the 700 MB before parsing began.
                return await self._convert(
                    SourceRef(upload=dest, filename=filename), options, None, workdir=workdir
                )
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

    async def _convert(
        self,
        src: SourceRef,
        options: ConvertOptions,
        output_path: str | None,
        *,
        workdir: Path | None = None,
    ) -> ConvertResult:
        if workdir is None:
            workdir = self._workdir()
        try:
            in_path, name = await self._materialize(src, workdir, self._budget())
            if src.artifact_id is not None:
                in_path = self._linked_in(in_path, workdir)
            out = workdir / "out.parquet"
            try:
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
                    job_dirs=(workdir,),
                )
            except UnsupportedFormat:
                self.demand.unsupported_format(name)  # roadmap signal: what agents tried to send
                raise
            stem = Path(name).name.split(".")[0] if name else "data"
            ref = self._publish(src, out, f"{stem or 'data'}.parquet", "convert", output_path)
            hint = None
            if self.local_mode and self.settings.hosted_url:
                hint = (
                    "Hosted version (no install; signed provenance, works from any agent "
                    f"platform): {self.settings.hosted_url}"
                )
            return ConvertResult(artifact=ref, report=report, hint=hint)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    @staticmethod
    def _linked_in(stored: Path, workdir: Path) -> Path:
        """A stored artifact's file, hard-linked into ``workdir`` for the engine to read.

        The engine copies an input from outside its work directory in, because DuckDB may read
        nothing else, and the job cap counts the copy: converting a stored 100 MB artifact spent
        100 MB of it on bytes already in the artifact budget. A link takes no space, and the
        watchdog does not count a file linked from outside the job's directories. The engine
        only reads its input, and the link goes with the work directory. Where the link fails
        (another filesystem, a refused link), the engine's copy is the fallback. Only for the
        store's files: a local caller's own file is never linked.
        """
        link = workdir / ("input" + "".join(stored.suffixes[-2:]))
        try:
            link.hardlink_to(stored)
        except OSError:
            return stored
        return link

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
        try:
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
        except BaseException:
            # Stored, but the caller gets an error and pays nothing: the file must not sit in
            # the budget until its TTL, evicting paying customers' artifacts.
            discard_withheld(self.store, [art.id], reason="publish_failed")
            raise

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

    async def _parquet_path(self, src: SourceRef, workdir: Path, budget: _InputBudget) -> Path:
        path, _ = await self._materialize(src, workdir, budget)
        return path

    # inspect and preview decode a caller's Parquet (inspect: its footer, which is native
    # Thrift plus a loop over every row group). They ran in-process, so a 258-byte dictionary
    # bomb took /v1/preview's server past 2 GB with limit=1: they run in the child like query.
    async def inspect(self, src: SourceRef) -> InspectResult:
        async with self._job(self.settings.query_timeout_s):
            workdir = self._workdir()
            try:
                path = await self._parquet_path(src, workdir, self._budget())
                return await self._execute(
                    inspect_parquet,
                    path,
                    timeout_s=self.settings.query_timeout_s,
                    isolate=self.settings.isolate_jobs,
                    job_dirs=(workdir,),
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
        async with self._job(self.settings.query_timeout_s):
            workdir = self._workdir()
            try:
                path = await self._parquet_path(src, workdir, self._budget())
                return await self._execute(
                    preview,
                    path,
                    columns=columns,
                    offset=max(0, offset),
                    limit=limit,
                    fmt=fmt,
                    max_chars=self.settings.max_cell_chars,
                    timeout_s=self.settings.query_timeout_s,
                    isolate=self.settings.isolate_jobs,
                    job_dirs=(workdir,),
                )
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

    async def query(
        self, tables: dict[str, SourceRef], sql: str, *, limit: int, fmt: OutputFormat
    ) -> TabularResult:
        self.check_tables(tables)
        limit = max(1, min(limit, self.settings.max_query_rows))
        async with self._job(self.settings.query_timeout_s + 5):
            workdir = self._workdir()
            try:
                budget = self._budget()
                paths: dict[str, Path] = {}
                for i, (name, ref) in enumerate(tables.items()):
                    sub = workdir / f"t{i}"
                    sub.mkdir()
                    paths[name] = await self._parquet_path(ref, sub, budget)
                # query() interrupts DuckDB itself at query_timeout_s; the outer deadline is a
                # backstop.
                return await self._execute(
                    query,
                    paths,
                    sql,
                    limit=limit,
                    fmt=fmt,
                    query_timeout=self.settings.query_timeout_s,
                    max_chars=self.settings.max_cell_chars,
                    memory_limit=self.limits.duckdb_memory_limit,
                    threads=self.limits.duckdb_threads,
                    max_temp_size=self.limits.duckdb_max_temp_size,
                    timeout_s=self.settings.query_timeout_s + 5,
                    isolate=self.settings.isolate_jobs,
                    job_dirs=(workdir,),
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
        async with self._job(self.settings.quantum_timeout_s):
            return await self._quantum_compress(src, options, output_path)

    async def _quantum_compress(
        self, src: SourceRef, options: QuantumCompressOptions, output_path: str | None
    ) -> QuantumCompressResult:
        from aimart_parquet.quantum.kernel_pca import compress
        from aimart_parquet.quantum.models import SIMULATOR_NOTE, QuantumCompressResult

        workdir = self._workdir()
        try:
            path, name = await self._materialize(src, workdir, self._budget())
            out = workdir / "quantum.parquet"
            fields = await self._execute(
                compress,
                path,
                out,
                options,
                timeout_s=self.settings.quantum_timeout_s,
                isolate=self.settings.isolate_jobs,
                job_dirs=(workdir,),
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

        async with self._job(self.settings.quantum_timeout_s):
            workdir = self._workdir()
            try:
                path, _ = await self._materialize(src, workdir, self._budget())
                fields = await self._execute(
                    optimize,
                    path,
                    options,
                    timeout_s=self.settings.quantum_timeout_s,
                    isolate=self.settings.isolate_jobs,
                    job_dirs=(workdir,),
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
