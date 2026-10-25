"""Module-level job functions for the isolation tests.

They must live in a real module, not a test file, so the forkserver child can unpickle them by
name: the child imports ``integration.isolate_helpers`` through ``tests/`` on ``sys.path``
(put there by ``tests/conftest.py`` before the forkserver starts).
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

from aimart_parquet import errors


def add(a: int, b: int, *, c: int = 0) -> int:
    return a + b + c


def raise_schema_violation() -> None:
    raise errors.SchemaViolation(
        "Column amount is not an integer.",
        hint="Pass expected_schema.",
        column="amount",
        rows=[3, 7],
    )


def raise_value_error() -> None:
    raise ValueError("unexpected bug")


def raise_recursion() -> None:
    raise RecursionError


def raise_memory() -> None:
    raise MemoryError


def segfault() -> None:
    os.kill(os.getpid(), signal.SIGSEGV)


def exit_without_reporting() -> None:
    os._exit(3)


def record_pid_then_sleep(pid_file: str, seconds: float) -> str:
    Path(pid_file).write_text(str(os.getpid()))
    time.sleep(seconds)
    return "woke up"


def own_rss() -> int | None:
    from aimart_parquet.engine.isolate import _read_rss

    return _read_rss(os.getpid())


def allocate(total_mb: int, step_mb: int = 20, hold_s: float = 0.0) -> int:
    # Random bytes, so no page is shared, deduplicated or compressed away by the OS.
    blocks = [os.urandom(step_mb * 1024 * 1024) for _ in range(total_mb // step_mb)]
    # Then keep reading every page for hold_s: on a busy macOS host the pages of a job that
    # allocated and returned were paged out before the parent's poll saw them resident, and
    # the cap test failed with "DID NOT RAISE" under parallel load.
    until = time.monotonic() + hold_s
    while time.monotonic() < until:
        for block in blocks:
            _ = block[::4096]  # one byte a page brings the page back in
    return len(blocks)
