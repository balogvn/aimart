"""The forkserver child (engine/isolate.py): errors cross the process boundary intact, the
deadline and the memory cap kill the child, and a native crash becomes an unpaid InvalidInput,
not a dead server.
"""

from __future__ import annotations

import importlib
import os
import time
from typing import TYPE_CHECKING

import pytest

from aimart_parquet import errors
from aimart_parquet.config import Settings
from aimart_parquet.engine import isolate
from aimart_parquet.engine.isolate import _rebuild, run_isolated

if TYPE_CHECKING:
    from pathlib import Path

helpers = importlib.import_module("integration.isolate_helpers")


def test_result_round_trips() -> None:
    assert run_isolated(helpers.add, 2, 3, c=4, timeout_s=30) == 9


def test_child_error_keeps_type_code_hint_and_details() -> None:
    with pytest.raises(errors.SchemaViolation) as info:
        run_isolated(helpers.raise_schema_violation, timeout_s=30)
    exc = info.value
    assert exc.code == "schema_violation"
    assert exc.http_status == 422
    assert exc.message == "Column amount is not an integer."
    assert exc.hint == "Pass expected_schema."
    assert exc.details == {"column": "amount", "rows": [3, 7]}


def test_unexpected_exception_is_an_internal_error() -> None:
    with pytest.raises(errors.AimartError) as info:
        run_isolated(helpers.raise_value_error, timeout_s=30)
    assert type(info.value) is errors.AimartError
    assert info.value.http_status == 500
    assert "ValueError: unexpected bug" in info.value.message


def test_recursion_and_memory_errors_are_typed() -> None:
    with pytest.raises(errors.InvalidInput, match="nested too deeply"):
        run_isolated(helpers.raise_recursion, timeout_s=30)
    with pytest.raises(errors.InputTooLarge, match="more memory"):
        run_isolated(helpers.raise_memory, timeout_s=30)


def test_timeout_kills_the_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    started = time.monotonic()
    with pytest.raises(errors.Timeout, match="within 1 seconds") as info:
        run_isolated(helpers.record_pid_then_sleep, str(pid_file), 60, timeout_s=1)
    assert time.monotonic() - started < 10
    assert info.value.http_status == 504
    assert "not charged" in (info.value.hint or "")
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # killed and reaped, not left burning CPU


def test_segfault_in_the_child_is_invalid_input_naming_the_signal() -> None:
    with pytest.raises(errors.InvalidInput, match=r"crashed the parser \(SIGSEGV\)") as info:
        run_isolated(helpers.segfault, timeout_s=30)
    assert info.value.http_status == 422
    assert "not charged" in (info.value.hint or "")
    assert run_isolated(helpers.add, 1, 1, timeout_s=30) == 2  # the next job still works


def test_child_exiting_without_a_report_is_a_crash() -> None:
    with pytest.raises(errors.InvalidInput, match=r"crashed the parser \(3\)"):
        run_isolated(helpers.exit_without_reporting, timeout_s=30)


def test_rebuild_falls_back_to_the_base_error_for_unknown_names() -> None:
    exc = _rebuild("NoSuchError", "m", "h", {"k": 1})
    assert type(exc) is errors.AimartError
    assert (exc.message, exc.hint, exc.details) == ("m", "h", {"k": 1})
    assert type(_rebuild("Timeout", "m", None, {})) is errors.Timeout
    assert type(_rebuild("InvalidInput", "m", None, {})) is errors.InvalidInput


@pytest.mark.anyio
async def test_service_runs_conversions_in_the_child(make_settings: object) -> None:
    import aimart_testkit as kit
    from aimart_parquet.artifacts import build_store
    from aimart_parquet.engine.models import ConvertOptions
    from aimart_parquet.service import Service, SourceRef

    settings = make_settings(isolate_jobs=True)  # type: ignore[operator]
    service = Service(settings, build_store(settings))
    result = await service.convert(
        SourceRef(content_base64=kit.b64(b"a,b\n1,x\n2,y\n"), filename="t.csv"), ConvertOptions()
    )
    assert result.report.rows == 2
    query = await service.query(
        {"data": SourceRef(artifact_id=result.artifact.id)},
        "SELECT sum(a) FROM data",
        limit=5,
        fmt="json",
    )
    assert query.rows == [[3]]


# ---------------------------------------------------------------- memory cap (review P1)
# A 1.6 KB PDF drawing 1M glyphs took a child to 2 GB RSS in 15 s with no cap at all; on a
# 2 GiB instance running two jobs and the API that kills every in-flight request.

_MB = 1024 * 1024


def _baseline_mb() -> int:
    rss = run_isolated(helpers.own_rss, timeout_s=60, memory_limit_mb=0)
    if rss is None:  # pragma: no cover - Linux and macOS both report it
        pytest.skip("no RSS reader on this platform")
    return rss // _MB


def test_memory_cap_kills_a_job_that_keeps_allocating() -> None:
    cap = _baseline_mb() + 150
    started = time.monotonic()
    with pytest.raises(errors.InputTooLarge, match=r"memory budget \(\d+ MB\)") as info:
        run_isolated(helpers.allocate, cap + 600, 20, 40, timeout_s=60, memory_limit_mb=cap)
    assert time.monotonic() - started < 40  # killed while it holds the memory, not at the end
    assert info.value.http_status == 413
    assert "not charged" in (info.value.hint or "")
    assert info.value.details == {"limit_bytes": cap * _MB}
    assert run_isolated(helpers.allocate, 40, timeout_s=60, memory_limit_mb=cap) == 2  # under it


