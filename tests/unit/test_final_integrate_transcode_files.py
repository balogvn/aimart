"""A non-UTF-8 text file sent compressed holds one copy of its text while it is read, not two.

The job's files count toward its 700 MB memory cap (on Cloud Run /tmp is RAM). A cp1252 CSV of
230 MiB sent as 44 MiB of gzip held the upload, the decompressed text and its full UTF-8 copy
(``text.to_utf8``) at once, and the decompressed text was never deleted: 223 MB in memory plus
508 MB of files, a 413 at the cap (unpaid, a lost sale), where the same data in UTF-8 converted
(175 + 286 MiB). The decompressed stream or zip member, which nothing reads once its UTF-8 copy
exists, is now deleted then (``safety.unwrapped``). Not a regression: 07fa450 refused both.

Since 1886f60 (merged after this file) the reader is handed a file of the pipeline's own and
the UTF-8 version is renamed over it (``to_utf8(replace=True)``), a gzip of CSV is transcoded
straight from the stream, and a gzip of UTF-8 CSV of one member (what gzip.compress writes) is
never unpacked at all. The tests below assert the same thing, one copy of the text while it is
read and the caller's file untouched, by what the files hold rather than by the names 65183f0
gave them.
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
) -> list[dict[str, bytes]]:
    """The work directory's files, and what they hold, each time ``module.attribute`` is called."""
    seen: list[dict[str, bytes]] = []
    real = getattr(module, attribute)

    def spy(first: Any, *args: Any, **kwargs: Any) -> Any:
        path = first if hasattr(first, "parent") else None
        if path is not None:
            seen.append({p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()})
        return real(first, *args, **kwargs)

    monkeypatch.setattr(module, attribute, spy)
    return seen


@pytest.mark.parametrize(
    ("name", "data", "read_as"),
    [
        ("orders.csv.gz", gzip.compress(CSV), "input"),
        ("orders.zip", _zip("orders.csv", CSV), "unzipped.csv"),
    ],
)
def test_a_cp1252_csv_is_staged_from_one_copy(
    name: str, data: bytes, read_as: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _files_while_reading(monkeypatch, delimited, "_read_sample")
    converted = kit.convert_bytes(tmp_path, name, data)
    assert converted.rows["name"] == [label for _, label in ROWS]
    assert converted.report.encoding == "cp1252"
    [files] = seen  # read from the UTF-8 text, before it is staged
    # Before: the decompressed text sat beside its copy. At 65183f0 this asserted 'utf8.txt'
    # present and 'decompressed'/'unzipped.csv' gone; since 1886f60 the UTF-8 version is
    # renamed over the pipeline's file (the zip's member), and a gzip's is transcoded from the
    # stream over its link: either way the only file is the UTF-8 text.
    assert files == {read_as: CSV.decode("cp1252").encode()}


def test_a_cp1252_ndjson_is_read_from_one_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _files_while_reading(monkeypatch, jsonish, "_first_char")
    converted = kit.convert_bytes(tmp_path, "orders.ndjson.gz", gzip.compress(NDJSON))
    assert converted.rows["name"] == [label for _, label in ROWS]
    [files] = seen
    # At 65183f0: 'utf8.txt' present, 'decompressed' gone. A gzip of JSON is still unpacked,
    # and its UTF-8 version now takes the unpacked file's place (1886f60): one copy.
    assert files == {"decompressed": NDJSON.decode("cp1252").encode()}


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
    # At 65183f0: {'upload', 'utf8.txt'}. The reader now gets a hard link to the upload, and
    # the UTF-8 version is renamed over the link: the upload itself is never touched.
    assert seen[-1] == {"upload": CSV, "input": CSV.decode("cp1252").encode()}
    assert upload.read_bytes() == CSV
    assert upload.stat().st_nlink == 1  # the link went with the read
    gz = gzip.compress(CSV.decode("cp1252").encode())
    utf8 = kit.convert_bytes(tmp_path, "u.csv.gz", gz)
    assert utf8.report.encoding == "utf-8"
    # At 65183f0: 'decompressed' (read in place, no second copy to save). Since 1886f60 a gzip
    # of UTF-8 CSV of one member is never unpacked: DuckDB reads the gzip itself.
    assert seen[-1] == {"input": gz}
