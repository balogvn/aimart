"""Follow-ups to the 1886f60 merge (perf/csv-memory), found by review against d83c738.

CF1, a regression. A reader is handed a file of the pipeline's own, named after the caller's:
'input.csv.gz' for a local file 'export.csv.gz', 'unzipped.csv.gz' for a zip's member
'export.csv.gz'. Since 1886f60 the UTF-8 version of text that is not UTF-8 is renamed over that
file, where d83c738 wrote it to 'utf8.txt'. DuckDB's readers inflate a file named '.gz' or
'.zst' unless told otherwise, so a cp1252, Latin-1 or UTF-16 export kept under a compressed
name but not compressed (a browser keeps '.gz' once it has undone Content-Encoding: gzip) was a
422, 'Input is not a GZIP stream', where d83c738 converted it: a lost sale on the hosted zip
path. The JSON reader fell back to its Python parser instead (other warnings; a 413 over
100 MB). Plain UTF-8 under such a name failed the same way at d83c738 already, and converts
now too. The readers say what they read: compression 'uncompressed', or 'gzip' for a gzip the
delimited reader checked.
"""

from __future__ import annotations

import gzip
from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit
from aimart_parquet.artifacts import build_store
from aimart_parquet.engine import convert as convert_module
from aimart_parquet.engine.limits import Limits
from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.errors import InvalidInput
from aimart_parquet.service import Service

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from aimart_parquet.config import Settings

_NAMES = [f"Café Müller {i}" for i in range(100)]
_CSV = "id;name;amount\n" + "".join(f"{i};{n};{i},50\n" for i, n in enumerate(_NAMES))


def _in_the_call_directory(work: Path, name: str, data: bytes, **options: Any) -> kit.Converted:
    """Convert as the service lays out a call: the upload is ``upload`` in the work directory."""
    upload = kit.write(work / "upload", data)
    out = work / "out.parquet"
    report = convert_module.convert_file(
        upload, out, filename=name, options=ConvertOptions(**options), workdir=work, limits=Limits()
    )
    return kit.Converted(report, out)


# ------------------------------------------------------------------- CF1: text named .gz
@pytest.mark.parametrize("suffix", [".gz", ".zst"])
@pytest.mark.parametrize("encoding", ["cp1252", "latin-1", "utf-16", "utf-8"])
@pytest.mark.parametrize("layout", ["local-file", "zip-member"])
def test_text_under_a_compressed_name_is_read_as_text(
    tmp_path: Path, suffix: str, encoding: str, layout: str
) -> None:
    # d83c738 converted all but UTF-8 (its UTF-8 version was 'utf8.txt'); 4510071 refused all:
    # 'Input is not a GZIP stream' / 'Unknown frame descriptor', from the name alone.
    data = _CSV.encode(encoding)
    if layout == "local-file":  # copied in as 'input.csv.gz'
        result = kit.convert_bytes(tmp_path, f"export.csv{suffix}", data)
    else:  # extracted as 'unzipped.csv.gz'
        archive = kit.zip_bytes({f"export.csv{suffix}": data})
        result = _in_the_call_directory(tmp_path / "call", "export.zip", archive)
    assert result.rows["name"] == _NAMES
    assert result.rows["amount"] == [i + 0.5 for i in range(100)]
    detected = {"latin-1": "cp1252", "utf-16": "utf-16"}.get(encoding, encoding)
    assert result.report.encoding == detected


@pytest.mark.anyio
async def test_a_hosted_zip_of_cp1252_text_named_gz_converts(
    make_settings: Callable[..., Settings],
) -> None:
    # The reviewer's case, through the service: a 200 with 100 rows at d83c738, a 422 at 4510071.
    settings = make_settings()
    service = Service(settings, build_store(settings))
    chunks = [kit.zip_bytes({"export.csv.gz": _CSV.encode("cp1252")})]

    async def read(_: int) -> bytes:
        return chunks.pop() if chunks else b""

    result = await service.convert_upload(read, "export.zip", ConvertOptions())
    assert (result.report.rows, result.report.encoding) == (100, "cp1252")


_NDJSON = "".join(f'{{"id": {i}, "name": "{n}"}}\n' for i, n in enumerate(_NAMES[:20]))
_NDJSON_ONE_BAD = _NDJSON + '{"id": 20, "name": \n'


@pytest.mark.parametrize("suffix", [".gz", ".zst"])
@pytest.mark.parametrize("encoding", ["cp1252", "utf-8"])
def test_json_under_a_compressed_name_is_read_by_duckdb_as_text(
    tmp_path: Path, suffix: str, encoding: str
) -> None:
    # 4510071 fell back to the Python parser: the same rows, but its own warning ('skipped 1
    # malformed JSON lines'), and a 413 for a document over its 100 MB.
    data = _NDJSON_ONE_BAD.encode(encoding)
    named = kit.convert_bytes(tmp_path, f"events.ndjson{suffix}", data)
    plain = kit.convert_bytes(tmp_path, "events.ndjson", data)
    assert named.rows == plain.rows
    assert named.rows["name"] == _NAMES[:20]
    assert named.report.warnings == plain.report.warnings
    assert "skipped 1 malformed or empty JSON lines" in named.report.warnings
    member = kit.zip_bytes({f"events.ndjson{suffix}": data})
    zipped = _in_the_call_directory(tmp_path / "call", "events.zip", member)
    assert zipped.rows == plain.rows
    assert zipped.report.warnings == plain.report.warnings


_CSV500 = "id,name\n" + "".join(f"{i},n{i}\n" for i in range(500))


def test_a_gzip_member_sent_as_utf8_is_refused_not_misread(tmp_path: Path) -> None:
    # A zip's member that is itself a gzip, with encoding 'utf-8' (agents send it by default),
    # is neither checked nor transcoded. d83c738 and 4510071 let DuckDB inflate it by its name
    # while the dialect and header were sniffed from its compressed bytes: a paid 200 with 500
    # rows under three columns named from that binary ('j_1r_9_e_e_3_s_f_...'). DuckDB reads the
    # bytes that were sniffed now, and they are not text: a 422, unpaid.
    archive = kit.zip_bytes({"data.csv.gz": gzip.compress(_CSV500.encode())})
    with pytest.raises(InvalidInput, match="Could not parse the file as delimited text") as info:
        _in_the_call_directory(tmp_path / "call", "data.zip", archive, encoding="utf-8")
    assert info.value.http_status == 422
    assert "options.encoding" in (info.value.hint or "")
