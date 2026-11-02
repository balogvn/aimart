"""A non-UTF-8 text file sent compressed holds one copy of its text while it is read, not two.

The job's files count toward its 700 MB memory cap (on Cloud Run /tmp is RAM). A cp1252 CSV of
230 MiB sent as 44 MiB of gzip held the upload, the decompressed text and its full UTF-8 copy
(``text.to_utf8``) at once, and the decompressed text was never deleted: 223 MB in memory plus
508 MB of files, a 413 at the cap (unpaid, a lost sale), where the same data in UTF-8 converted
(175 + 286 MiB). The decompressed stream or zip member, which nothing reads once its UTF-8 copy
exists, is now deleted then (``safety.unwrapped``). Not a regression: 07fa450 refused both.
"""

from __future__ import annotations

import gzip
import io
import zipfile
from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit
from aimart_parquet.engine.readers import delimited, jsonish

if TYPE_CHECKING:
    from pathlib import Path

ROWS = [(i, f"Café {i} – Ærø") for i in range(50)]
CSV = ("id;name\n" + "".join(f"{i};{name}\n" for i, name in ROWS)).encode("cp1252")
NDJSON = "".join(f'{{"id": {i}, "name": "{name}"}}\n' for i, name in ROWS).encode("cp1252")


def _zip(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, data)
    return buf.getvalue()


def _files_while_reading(
    monkeypatch: pytest.MonkeyPatch, module: Any, attribute: str
) -> list[set[str]]:
    """The work directory's file names each time ``module.attribute`` is called."""
    seen: list[set[str]] = []
    real = getattr(module, attribute)

    def spy(first: Any, *args: Any, **kwargs: Any) -> Any:
        path = first if hasattr(first, "parent") else None
        if path is not None:
            seen.append({p.name for p in path.parent.iterdir() if p.is_file()})
        return real(first, *args, **kwargs)

    monkeypatch.setattr(module, attribute, spy)
    return seen


@pytest.mark.parametrize(
    ("name", "data", "intermediate"),
    [
        ("orders.csv.gz", gzip.compress(CSV), "decompressed"),
        ("orders.zip", _zip("orders.csv", CSV), "unzipped.csv"),
    ],
)
def test_a_cp1252_csv_is_staged_from_one_copy(
    name: str, data: bytes, intermediate: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _files_while_reading(monkeypatch, delimited, "_read_sample")
    converted = kit.convert_bytes(tmp_path, name, data)
    assert converted.rows["name"] == [label for _, label in ROWS]
    assert converted.report.encoding == "cp1252"
    [files] = seen  # read from the UTF-8 copy, before it is staged
    assert "utf8.txt" in files
    assert intermediate not in files  # before: the decompressed text sat beside its copy


def test_a_cp1252_ndjson_is_read_from_one_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _files_while_reading(monkeypatch, jsonish, "_first_char")
    converted = kit.convert_bytes(tmp_path, "orders.ndjson.gz", gzip.compress(NDJSON))
    assert converted.rows["name"] == [label for _, label in ROWS]
    [files] = seen
    assert "utf8.txt" in files
    assert "decompressed" not in files


def test_the_callers_own_file_and_utf8_text_are_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aimart_parquet.engine.convert import convert_file
    from aimart_parquet.engine.limits import Limits
    from aimart_parquet.engine.models import ConvertOptions

    seen = _files_while_reading(monkeypatch, delimited, "_read_sample")
    # As the service lays it out: the upload in the call's own work directory, transcoded.
    upload = kit.write(tmp_path / "call" / "upload", CSV)
    report = convert_file(
        upload,
        upload.parent / "out.parquet",
        filename="orders.csv",
        options=ConvertOptions(),
        workdir=upload.parent,
        limits=Limits(),
    )
    assert report.rows == 50
    assert {"upload", "utf8.txt"} <= seen[-1]
    assert upload.read_bytes() == CSV
    utf8 = kit.convert_bytes(tmp_path, "u.csv.gz", gzip.compress(CSV.decode("cp1252").encode()))
    assert utf8.report.encoding == "utf-8"
    assert "decompressed" in seen[-1]  # read in place: there is no second copy to save