def test_memory_cap_stops_a_query_returning_huge_strings(tmp_path: Path) -> None:
    import aimart_testkit as kit
    from aimart_parquet.engine.tabular import query

    data = kit.write_parquet(tmp_path / "t.parquet", {"a": [1]})
    cap = _baseline_mb() + 150
    with pytest.raises(errors.InputTooLarge, match="memory budget"):
        run_isolated(
            query,
            {"data": data},
            "SELECT repeat('x', 1000000) AS s FROM range(700)",
            limit=1000,
            fmt="json",
            query_timeout=60,
            max_chars=2000,
            memory_limit="4GB",  # DuckDB's own limit out of the way: the cap must catch it
            threads=1,
            timeout_s=60,
            memory_limit_mb=cap,
        )


def test_memory_cap_is_on_by_default_at_the_settings_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    default = Settings(_env_file=None).job_memory_limit_mb
    assert default == 700
    checked: list[int] = []

    def at_the_limit(pid: int) -> int:
        checked.append(pid)
        return default * _MB

    monkeypatch.setattr(isolate, "_read_rss", at_the_limit)
    assert run_isolated(helpers.record_pid_then_sleep, os.devnull, 0.5, timeout_s=30) == "woke up"
    assert checked  # the default cap was checked, and exactly 700 MB is allowed
    pid_file = tmp_path / "pid"

    def over_once_started(pid: int) -> int:
        started = pid_file.exists() and pid_file.read_text() == str(pid)
        return default * _MB + (1 if started else 0)

    monkeypatch.setattr(isolate, "_read_rss", over_once_started)
    started = time.monotonic()
    with pytest.raises(errors.InputTooLarge, match=r"memory budget \(700 MB\)"):
        run_isolated(helpers.record_pid_then_sleep, str(pid_file), 60, timeout_s=30)
    assert time.monotonic() - started < 10
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)  # killed and reaped, not left allocating


def test_memory_cap_follows_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Round-2 review: Service does not pass the setting, so every job was capped at the 700 MB
    # field default and AIMART_JOB_MEMORY_LIMIT_MB (including 0 to disable) did nothing. With
    # no explicit cap, run_isolated now reads the settings the server runs with.
    from aimart_parquet.config import get_settings

    reads: list[int] = []

    def rss(pid: int) -> int:
        reads.append(pid)
        return 124 * _MB

    monkeypatch.setattr(isolate, "_read_rss", rss)
    monkeypatch.setenv("AIMART_JOB_MEMORY_LIMIT_MB", "123")
    get_settings.cache_clear()
    with pytest.raises(errors.InputTooLarge, match=r"memory budget \(123 MB\)"):
        run_isolated(helpers.record_pid_then_sleep, os.devnull, 5, timeout_s=30)
    monkeypatch.setenv("AIMART_JOB_MEMORY_LIMIT_MB", "0")
    get_settings.cache_clear()
    reads.clear()
    assert run_isolated(helpers.record_pid_then_sleep, os.devnull, 0.3, timeout_s=30) == "woke up"
    assert reads == []
    # An explicit cap still wins over the environment.
    with pytest.raises(errors.InputTooLarge, match=r"memory budget \(100 MB\)"):
        run_isolated(
            helpers.record_pid_then_sleep, os.devnull, 5, timeout_s=30, memory_limit_mb=100
        )


def test_memory_cap_zero_disables_it(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def huge(pid: int) -> int:
        calls.append(pid)
        return 1 << 50

    monkeypatch.setattr(isolate, "_read_rss", huge)
    assert (
        run_isolated(
            helpers.record_pid_then_sleep, os.devnull, 0.3, timeout_s=30, memory_limit_mb=0
        )
        == "woke up"
    )
    assert calls == []


def test_an_unreadable_rss_does_not_fail_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(isolate, "_read_rss", lambda pid: None)
    assert run_isolated(helpers.record_pid_then_sleep, os.devnull, 0.3, timeout_s=30) == "woke up"


def test_rss_from_proc_reads_vmrss(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(isolate, "_PROC", tmp_path)
    (tmp_path / "42").mkdir()
    (tmp_path / "42" / "status").write_text("Name:\tpython\nVmPeak:\t 9 kB\nVmRSS:\t  163840 kB\n")
    assert isolate._rss_from_proc(42) == 163840 * 1024
    (tmp_path / "43").mkdir()
    (tmp_path / "43" / "status").write_text("Name:\tzombie\nState:\tZ (zombie)\n")
    assert isolate._rss_from_proc(43) is None  # a zombie has no VmRSS line
    assert isolate._rss_from_proc(44) is None  # gone


def test_rss_reader_measures_this_process() -> None:
    rss = isolate._read_rss(os.getpid())
    if rss is None:  # pragma: no cover - Linux and macOS both report it
        pytest.skip("no RSS reader on this platform")
    assert 1 * _MB < rss < 64 * 1024 * _MB
    assert isolate._read_rss(2**22 + 12345) is None  # no such process
