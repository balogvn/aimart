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

CF2, a regression. ``safety.gzip_chunks`` let DuckDB read a gzip whose second or later member
has a header of 32,760 bytes or more (a long FNAME or FEXTRA). DuckDB 1.5.6 parses such a header
from its 32 KiB input buffer, which starts at the previous member's 8-byte trailer, and refuses
it with an InternalException: a 422 that blamed options.delimiter, where d83c738 unpacked the
file with Python and converted it. Python unpacks it as before now. Since CS5
(``test_fix_csv_followup_safety.py``: DuckDB may drop the last members of any gzip of more than
one, with no error) Python unpacks every gzip of more than one member, whatever its headers.

CF3, at d83c738 too. ``expected_schema`` over a table with no rows (a CSV that is only its
header, an empty sheet or Parquet file), or casting a column with no values to a number type,
crashed: DuckDB's ``count_if`` is NULL, not 0, when no row gives it a value, and ``int(None)``
raised TypeError, the 500 that says "Retry" after the payment, for a file that converts without
the schema. It is the same table now, typed as declared.

CF4, at d83c738 too. JSON or NDJSON whose bytes are not UTF-8, sent with options.encoding
'utf-8' (which to_utf8 trusts, unchecked), raised UnicodeDecodeError in the JSON reader's own
decoding (``_looks_like_ndjson``, ``_load_python``): a bare 500 after the payment. It is the 422
the transcoder gives a forced encoding the bytes do not fit, whose hint says to omit encoding.

XA2, at d83c738 too. Where those bytes came after the first 8 KB of NDJSON, Python never decoded
them: DuckDB's lenient pass skipped their line, and the rest was a 200 with a warning, charged.
For the JSON reader to_utf8 now checks a forced 'utf-8' to the end (``check_forced``).

