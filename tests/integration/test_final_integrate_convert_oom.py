"""DuckDB running out of memory while a convert writes its Parquet is an unpaid 413, not a 500.

Incident (final data review): the type ladder's rungs inlined each column's cleaned text several
times, so the final COPY passed DuckDB's 1 GB ``memory_limit`` with only 30 ISO-date columns of
100 rows (a 33 KB CSV). Through the service the ``duckdb.OutOfMemoryException`` became a 500
``internal_error`` whose hint said to retry, which cannot succeed. The data fix makes such files
cheap; whatever file still needs more memory than DuckDB has is now ``InputTooLarge``, with what
to change, as ``read_csv`` running out already was (test_final_isolate_gaps.py). Measured at the
integration (before the data fix): that 30-column file is now this 413, and converts as text
with ``options.infer_types=false``, as the hint says.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import duckdb
import pytest

import aimart_testkit as kit
from aimart_parquet.engine import convert
from aimart_parquet.errors import InputTooLarge

if TYPE_CHECKING:
    from pathlib import Path

CSV = b"a,b\n1,x\n2,y\n"
MESSAGE = "The file needs more memory to convert than this server allows."


def _out_of_memory(*_: Any, **__: Any) -> Any:
    raise duckdb.OutOfMemoryException(
        "Out of Memory Error: could not allocate block of size 256.0 KiB (953.6 MiB/953.6 MiB used)"
    )


@pytest.fixture
def copy_runs_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(convert, "copy_to_parquet", _out_of_memory)


@pytest.mark.anyio
async def test_the_final_copy_running_out_is_an_unpaid_413(
    copy_runs_out: None, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    body = {"content_base64": kit.b64(CSV), "filename": "x.csv"}
    r = await paid_app.paying.post("/v1/convert", json=body)
    assert r.status_code == 413, r.text  # before: 500 internal_error, "Retry"
    error = r.json()["error"]
    assert error["code"] == "input_too_large"
    assert error["message"] == MESSAGE
    assert "options.infer_types=false" in error["hint"]
    assert "not charged" in error["hint"]
    assert "Retry" not in error["hint"]
    assert "payment-response" not in r.headers
    assert len(facilitator.verified) == 1
    assert facilitator.settled == []


def test_the_413_keeps_duckdbs_exception_as_its_cause(copy_runs_out: None, tmp_path: Path) -> None:
    with pytest.raises(InputTooLarge, match=MESSAGE) as info:
        kit.convert_bytes(tmp_path, "x.csv", CSV)
    assert isinstance(info.value.__cause__, duckdb.OutOfMemoryException)
    assert "953.6 MiB" not in info.value.message  # DuckDB's text stays in the log, not the reply


def test_the_labelled_writer_running_out_is_a_413_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Labelled columns stream through pyarrow from con.sql(...).to_arrow_reader(): DuckDB runs
    # the same SELECT there, and runs out the same way.
    monkeypatch.setattr(convert, "write_parquet", _out_of_memory)
    path = kit.survey_file(tmp_path / "survey.sav", "sav")
    with pytest.raises(InputTooLarge, match=MESSAGE):
        kit.convert_path(path)
