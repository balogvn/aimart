"""Every reader message built from a native library's text goes through ``without_paths``.

Incident (final-isolate review, defence in depth): convert answered pyarrow's "Could not open
Parquet input source '/tmp/aimart-artifacts/.work/tmpX/inline'" with the server's work layout
(test_final_isolate_paths.py), and the readers of compressed streams, zip archives, workbooks,
SPSS/Stata/SAS files, nested JSON and URL downloads still put the library's text in their
messages as it came. None was seen to leak a path, but each library names files in some of its
errors. Each is forced here to name one.
"""

from __future__ import annotations

import gzip
import zipfile
from typing import TYPE_CHECKING, Any

import httpcore
import pytest
from python_calamine import CalamineError

import aimart_testkit as kit
from aimart_parquet import sources
from aimart_parquet.engine import safety
from aimart_parquet.engine.readers import excel, jsonish, statistical
from aimart_parquet.errors import AimartError, FetchFailed, InvalidInput

if TYPE_CHECKING:
    from pathlib import Path

SECRET = "/srv/aimart/.work/tmpq1w2e3/inline"


def _raiser(exc: BaseException) -> Any:
    def raise_it(*_: Any, **__: Any) -> Any:
        raise exc

    return raise_it


def _assert_clean(error: AimartError, prefix: str) -> None:
    assert error.message.startswith(prefix), error.message
    assert "/srv" not in error.message
    assert "tmpq1w2e3" not in error.message
    assert "<file>" in error.message  # where the path was, so the rest still reads


def test_a_corrupt_gzip_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = kit.write(tmp_path / "x.csv.gz", gzip.compress(b"a\n1\n"))
    monkeypatch.setattr(safety.gzip, "open", _raiser(OSError(f"Bad block in '{SECRET}'")))
    with pytest.raises(InvalidInput) as info:
        safety.decompress_stream(src, tmp_path / "out", "gzip", 10**6)
    _assert_clean(info.value, "The gzip stream is corrupt or truncated: ")


def test_an_unreadable_zip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = kit.write(tmp_path / "x.zip", b"PK\x03\x04junk")
    monkeypatch.setattr(
        safety.zipfile, "ZipFile", _raiser(zipfile.BadZipFile(f"File is not a zip file: {SECRET}"))
    )
    with pytest.raises(InvalidInput) as info:
        safety.check_zip(src, 10**6)
    _assert_clean(info.value, "Not a readable zip archive: ")


def test_an_unreadable_workbook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = kit.write(tmp_path / "x.xls", b"\xd0\xcf\x11\xe0" + b"\x00" * 600)
    monkeypatch.setattr(
        excel.CalamineWorkbook, "from_path", _raiser(CalamineError(f"Cannot open {SECRET}"))
    )
    with pytest.raises(InvalidInput) as info:
        kit.convert_path(path)
    _assert_clean(info.value, "Could not read the spreadsheet: ")


def test_an_unreadable_statistical_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = kit.survey_file(tmp_path / "x.sav", "sav")
    monkeypatch.setitem(
        statistical._READERS,
        statistical.InputFormat.SAV,
        _raiser(statistical.pyreadstat.ReadstatError(f"File {SECRET} could not be opened")),
    )
    with pytest.raises(InvalidInput) as info:
        kit.convert_path(path)
    _assert_clean(info.value, "Could not read the sav file: ")


def test_nested_keys_duckdb_cannot_keep_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The second read, of the renamed records, reports what DuckDB reports about its file.
    real = jsonish._read_json_sql

    def read_elsewhere(path: Path, fmt: str, *, strict: bool = False) -> str:
        if path.name == "records_renamed.ndjson":
            return f"read_json('{SECRET}')"  # outside the sandbox: DuckDB quotes the path
        return real(path, fmt, strict=strict)

    monkeypatch.setattr(jsonish, "_read_json_sql", read_elsewhere)
    with pytest.raises(InvalidInput) as info:
        kit.convert_bytes(tmp_path, "x.json", '[{"a": {"k": 1, "K": 2}}]')
    _assert_clean(info.value, "Nested keys in this JSON could not be kept apart: ")


@pytest.mark.anyio
async def test_a_download_that_fails_in_the_network_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sources.GuardedBackend,
        "connect_tcp",
        _raiser(httpcore.ConnectError(f"[Errno 2] No such file or directory: '{SECRET}'")),
    )
    with pytest.raises(FetchFailed) as info:
        await sources.fetch_url(
            "https://example.com/a.csv", tmp_path / "d", max_bytes=10**6, deadline_s=5
        )
    _assert_clean(info.value, "Could not download the URL: ")
