"""Run a job in a short-lived child process: crash isolation, a hard deadline, a memory cap.

Why a process and not a thread:

* Native parsers can crash, not just raise. DuckDB's ``read_json`` segfaults on JSON nested
  ~21,600 levels deep — a 50 KB file. In-process, that takes down the server mid-way through
  other callers' paid requests. In a child, it is one failed (and therefore unpaid) call.
* Threads cannot be killed. pyreadstat and calamine ignore DuckDB's interrupt, so a thread
  timeout only stops *waiting*; the CPU burn continues. A child is killed at the deadline.
* Memory is returned to the OS when the child exits, which matters on 2 GiB instances.
* Memory is capped per job. A 1.6 KB PDF drawing 1M glyphs took a child to 2 GB in 15 s, and
  a query returning huge strings or a Parquet dictionary bomb in preview does the same: on a
  2 GiB instance running two jobs and the API, one such job kills every in-flight request.
  The parent polls the child's resident memory and kills it over ``job_memory_limit_mb``,
  raising ``InputTooLarge`` (unpaid). ``RLIMIT_AS``/``RLIMIT_DATA`` cannot do this: measured
  on Linux, a child starts at 2 GB virtual (DuckDB, Arrow, PennyLane and glibc arenas reserve
  address space) with 160 MB resident, and a 1.5 GB limit already failed a 200k-row CSV.

Children fork from a ``forkserver`` that has already imported the engine, so a job costs a
fork (milliseconds), not a fresh interpreter importing pyarrow and DuckDB.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import multiprocessing as mp
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from aimart_parquet import errors
from aimart_parquet.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Callable

T = TypeVar("T")

log = logging.getLogger("aimart.isolate")

# BLAS thread pools do not survive fork() everywhere: on macOS (Accelerate) the quantum kernel
# PCA segfaulted in a worker on one input and not another — BLAS only goes multi-threaded above
# a size threshold, so the crash depended on the data. Workers run one job on 1-2 vCPUs, so a
# single BLAS thread costs nothing. Must be set before the forkserver (a fresh process) starts.
for _var in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ.setdefault(_var, "1")

_METHOD = "forkserver" if "forkserver" in mp.get_all_start_methods() else "spawn"
_CTX: Any = mp.get_context(_METHOD)
if _METHOD == "forkserver":
    # Preloading the quantum modules costs ~2.5 s once at forkserver start instead of on every
    # premium call; a missing optional extra is skipped silently by multiprocessing.
    _CTX.set_forkserver_preload(
        [
            "aimart_parquet.engine.convert",
            "aimart_parquet.engine.tabular",
            "aimart_parquet.quantum.kernel_pca",
            "aimart_parquet.quantum.portfolio",
            "pennylane",  # the quantum modules import it lazily; preload it explicitly
            "pdfplumber",
        ]
    )


# The parent checks the child's memory this often. A job allocating as fast as it can gains
# a few hundred MB between checks; the default limit leaves that much headroom on 2 GiB.
_POLL_S = 0.1
_PROC = Path("/proc")
_LIBPROC = "/usr/lib/libproc.dylib"
_PROC_PIDTASKINFO = 4  # proc_pidinfo flavor returning struct proc_taskinfo


def _rss_from_proc(pid: int) -> int | None:
    """Linux: ``VmRSS`` from ``/proc/<pid>/status``, in bytes (None once the child is gone)."""
    try:
        status = (_PROC / str(pid) / "status").read_text()
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    return None


@functools.cache
def _libproc() -> ctypes.CDLL:
    lib = ctypes.CDLL(_LIBPROC)
    lib.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    return lib


def _rss_from_libproc(pid: int) -> int | None:
    """macOS: ``pti_resident_size`` (the second field of the 96-byte ``proc_taskinfo``)."""
    info = (ctypes.c_uint64 * 12)()
    got = _libproc().proc_pidinfo(pid, _PROC_PIDTASKINFO, 0, info, ctypes.sizeof(info))
    return int(info[1]) if got == ctypes.sizeof(info) else None


_RSS_READERS: dict[str, Callable[[int], int | None]] = {
    "linux": _rss_from_proc,
    "darwin": _rss_from_libproc,
}


def _read_rss(pid: int) -> int | None:
    """Resident bytes of ``pid``; None where the platform offers no cheap way to ask.

    Linux (production) and macOS (development) only. On Linux the figure includes the pages the
    child shares with the forkserver (~160 MB of preloaded engine); macOS counts them elsewhere.
    """
    reader = _RSS_READERS.get(sys.platform)
    return reader(pid) if reader is not None else None


def _wait_for_report(receiver: Any, pid: int, timeout_s: float, memory_limit_mb: int) -> Any:
    """The child's message, or None if it died without one. Enforces the deadline and memory."""
    deadline = time.monotonic() + timeout_s
    while (remaining := deadline - time.monotonic()) > 0:
        if receiver.poll(min(remaining, _POLL_S) if memory_limit_mb else remaining):
            try:
                return receiver.recv()
            except EOFError:
                return None  # the child died before reporting: a crash
        rss = _read_rss(pid) if memory_limit_mb else None
        if rss is not None and rss > memory_limit_mb * 1024 * 1024:
            raise errors.InputTooLarge(
                f"The job exceeded its memory budget ({memory_limit_mb} MB) and was stopped.",
                hint=(
                    "Send a smaller file (for a PDF, fewer pages via pdf_pages), or a query that "
                    "returns less data; you were not charged."
                ),
                limit_bytes=memory_limit_mb * 1024 * 1024,
            )
    raise errors.Timeout(
        f"The operation did not finish within {timeout_s:g} seconds.",
        hint="Send a smaller file or a simpler query; you were not charged.",
    )


