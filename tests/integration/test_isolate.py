"""The forkserver child (engine/isolate.py): errors cross the process boundary intact, the
deadline kills the child, and a native crash becomes an unpaid InvalidInput, not a dead server.
"""

from __future__ import annotations

import importlib
import os
import time
from typing import TYPE_CHECKING

import pytest

from aimart_parquet import errors
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