XA1, at d83c738 too. ``convert.unwrap`` removed one layer of compression, and the next one's
compressed bytes were read as text: a zip of data.csv.gz, sent without options.encoding, was a
200 of 10 rows of cp1252 garbage, and so was a gzip of a gzip. It removes up to two layers now,
each to the same cap and checks, and refuses a third, and compression no reader reads (xz,
zstd), typed and unpaid. bz2 is known by more than its 'BZh': a CSV beginning so was a 422.
"""

from __future__ import annotations

import bz2
import gzip
import json
import lzma
from typing import TYPE_CHECKING, Any

import duckdb
import pyarrow as pa
import pytest

import aimart_testkit as kit
from aimart_parquet.artifacts import build_store
from aimart_parquet.engine import convert as convert_module
from aimart_parquet.engine import safety
from aimart_parquet.engine.limits import Limits
from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.errors import InputTooLarge, InvalidInput, UnsupportedFormat
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


def test_a_gzip_member_sent_as_utf8_is_read_not_misread(tmp_path: Path) -> None:
    # A zip's member that is itself a gzip, with encoding 'utf-8' (agents send it by default),
    # is neither checked nor transcoded. d83c738 and 4510071 let DuckDB inflate it by its name
    # while the dialect and header were sniffed from its compressed bytes: a paid 200 with 500
    # rows under three columns named from that binary ('j_1r_9_e_e_3_s_f_...'). f3231a2 made
    # DuckDB read the bytes that were sniffed, which are not text, and this asserted that 422
    # ('Could not parse the file as delimited text'), unpaid. Since XA1 unwrap inflates the
    # member too, and its 500 rows convert.
    archive = kit.zip_bytes({"data.csv.gz": gzip.compress(_CSV500.encode())})
    result = _in_the_call_directory(tmp_path / "call", "data.zip", archive, encoding="utf-8")
    assert result.rows["id"] == list(range(500))
    assert result.rows["name"] == [f"n{i}" for i in range(500)]


# ------------------------------------------------ CF2: a later member's header past 32 KiB
_ROWS = "id,name,amount\n" + "".join(f"{i},n{i},{i * 1.5}\n" for i in range(20_000))
_CUT = _ROWS.index("\n10000,") + 1
_LIMIT = 32_768 - 8  # DuckDB's input buffer, less the trailer before a later member's header


def _two_members(header: int, flags: int) -> bytes:
    """A gzip of _ROWS in two members, the second with a header of ``header`` bytes."""
    if flags == 8:
        fields: dict[str, bytes] = {"name": b"n" * (header - 11)}
    elif flags == 4:
        fields = {"extra": b"e" * (header - 12)}
    else:
        fields = {"extra": b"e" * 20_000, "name": b"n" * (header - 12 - 20_000 - 1)}
    first = gzip.compress(_ROWS[:_CUT].encode())
    return first + kit.gzip_member(_ROWS[_CUT:].encode(), flags, **fields)


def _unpacked(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    seen: list[Path] = []
    real = safety.decompress_stream

    def spy(src: Path, *args: Any) -> int:
        seen.append(src)
        return real(src, *args)

    monkeypatch.setattr(safety, "decompress_stream", spy)
    return seen


@pytest.mark.parametrize("flags", [8, 4, 4 | 8], ids=["fname", "fextra", "both"])
@pytest.mark.parametrize("header", [_LIMIT - 1, _LIMIT, _LIMIT + 1, 50_000])
def test_a_later_member_header_duckdb_cannot_hold_is_unpacked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flags: int, header: int
) -> None:
    # 4510071: a 422, 'INTERNAL Error: Filename resulting in GZIP header larger than defined
    # maximum (32768)' (or 'Extra field ...'), from a gzip that gunzip and d83c738 read whole.
    # _LIMIT is an FNAME of 32,749 bytes, or an FEXTRA of 32,748.
    data = _two_members(header, flags)
    assert gzip.decompress(data) == _ROWS.encode()
    unpacked = _unpacked(monkeypatch)
    result = _in_the_call_directory(tmp_path / "call", "data.csv.gz", data)
    assert result.report.rows == 20_000
    assert result.rows["id"] == list(range(20_000))
    assert result.rows["name"] == [f"n{i}" for i in range(20_000)]
    assert result.rows["amount"] == [i * 1.5 for i in range(20_000)]
    # Until CS5 a header under _LIMIT was read by DuckDB from the gzip (this asserted no unpack
    # for it): any gzip of more than one member is unpacked now.
    assert len(unpacked) == 1


@pytest.mark.parametrize("header", [_LIMIT - 1, _LIMIT])
def test_duckdb_holds_a_later_header_under_its_limit_and_the_check_passes_none(
    tmp_path: Path, header: int
) -> None:
    # DuckDB's limit on a later member's header, pinned on this DuckDB. Until CS5 the check let
    # DuckDB read the gzip under it (this asserted gzip_chunks yielded _ROWS at _LIMIT - 1).
    path = kit.write(tmp_path / "t.gz", _two_members(header, 8))
    con = duckdb.connect()
    sql = (
        f"SELECT count(*) FROM read_csv('{path}', header=true, auto_detect=false, "
        "columns={'id': 'VARCHAR', 'name': 'VARCHAR', 'amount': 'VARCHAR'}, compression='gzip')"
    )
    with pytest.raises(safety.GzipMismatch):
        b"".join(safety.gzip_chunks(path, 10**9))
    if header < _LIMIT:
        assert con.execute(sql).fetchone() == (20_000,)
    else:
        with pytest.raises(duckdb.Error, match="GZIP header larger than defined maximum"):
            con.execute(sql)


# ------------------------------------------------------ CF3: expected_schema over no rows
_DECLARED = {
    "int64": pa.int64(),
    "float64": pa.float64(),
    "decimal": pa.decimal128(10, 2),
    "string": pa.string(),
    "bool": pa.bool_(),
    "date": pa.date32(),
    "timestamp": pa.timestamp("us"),
    "timestamptz": pa.timestamp("us", tz="UTC"),
}


def _declared(name: str, type_name: str, **more: Any) -> dict[str, Any]:
    if type_name == "decimal":
        more = {"precision": 10, "scale": 2, **more}
    return {"name": name, "type": type_name, **more}


@pytest.mark.parametrize("type_name", list(_DECLARED))
@pytest.mark.parametrize("header", ["id,amount\n", "id;amount\n", "id\tamount\n"])
def test_expected_schema_over_a_header_only_csv_is_its_empty_table(
    tmp_path: Path, header: str, type_name: str
) -> None:
    # d83c738 and 4510071: TypeError ('int() argument must be ... not NoneType') in
    # TypeInferrer._text_style for a number type, in _apply_expected_schema for the others.
    plain = kit.convert_bytes(tmp_path, "header.csv", header)
    assert (plain.report.rows, [c.type for c in plain.report.columns]) == (0, ["string"] * 2)
    schema = [_declared("id", type_name), _declared("amount", type_name, nullable=False)]
    for strict in (False, True):
        typed = kit.convert_bytes(
            tmp_path, "header.csv", header, expected_schema=schema, strict=strict
        )
        assert typed.report.rows == 0
        assert typed.table.schema == pa.schema(
            [("id", _DECLARED[type_name]), ("amount", _DECLARED[type_name])]
        )
        assert all(c.rejected_count == 0 for c in typed.report.columns)


@pytest.mark.parametrize("type_name", list(_DECLARED))
def test_a_declared_column_missing_from_a_header_only_csv_is_added(
    tmp_path: Path, type_name: str
) -> None:
    schema = [_declared("id", "string"), _declared("extra", type_name)]
    typed = kit.convert_bytes(tmp_path, "header.csv", "id,amount\n", expected_schema=schema)
    assert typed.report.rows == 0
    assert typed.table.schema.names == ["id", "extra", "amount"]
    assert typed.table.schema.field("extra").type == _DECLARED[type_name]
    assert typed.column("extra").notes == ["added as all-null: missing from input"]


@pytest.mark.parametrize("type_name", ["int64", "float64", "decimal"])
def test_expected_schema_casting_a_column_with_no_values_to_a_number(
    tmp_path: Path, type_name: str
) -> None:
    # An optional amount nobody filled in: rows, but count_if over its values is NULL as well
    # (d83c738 and 4510071: TypeError in TypeInferrer._text_style).
    body = "id,amount\n1,\n2, \n3,\n"
    plain = kit.convert_bytes(tmp_path, "a.csv", body)
    assert plain.column("amount").notes == ["all values empty"]
    schema = [_declared("id", "int64"), _declared("amount", type_name)]
    for strict in (False, True):
        typed = kit.convert_bytes(tmp_path, "a.csv", body, expected_schema=schema, strict=strict)
        assert typed.rows == {"id": [1, 2, 3], "amount": [None, None, None]}
        assert typed.table.schema.field("amount").type == _DECLARED[type_name]
        assert typed.column("amount").rejected_count == 0


def test_expected_schema_over_other_readers_with_no_rows(tmp_path: Path) -> None:
    # The same two crashes from an Excel sheet that is only its header and a Parquet file
    # with no rows (d83c738 too).
    import openpyxl

    book = openpyxl.Workbook()
    ws = book.active
    assert ws is not None
    ws.append(["id", "amount"])
    book.save(tmp_path / "header.xlsx")
    sheet = kit.convert_path(tmp_path / "header.xlsx", expected_schema=[_declared("id", "int64")])
    assert sheet.report.rows == 0
    assert sheet.table.schema == pa.schema([("id", pa.int64()), ("amount", pa.string())])
    empty = kit.write_parquet(
        tmp_path / "empty.parquet",
        {"id": pa.array([], pa.int64()), "name": pa.array([], pa.string())},
    )
    schema = [_declared("id", "decimal"), _declared("name", "int64")]
    typed = kit.convert_path(empty, expected_schema=schema)
    assert typed.report.rows == 0
    assert typed.table.schema == pa.schema([("id", pa.decimal128(10, 2)), ("name", pa.int64())])


# ------------------------------------------- CF4: JSON that is not the UTF-8 it was sent as
_RECORDS = [{"id": i, "name": n} for i, n in enumerate(_NAMES[:30])]
_JSON_LAYOUTS = {
    "array": ("records.json", json.dumps(_RECORDS, ensure_ascii=False)),
    "ndjson": (
        "records.ndjson",
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in _RECORDS),
    ),
    "envelope": ("records.json", json.dumps({"data": {"items": _RECORDS}}, ensure_ascii=False)),
    "broken": ("records.json", json.dumps(_RECORDS, ensure_ascii=False)[:-1] + ",]"),
}


def _wrapped(wrap: str, name: str, data: bytes) -> tuple[str, bytes]:
    if wrap == "gz":
        return name + ".gz", gzip.compress(data)
    if wrap == "zip":
        return "records.zip", kit.zip_bytes({name: data})
    return name, data


@pytest.mark.parametrize("wrap", ["plain", "gz", "zip"])
@pytest.mark.parametrize("layout", list(_JSON_LAYOUTS))
def test_json_that_is_not_the_utf8_it_was_sent_as_is_an_unpaid_422(
    tmp_path: Path, layout: str, wrap: str
) -> None:
    # d83c738 and 4510071: UnicodeDecodeError ('utf-8' codec can't decode byte 0xe9 ...), the
    # untyped 500. The CSV reader's DuckDB refuses the same bytes itself, typed.
    name, text = _JSON_LAYOUTS[layout]
    filename, data = _wrapped(wrap, name, text.encode("cp1252"))
    with pytest.raises(InvalidInput) as info:
        _in_the_call_directory(tmp_path / "forced", filename, data, encoding="utf-8")
    assert info.value.http_status == 422
    assert info.value.message == "The file cannot be decoded as utf-8: invalid continuation byte."
    assert info.value.hint == "Pass the file's actual encoding, or omit encoding to detect it."
    # What the hint says works: the bytes' own encoding, detected or named.
    for encoding in (None, "cp1252"):
        converted = _in_the_call_directory(
            tmp_path / f"{encoding}", filename, data, encoding=encoding
        )
        assert converted.rows["name"] == _NAMES[:30]
        assert converted.report.encoding == "cp1252"


# ------------------------- XA2: NDJSON sent as UTF-8 that stops being UTF-8 after its start
def _ndjson_with_one_cp1252_line(at: int, rows: int = 5_000) -> bytes:
    """``rows`` ASCII records, and at line ``at`` one whose name is cp1252 'Café'."""
    lines = [json.dumps({"id": i, "name": f"n{i}"}).encode() for i in range(rows)]
    lines.insert(at, '{"id": -1, "name": "Café"}'.encode("cp1252"))
    return b"\n".join(lines) + b"\n"


@pytest.mark.parametrize("wrap", ["plain", "gz", "zip"])
@pytest.mark.parametrize("at", [2_500, 5_000], ids=["middle", "last"])
@pytest.mark.parametrize("encoding", ["utf-8", "UTF8"])
def test_ndjson_sent_as_utf8_that_is_not_all_utf8_is_refused_not_cut(
    tmp_path: Path, wrap: str, at: int, encoding: str
) -> None:
    # d83c738 and 0a0f26d: a 200 with 5,000 rows of 5,001 and the warning 'skipped 1 malformed or
    # empty JSON lines', charged. CF4's guards caught such bytes only where Python decodes the
    # file itself, the start of NDJSON (8 KB); DuckDB's lenient pass skipped a later line.
    data = _ndjson_with_one_cp1252_line(at)
    assert len(data) > 64 * 1024
    filename, data = _wrapped(wrap, "records.ndjson", data)
    with pytest.raises(InvalidInput) as info:
        _in_the_call_directory(tmp_path / "forced", filename, data, encoding=encoding)
    assert info.value.http_status == 422
    assert info.value.message == "The file cannot be decoded as utf-8: invalid continuation byte."
    assert info.value.hint == "Pass the file's actual encoding, or omit encoding to detect it."
    # What the hint says works: every row, the name decoded.
    for named in (None, "cp1252"):
        converted = _in_the_call_directory(tmp_path / f"{named}", filename, data, encoding=named)
        assert converted.report.rows == 5_001
        assert converted.rows["name"][at] == "Café"
        assert converted.report.warnings == []


# --------------------------------------------- XA1: a compressed file inside a zip or a gzip
_PLAIN = _CSV500.encode()
_NESTED: dict[str, tuple[str, bytes]] = {
    "zip-of-gzip": ("data.zip", kit.zip_bytes({"data.csv.gz": gzip.compress(_PLAIN)})),
    "zip-of-gzip-named-csv": ("data.zip", kit.zip_bytes({"data.csv": gzip.compress(_PLAIN)})),
    "zip-of-bz2": ("data.zip", kit.zip_bytes({"data.csv.bz2": bz2.compress(_PLAIN)})),
    "zip-of-zip": ("data.zip", kit.zip_bytes({"data.csv.zip": kit.zip_bytes({"d.csv": _PLAIN})})),
    "gzip-of-gzip": ("data.csv.gz.gz", gzip.compress(gzip.compress(_PLAIN))),
    "gzip-of-gzip-named-once": ("data.csv.gz", gzip.compress(gzip.compress(_PLAIN))),
    "gzip-of-bz2": ("data.csv.bz2.gz", gzip.compress(bz2.compress(_PLAIN))),
    "bz2-of-gzip": ("data.csv.gz.bz2", bz2.compress(gzip.compress(_PLAIN))),
    "gzip-of-zip": ("data.zip.gz", gzip.compress(kit.zip_bytes({"data.csv": _PLAIN}))),
}


@pytest.mark.parametrize(
    "options", [{}, {"encoding": "cp1252"}, {"format": "csv"}], ids=["auto", "cp1252", "csv"]
)
@pytest.mark.parametrize("layout", list(_NESTED))
def test_a_compressed_file_inside_a_zip_or_a_gzip_is_inflated(
    tmp_path: Path, layout: str, options: dict[str, str]
) -> None:
    # d83c738 and 0a0f26d removed one layer and read the next one's compressed bytes as cp1252
    # text: for a zip of data.csv.gz, a 200 with 10 rows under three columns named from the
    # binary ('y_1r_9_œa_e_e_3_...') and a warning that 35 characters could not be decoded.
    filename, data = _NESTED[layout]
    result = _in_the_call_directory(tmp_path / "call", filename, data, **options)
    assert result.rows["id"] == list(range(500))
    assert result.rows["name"] == [f"n{i}" for i in range(500)]
    assert result.report.warnings == []
    # Neither layer outlives its read (the work directory is on Cloud Run's RAM disk).
    left = {p.name for p in (tmp_path / "call").rglob("*") if p.is_file()}
    assert left == {"upload", "raw_text.parquet", "out.parquet"}


@pytest.mark.parametrize("wrap", ["zip", "gzip"])
def test_cp1252_and_json_inside_two_layers_read_as_in_one(tmp_path: Path, wrap: str) -> None:
    once = {
        "csv": kit.convert_bytes(tmp_path, "export.csv.gz", gzip.compress(_CSV.encode("cp1252"))),
        "json": kit.convert_bytes(tmp_path, "r.ndjson.gz", gzip.compress(_NDJSON.encode())),
    }
    twice = {
        "csv": ("export.csv.gz", gzip.compress(_CSV.encode("cp1252"))),
        "json": ("r.ndjson.gz", gzip.compress(_NDJSON.encode())),
    }
    for kind, (member, data) in twice.items():
        if wrap == "zip":
            filename, outer = "export.zip", kit.zip_bytes({member: data})
        else:
            filename, outer = member + ".gz", gzip.compress(data)
        result = _in_the_call_directory(tmp_path / f"{kind}-{wrap}", filename, outer)
        assert result.rows == once[kind].rows
        assert result.report.encoding == once[kind].report.encoding
        assert result.report.warnings == once[kind].report.warnings


_MB = 1024 * 1024
_BOMB = gzip.compress(b"0" * 3 * _MB)  # 3 MB of zeros in 3 KB


@pytest.mark.parametrize(
    ("filename", "data", "error", "message"),
    [
        (
            "data.zip",
            kit.zip_bytes({"data.csv.gz": _BOMB}),
            InputTooLarge,
            "Decompressed input exceeds the 1 MB limit.",
        ),
        ("data.csv.gz.gz", gzip.compress(_BOMB), InputTooLarge, "Decompressed input exceeds"),
        (
            "data.zip",
            kit.zip_bytes({"data.csv.gz": gzip.compress(_PLAIN)[:-20]}),
            InvalidInput,
            "The gzip stream is corrupt or truncated: Compressed file ended before the "
            "end-of-stream marker was reached",
        ),
        (
            "data.zip",
            kit.zip_bytes({"inner.zip": kit.zip_bytes({"a.csv": _PLAIN, "b.csv": _PLAIN})}),
            InvalidInput,
            "Zip archive must contain exactly one data file; found 2.",
        ),
    ],
    ids=["zip-of-a-gzip-bomb", "gzip-of-a-gzip-bomb", "zip-of-a-cut-gzip", "zip-of-a-zip-of-two"],
)
def test_the_second_layer_meets_the_caps_and_checks_of_the_first(
    tmp_path: Path, filename: str, data: bytes, error: type[Exception], message: str
) -> None:
    # Unpacked as a top-level stream is: the same cap (a 1 MB one here), the same typed errors,
    # and no layer written past the cap.
    work = tmp_path / "call"
    upload = kit.write(work / "upload", data)
    with pytest.raises(error) as info:
        convert_module.convert_file(
            upload,
            work / "out.parquet",
            filename=filename,
            options=ConvertOptions(),
            workdir=work,
            limits=Limits(max_decompressed_bytes=_MB),
        )
    assert str(info.value).startswith(message)
    assert all(p.stat().st_size <= _MB for p in work.rglob("*") if p.is_file())


def test_a_third_layer_is_refused_naming_each(tmp_path: Path) -> None:
    # d83c738 and 0a0f26d: '422 The file contains no rows.', from the binary of the second.
    data = kit.zip_bytes({"data.csv.gz.gz": gzip.compress(gzip.compress(_PLAIN))})
    with pytest.raises(UnsupportedFormat) as info:
        _in_the_call_directory(tmp_path / "call", "data.zip", data)
    assert info.value.http_status == 415
    assert info.value.message == (
        "The file is compressed three times over (a gzip inside a gzip inside a zip); "
        "two layers at most are unpacked."
    )
    assert info.value.hint == "Send it compressed once, e.g. data.csv.gz or a zip of data.csv."


_XZ = lzma.compress(_PLAIN)
_ZSTD = b"\x28\xb5\x2f\xfd" + bytes(range(256)) * 4  # its frame magic, then anything


@pytest.mark.parametrize(
    ("filename", "data", "kind"),
    [
        ("data.csv", _XZ, "xz"),
        ("data.zip", kit.zip_bytes({"data.csv": _XZ}), "xz"),
        ("data.csv", _ZSTD, "zstd"),
        ("data.csv.gz", gzip.compress(_ZSTD), "zstd"),
    ],
    ids=["xz-named-csv", "zip-of-xz", "zstd-named-csv", "gzip-of-zstd"],
)
@pytest.mark.parametrize("options", [{}, {"format": "csv"}], ids=["auto", "csv"])
def test_compression_no_reader_reads_is_refused_not_read_as_text(
    tmp_path: Path, filename: str, data: bytes, kind: str, options: dict[str, str]
) -> None:
    # d83c738 and 0a0f26d read xz named data.csv as cp1252 text: a 200 with 128 rows of garbage.
    with pytest.raises(UnsupportedFormat) as info:
        _in_the_call_directory(tmp_path / "call", filename, data, **options)
    assert info.value.http_status == 415
    assert info.value.message == f"The file is {kind}-compressed, which is not read here."
    assert info.value.hint == "Send it uncompressed, or compressed with gzip, bz2 or zip."


@pytest.mark.parametrize("wrap", ["plain", "gzip", "zip"])
def test_text_that_begins_like_bz2_is_read_as_text(tmp_path: Path, wrap: str) -> None:
    # bz2 begins 'BZh', its block size and its first block's magic. On 'BZh' alone, d83c738 and
    # 0a0f26d refused this CSV as a corrupt bz2 stream (422), and converted its gzip and zip.
    text = b"BZh_code,amount\n" + b"".join(b"BZ%d,%d.5\n" % (i, i) for i in range(50))
    filename, data = {
        "plain": ("codes.csv", text),
        "gzip": ("codes.csv.gz", gzip.compress(text)),
        "zip": ("codes.zip", kit.zip_bytes({"codes.csv": text})),
    }[wrap]
    result = _in_the_call_directory(tmp_path / "call", filename, data)
    codes, amounts = result.rows.values()
    assert codes == [f"BZ{i}" for i in range(50)]
    assert amounts == [i + 0.5 for i in range(50)]
