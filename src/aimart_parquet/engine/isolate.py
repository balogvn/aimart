"""Run a job in a short-lived child process: crash isolation and a hard deadline.

Why a process and not a thread:

* Native parsers can crash, not just raise. DuckDB's ``read_json`` segfaults on JSON nested
  ~21,600 levels deep — a 50 KB file. In-process, that takes down the server mid-way through
  other callers' paid requests. In a child, it is one failed (and therefore unpaid) call.
* Threads cannot be killed. pyreadstat and calamine ignore DuckDB's interrupt, so a thread
  timeout only stops *waiting*; the CPU burn continues. A child is killed at the deadline.
* Memory is returned to the OS when the child exits, which matters on 2 GiB instances.

Children fork from a ``forkserver`` that has already imported the engine, so a job costs a
fork (milliseconds), not a fresh interpreter importing pyarrow and DuckDB.
"""

from __future__ import annotations

import multiprocessing as mp
import signal
from typing import TYPE_CHECKING, Any, TypeVar

from aimart_parquet import errors

if TYPE_CHECKING:
    from collections.abc import Callable

T = TypeVar("T")

_METHOD = "forkserver" if "forkserver" in mp.get_all_start_methods() else "spawn"
_CTX: Any = mp.get_context(_METHOD)
if _METHOD == "forkserver":
    _CTX.set_forkserver_preload(["aimart_parquet.engine.convert", "aimart_parquet.engine.tabular"])


def _child(
    conn: Any, fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
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
        conn.send(("crash", f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


def _rebuild(
    name: str, message: str, hint: str | None, details: dict[str, Any]
) -> errors.AimartError:
    cls = getattr(errors, name, None)
    if not (isinstance(cls, type) and issubclass(cls, errors.AimartError)):
        cls = errors.AimartError
    return cls(message, hint=hint, **details)


def run_isolated[T](fn: Callable[..., T], *args: Any, timeout_s: float, **kwargs: Any) -> T:
    """Run ``fn(*args, **kwargs)`` in a child process. Blocking; call from a worker thread."""
    receiver, sender = _CTX.Pipe(duplex=False)
    proc = _CTX.Process(target=_child, args=(sender, fn, args, kwargs), daemon=True)
    proc.start()
    sender.close()
    message: Any = None
    try:
        if receiver.poll(timeout_s):
            try:
                message = receiver.recv()
            except EOFError:
                message = None  # the child died before reporting: a crash
        else:
            raise errors.Timeout(
                f"The operation did not finish within {timeout_s:g} seconds.",
                hint="Send a smaller file or a simpler query; you were not charged.",
            )
    finally:
        receiver.close()
        if proc.is_alive():
            proc.kill()
        proc.join(5)
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
    raise errors.AimartError(
        f"Internal error: {message[1]}", hint="Retry; if it persists, report it."
    )