def _child(
    conn: Any, fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any], tmp: str
) -> None:
    # Whatever the job puts in the temp directory lands in a folder the parent deletes once the
    # child is gone. A child killed at the deadline or the memory cap skips its own cleanup, and
    # a query's spill folder holds hard links to (or copies of) its inputs: RAM on Cloud Run.
    tempfile.tempdir = tmp
    os.environ["TMPDIR"] = tmp
    try:
        conn.send(("ok", fn(*args, **kwargs)))
    except errors.AimartError as exc:
        conn.send(("err", type(exc).__name__, exc.message, exc.hint, exc.details))
    except RecursionError:
        conn.send(
            (
                "err",
                "InvalidInput",
                "The input is nested too deeply to parse.",
                "Flatten the data before sending; you were not charged.",
                {},
            )
        )
    except MemoryError:
        conn.send(
            (
                "err",
                "InputTooLarge",
                "The input needs more memory than this server allows.",
                "Send a smaller file or fewer columns.",
                {},
            )
        )
    except BaseException as exc:  # noqa: BLE001 - report anything; the parent decides
        conn.send(("crash", type(exc).__name__, f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


def _rebuild(
    name: str, message: str, hint: str | None, details: dict[str, Any]
) -> errors.AimartError:
    cls = getattr(errors, name, None)
    if not (isinstance(cls, type) and issubclass(cls, errors.AimartError)):
        cls = errors.AimartError
    return cls(message, hint=hint, **details)


def run_isolated[T](
    fn: Callable[..., T],
    *args: Any,
    timeout_s: float,
    memory_limit_mb: int | None = None,
    **kwargs: Any,
) -> T:
    """Run ``fn(*args, **kwargs)`` in a child process. Blocking; call from a worker thread.

    The child is killed at ``timeout_s`` (``Timeout``) or once its resident memory exceeds
    ``memory_limit_mb`` (``InputTooLarge``; 0 disables the cap). None means the configured
    ``job_memory_limit_mb`` of ``get_settings()``, the settings the server runs with: callers
    that pass nothing (``Service`` among them) were held at the field's 700 MB default, and
    ``AIMART_JOB_MEMORY_LIMIT_MB`` did nothing.
    """
    if memory_limit_mb is None:
        memory_limit_mb = get_settings().job_memory_limit_mb
    tmp = tempfile.mkdtemp(prefix="aimart-job-")
    receiver, sender = _CTX.Pipe(duplex=False)
    proc = _CTX.Process(target=_child, args=(sender, fn, args, kwargs, tmp), daemon=True)
    try:
        proc.start()
        sender.close()
        try:
            message = _wait_for_report(receiver, proc.pid, timeout_s, memory_limit_mb)
        finally:
            receiver.close()
            if proc.is_alive():
                proc.kill()
            proc.join(5)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if message is None:
        code = proc.exitcode
        signame = signal.Signals(-code).name if code is not None and code < 0 else str(code)
        raise errors.InvalidInput(
            f"The input crashed the parser ({signame}).",
            hint="The file is probably malformed or pathologically nested; you were not charged.",
        )
    if message[0] == "ok":
        return message[1]  # type: ignore[no-any-return]
    if message[0] == "err":
        raise _rebuild(*message[1:])
    # The text of an unexpected exception can carry server paths: log it, never send it.
    log.error("isolated job crashed: %s", message[2])
    raise errors.AimartError(
        f"Internal error ({message[1]}).", hint="Retry; if it persists, report it."
    )
