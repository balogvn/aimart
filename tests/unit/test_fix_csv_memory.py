"""CSV memory stays flat as files grow: the job cap is 700 MB, max_decompressed_bytes 256 MB.

Measured on e8d228e: a 200 MB CSV peaked at ~700 MB resident and a 450 MB one at 1.4 GB, all
of it DuckDB (Arrow and Python held almost nothing): the text loaded into an uncompressed
VARCHAR table, 256 MB of CSV buffers, batches a 2-thread ordered COPY held back, and one hash
join per null-token IN list. Each test here pins one of them. Fixed, the 450 MB file peaks at
~350 MB in process, about the same as a 20 MB one; in the isolated child at 0348e94, 161 MiB
resident (its files, 622 MiB, are what reach the job cap now).

Files count too: on Cloud Run /tmp is RAM. Measured on 0233294, a 446 MiB CSV gzipped to
159 MiB left 777 MiB in its job directory (1.25 GiB with sort_by): the decompressed text, a
copy of the upload, a second UTF-8 copy for a cp1252 file, all kept to the end of the job. The
last tests pin each of them, and that DuckDB reads a gzip of UTF-8 text itself: the 446 MiB
of text then never land there at all.
"""

from __future__ import annotations

import bz2
import datetime as dt
import gzip
import random
from typing import TYPE_CHECKING, Any

import duckdb
import pytest

import aimart_testkit as kit
from aimart_parquet import service as service_module
from aimart_parquet.artifacts import build_store
from aimart_parquet.engine import convert as convert_module
from aimart_parquet.engine import safety
from aimart_parquet.engine import text as text_module
from aimart_parquet.engine.duck import connect, ident
from aimart_parquet.engine.infer import TypeInferrer
from aimart_parquet.engine.limits import Limits, size_bytes
from aimart_parquet.engine.models import ConvertOptions, DateOrder, InputFormat
from aimart_parquet.engine.readers import delimited as delimited_module
from aimart_parquet.engine.readers.delimited import read_delimited
from aimart_parquet.errors import InputTooLarge, InvalidInput, InvalidRequest
from aimart_parquet.service import Service, SourceRef

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from aimart_parquet.config import Settings

_MB = 1024 * 1024


def _mixed_csv(rows: int) -> str:
    """Ints, IDs with leading zeros, US amounts, dates, timestamps, yes/no, text, NA tokens."""
    lines = ["id,code,qty,price,amount,day,ts,flag,note,city"]
    cities = ("Lagos", "Berlin", "Austin", "Osaka", "Lima")
    for i in range(rows):
        note = "NA" if i % 50 == 0 else f"note {i * 7919 % 100_003}"
        lines.append(
            f"{i},{i % 100_000:06d},{i * 7 % 1000},{i * 37 % 100_000 / 100},"
            f'"{i * 7919 % 10**8 / 100:,.2f}",2020-{1 + i % 12:02d}-{1 + i % 28:02d},'
            f"2020-01-01 {i % 24:02d}:{i % 60:02d}:00,{'yes' if i % 2 else 'no'},{note},"
            f"{cities[i % 5]}"
        )
    return "\n".join(lines) + "\n"


def test_a_csv_converts_under_a_small_duckdb_memory_limit(tmp_path: Path) -> None:
    # read_csv's buffers default to 16 x max_line_size: a 4 MB file needed 256 MB of them and
    # failed below a 300 MB limit with "could not allocate block of size 256.0 MiB".
    data = _mixed_csv(40_000)
    assert len(data) > 3 * _MB
    result = kit.convert_bytes(tmp_path, "m.csv", data, limits=Limits(duckdb_memory_limit="96MB"))
    assert result.report.rows == 40_000
    assert {c.name: c.type for c in result.report.columns} == {
        "id": "int64",
        "code": "string",
        "qty": "int64",
        "price": "double",
        "amount": "double",
        "day": "date32[day]",
        "ts": "timestamp[us]",
        "flag": "bool",
        "note": "string",
        "city": "string",
    }


def test_the_raw_text_is_a_view_over_a_lossless_staged_copy(tmp_path: Path) -> None:
    # A table of the text grew with the file (1.6x it, uncompressed, in memory). The view reads
    # a zstd Parquet copy instead, which must hold exactly what the lenient read produced: these
    # rows are what CREATE TABLE raw held on e8d228e.
    work = tmp_path / "work"
    src = kit.write(
        work / "t.csv",
        'id,text,empty,space\n1,"multi\nline, with comma",,  padded  \n2,"",x," "\n'
        '3,"quote "" inside",NA,\n4,ünïcödé ✓,"",\t\n5,short\n6,a,b,c,extra\n',
    )
    con = connect(memory_limit="64MB", threads=1, allowed_dirs=[work])
    raw = read_delimited(con, src, work, ConvertOptions(), InputFormat.CSV, Limits())
    assert con.sql(f"SELECT * FROM {ident(raw.table)}").fetchall() == [
        ("1", "multi\nline, with comma", None, "  padded  "),
        ("2", None, "x", " "),
        ("3", 'quote " inside', "NA", None),
        ("4", "ünïcödé ✓", None, "\t"),
        ("5", "short", None, None),
        ("6", "a", "b", "c"),
    ]
    assert raw.warnings == [
        "1 rows had more fields than the header; extra fields were dropped (first at line 7)"
    ]
    views = con.sql("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()
    tables = con.sql("SELECT table_name FROM duckdb_tables()").fetchall()
    assert views == [("raw",)]
    assert ("raw",) not in tables


def test_a_line_at_the_cap_still_parses_with_smaller_buffers(tmp_path: Path) -> None:
    # buffer_size is now one max_line_size (16 MB), not sixteen: a cell just under it parses.
    big = "x" * (16 * _MB - 64)
    result = kit.convert_bytes(tmp_path, "long.csv", f'a,b\n1,"{big}"\n2,y\n')
    table = result.table
    assert table.column("a").to_pylist() == [1, 2]
    assert [len(v) for v in table.column("b").to_pylist()] == [len(big), 1]


def _inferrer() -> TypeInferrer:
    con = connect(memory_limit="64MB", threads=1)
    con.execute(
        "CREATE TABLE raw AS SELECT * FROM (VALUES "
        "('1', 'yes', '1.234,5', '03/04/2025', 'x'), ('NA', 'no', '2,5', '13/04/2025', 'y')"
        ") t(n, b, eu, d, s)"
    )
    return TypeInferrer(
        con,
        "raw",
        extra_null_tokens=["oops", "missing!", "?!", "nope", "none!"],
        date_order=DateOrder.AUTO,
        prefer_decimal_comma=True,
    )


def test_inferred_sql_plans_no_joins() -> None:
    # DuckDB plans an IN list of 5+ constants as a hash join, one per occurrence: the final
    # SELECT of a 40-column CSV held 90, about 600 MB for a 13 MB file. list_contains does not.
    inferrer = _inferrer()
    plans = inferrer.plan(["n", "b", "eu", "d", "s"])
    assert {s: p.type_name for s, p in plans.items()} == {
        "n": "int64",
        "b": "bool",
        "eu": "float64",
        "d": "date",
        "s": "string",
    }
    exprs = [p.expr for p in plans.values()]
    exprs += [inferrer.text_plan("s").expr, inferrer.cast_text("s", "bool", "BOOLEAN")]
    plan = "\n".join(
        row[1] for row in inferrer.con.sql(f"EXPLAIN SELECT {', '.join(exprs)} FROM raw").fetchall()
    )
    assert "JOIN" not in plan
    assert inferrer.con.sql(f"SELECT {', '.join(exprs)} FROM raw").fetchall() == [
        (1, True, 1234.5, dt.date(2025, 4, 3), "x", "x", None),
        (None, False, 2.5, dt.date(2025, 4, 13), "y", "y", True),
    ]


def test_csv_conversions_run_duckdb_on_one_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DuckDB keeps an ordered COPY in order by holding batches a faster thread finished early:
    # with 2 threads that was 400-650 MB for a 40-column CSV, with one about 100 MB. Formats
    # that are read into memory first keep the configured thread count.
    seen: list[int] = []
    real_connect = convert_module.connect

    def spy(**kwargs: Any) -> duckdb.DuckDBPyConnection:
        seen.append(kwargs["threads"])
        return real_connect(**kwargs)

    monkeypatch.setattr(convert_module, "connect", spy)
    limits = Limits(duckdb_threads=3)
    kit.convert_bytes(tmp_path, "a.csv", "a,b\n1,2\n", limits=limits)
    kit.convert_bytes(tmp_path, "a.tsv", "a\tb\n1\t2\n", limits=limits)
    kit.convert_bytes(tmp_path, "a.json", '[{"a": 1}]', limits=limits)
    assert seen == [1, 1, 3]


def test_out_of_memory_after_the_read_is_a_413(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the reader mapped DuckDB's OutOfMemoryException; a sort or write past the limit and
    # the spill cap was a 500 (unpaid, but saying nothing about the cause).
    def out_of_memory(*_: Any, **__: Any) -> None:
        raise duckdb.OutOfMemoryException("Out of Memory Error: max_temp_directory_size reached")

    monkeypatch.setattr(convert_module, "copy_to_parquet", out_of_memory)
    with pytest.raises(InputTooLarge) as info:
        kit.convert_bytes(tmp_path, "a.csv", "a,b\n1,2\n", sort_by=["a"])
    assert info.value.http_status == 413
    assert info.value.hint is not None
    assert "not charged" in info.value.hint


# The spill cap is what the cap leaves DuckDB's memory limit, not the whole cap: the watchdog
# counts spill files with resident memory (integrate/final), so 400 MiB of memory plus 700 MiB
# of spill was a cap DuckDB could never reach before the watchdog killed the job (updated at the
# merge; these rows said 700MiB, 1500MiB, 700MiB, 700MiB and 350MiB).
@pytest.mark.parametrize(
    ("overrides", "memory_limit", "max_temp_size"),
    [
        # The 1 GB default sat above the 700 MB cap: the watchdog killed a sort before it spilled.
        ({}, "400MiB", "300MiB"),
        ({"job_memory_limit_mb": 1500}, "1GB", "546MiB"),  # 1500 MiB - 10**9 bytes
        ({"duckdb_memory_limit": "256MB"}, "256MB", "455MiB"),
        ({"duckdb_memory_limit": "75%"}, "400MiB", "300MiB"),  # a form we do not read: the cap
        ({"job_memory_limit_mb": 350}, "128MiB", "222MiB"),  # never below DuckDB's working set
        ({"job_memory_limit_mb": 100}, "128MiB", "0MiB"),  # nothing left to spill into
        ({"job_memory_limit_mb": 0}, "1GB", "2GB"),  # no cap, nothing to stay under
        ({"isolate_jobs": False}, "1GB", "2GB"),  # in-process jobs have no watchdog
    ],
)
def test_duckdb_limits_stay_under_the_job_cap(
    make_settings: Callable[..., Settings],
    overrides: dict[str, Any],
    memory_limit: str,
    max_temp_size: str,
) -> None:
    settings = make_settings(**{"isolate_jobs": True, "job_memory_limit_mb": 700, **overrides})
    limits = Limits.from_settings(settings)
    assert (limits.duckdb_memory_limit, limits.duckdb_max_temp_size) == (
        memory_limit,
        max_temp_size,
    )
    if settings.isolate_jobs and settings.job_memory_limit_mb >= 128:
        memory, spill = (size_bytes(v) for v in (memory_limit, max_temp_size))
        assert memory is not None
        assert spill is not None
        assert memory + spill <= settings.job_memory_limit_mb * _MB


def test_a_sort_past_the_spill_cap_is_duckdbs_own_413(tmp_path: Path) -> None:
    # Under the derived limits DuckDB stops a sort at its spill cap itself, before the
    # watchdog (which polls every 0.1 s, and counts the spill as memory) would: a 413 whose
    # hint names sort_by, from DuckDB's real out-of-memory error, not a mocked one.
    rnd = random.Random(1)
    rows = "".join(f"{i},{rnd.getrandbits(128):032x}\n" for i in range(600_000))  # 24 MB
    limits = Limits(duckdb_memory_limit="48MB", duckdb_max_temp_size="1MiB")
    with pytest.raises(InputTooLarge) as info:
        kit.convert_bytes(tmp_path, "s.csv", "id,k\n" + rows, sort_by=["k"], limits=limits)
    assert "max_temp_directory_size" in str(info.value.__cause__)
    assert info.value.hint is not None
    assert "drop sort_by" in info.value.hint
    assert "not charged" in info.value.hint


@pytest.mark.parametrize(
    ("size", "expected"),
    [("1GB", 10**9), ("512MiB", 512 * _MB), ("1.5 gb", 15 * 10**8), ("4096", 4096), ("75%", None)],
)
def test_size_bytes(size: str, expected: int | None) -> None:
    assert size_bytes(size) == expected


@pytest.mark.anyio
async def test_jobs_run_with_the_derived_limits(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Convert and query run under the same cap, so both take the one derived answer; the query
    # path used to read settings.duckdb_memory_limit (1 GB) itself. The query reports what its
    # DuckDB actually ran with.
    def in_process(fn: Callable[..., Any], *args: Any, timeout_s: float, **kw: Any) -> Any:
        kw.pop("memory_limit_mb")
        kw.pop("job_dirs")  # the watchdog's directories (integrate/final): no watchdog here
        return fn(*args, **kw)

    converts: list[dict[str, Any]] = []
    real_connect = convert_module.connect

    def spy(**kwargs: Any) -> duckdb.DuckDBPyConnection:
        converts.append(kwargs)
        return real_connect(**kwargs)

    monkeypatch.setattr(service_module, "run_isolated", in_process)
    monkeypatch.setattr(convert_module, "connect", spy)
    settings = make_settings(isolate_jobs=True, job_memory_limit_mb=640)
    service = Service(settings, build_store(settings))
    art = (
        await service.convert(
            SourceRef(content_base64=kit.b64(b"a,b\n1,2\n"), filename="s.csv"), ConvertOptions()
        )
    ).artifact
    assert converts[0]["memory_limit"] == "340MiB"
    assert converts[0]["max_temp_size"] == "300MiB"  # was 640MiB: memory plus spill fit the cap
    queried = await service.query(
        {"data": SourceRef(artifact_id=art.id)},
        "SELECT current_setting('memory_limit'), current_setting('max_temp_directory_size') "
        "FROM data LIMIT 1",
        limit=1,
        fmt="json",
    )
    assert queried.rows == [["340.0 MiB", "300.0 MiB"]]


# ----------------------------------------------------------------- files on the RAM disk
_CITIES = "id,city\n1,Zürich\n2,Bogotá\n3,Lagos\n"


def _names(root: Path) -> set[str]:
    return {p.name for p in root.rglob("*") if p.is_file()}


def _convert_in_the_call_directory(work: Path, name: str, data: bytes) -> kit.Converted:
    """Convert as the service lays a call out (7940b8b): the upload in the call's own work
    directory, named ``upload``, its name passed beside it."""
    upload = kit.write(work / "upload", data)
    out = work / "out.parquet"
    report = convert_module.convert_file(
        upload, out, filename=name, options=ConvertOptions(), workdir=work, limits=Limits()
    )
    return kit.Converted(report, out)


@pytest.mark.parametrize(
    ("name", "data", "left"),
    [
        ("a.csv", _CITIES.encode(), {"raw_text.parquet"}),
        ("a.csv.gz", gzip.compress(_CITIES.encode()), {"raw_text.parquet"}),
        ("a.csv.bz2", bz2.compress(_CITIES.encode()), {"raw_text.parquet"}),
        ("a.zip", kit.zip_bytes({"a.csv": _CITIES.encode()}), {"raw_text.parquet"}),
        ("cp1252.csv.gz", gzip.compress(_CITIES.encode("cp1252")), {"raw_text.parquet"}),
        ("cp1252.csv", _CITIES.encode("cp1252"), {"raw_text.parquet"}),
        ("a.json.gz", gzip.compress('[{"city": "Zürich"}]'.encode("cp1252")), set()),
    ],
    ids=["csv", "gzip", "bz2", "zip", "cp1252-gzip", "cp1252", "json-gzip"],
)
@pytest.mark.parametrize("layout", ["call-directory", "elsewhere"])
def test_no_input_file_outlives_the_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    data: bytes,
    left: set[str],
    layout: str,
) -> None:
    # Nothing reads the input once the reader returns (raw is a table, or a view over the
    # staged text), yet the decompressed text, the upload's copy and the UTF-8 copy stayed
    # through inference, the write and a sort's spill. The source stays: the service's upload
    # in the call's directory (where it is written since 7940b8b), or a file elsewhere (a
    # local caller's), which is copied in and the copy deleted.
    at_finish: list[set[str]] = []
    real_finish = convert_module._finish

    def spy(con: duckdb.DuckDBPyConnection, raw: Any, out_path: Path, *args: Any) -> Any:
        at_finish.append(_names(out_path.parent))
        return real_finish(con, raw, out_path, *args)

    monkeypatch.setattr(convert_module, "_finish", spy)
    if layout == "call-directory":
        result = _convert_in_the_call_directory(tmp_path / "call", name, data)
        src, left = tmp_path / "call" / "upload", left | {"upload"}
    else:
        src = kit.write(tmp_path / "mine" / name, data)
        result = kit.convert_path(src)  # in a work directory next to it
    assert at_finish == [left]
    assert src.read_bytes() == data
    text = '[{"city": "Zürich"}]' if "json" in name else _CITIES
    assert result.report.estimated_tokens_raw == len(text.encode()) // 4  # the UTF-8 text's size


def test_a_workbook_is_gone_once_read_and_still_sized(tmp_path: Path) -> None:
    # A reader without text_bytes is sized from its input, which must happen before the delete.
    book = kit.messy_workbook(tmp_path / "upload" / "book.xlsx")
    result = kit.convert_path(book)
    assert result.report.estimated_tokens_raw == book.stat().st_size // 4
    assert _names(result.path.parent) == {"out.parquet"}


def test_a_transcoded_csv_is_staged_from_one_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # to_utf8 wrote utf8.txt next to the decompressed cp1252 text, and the stage read it with
    # both on the RAM disk: twice the CSV plus the staged text. The text is now transcoded
    # straight from the gzip, and the UTF-8 version takes the input's place.
    work = tmp_path / "call"
    at_transcode: list[dict[str, int]] = []
    real = text_module._transcode

    def spy(read: Any, dst: Path, encoding: str) -> int:
        at_transcode.append({p.name: p.stat().st_ino for p in work.rglob("*") if p.is_file()})
        return real(read, dst, encoding)

    monkeypatch.setattr(text_module, "_transcode", spy)
    during = _inodes_during_read(monkeypatch, work)
    data = gzip.compress(_CITIES.encode("cp1252"))
    result = _convert_in_the_call_directory(work, "w.csv.gz", data)
    upload = work / "upload"
    ino = upload.stat().st_ino
    assert at_transcode == [{"upload": ino, "input": ino}]  # the upload, and a link to it
    assert set(during[0]) == {"upload", "input", "raw_text.parquet"}
    assert during[0]["input"] != ino  # the UTF-8 text
    assert upload.read_bytes() == data
    assert result.report.encoding == "cp1252"
    assert result.table.column("city").to_pylist() == ["Zürich", "Bogotá", "Lagos"]


def test_an_upload_is_unpacked_where_it_lies(tmp_path: Path) -> None:
    # Every input outside the work directory was copied in before unpacking: up to 50 MB more
    # for the whole job. Python reads the archive; only DuckDB is held to the work directory.
    src = kit.write(tmp_path / "upload" / "a.csv.gz", gzip.compress(b"a\n1\n"))
    work = tmp_path / "work"
    work.mkdir()
    path, _ = convert_module.unwrap(src, "a.csv.gz", work, Limits())
    assert _names(work) == {path.name} == {"decompressed"}


@pytest.mark.parametrize("inside", [False, True])
def test_a_plain_input_is_linked_not_copied(tmp_path: Path, inside: bool) -> None:
    # An upload, a download and a stored artifact's link are in the work directory: the reader
    # gets a hard link it may consume, at no cost in space. 1886f60 linked a file from anywhere;
    # one elsewhere is a local caller's own and is copied, never linked, as on main
    # (test_final_isolate_service.py::test_a_local_callers_own_file_is_copied_never_linked):
    # a link would put the converter one bug away from writing to the caller's file.
    work = tmp_path / "work"
    src = kit.write((work if inside else tmp_path / "mine") / "a.csv", b"a\n1\n")
    work.mkdir(exist_ok=True)
    path, _ = convert_module.unwrap(src, "a.csv", work, Limits())
    assert path != src
    assert path.read_bytes() == src.read_bytes()
    assert (path.stat().st_ino == src.stat().st_ino) is inside
    assert src.stat().st_nlink == (2 if inside else 1)


@pytest.mark.parametrize(
    ("name", "data"),
    [("input.csv", b"a\n1\n2\n"), ("input", gzip.compress(b"a\n1\n2\n"))],
    ids=["plain", "gzip"],
)
def test_a_source_with_the_links_name_survives(tmp_path: Path, name: str, data: bytes) -> None:
    work = tmp_path / "work"
    src = kit.write(work / name, data)
    report = convert_module.convert_file(
        src,
        work / "out.parquet",
        filename="input.csv",
        options=ConvertOptions(),
        workdir=work,
        limits=Limits(),
    )
    assert report.rows == 2
    assert src.read_bytes() == data


# ------------------------------------------------------- a gzip that DuckDB reads as it is
_TEXT = "id,city\n" + "".join(f"{i},Zürich {i}\n" for i in range(3000))


def _inodes_during_read(monkeypatch: pytest.MonkeyPatch, root: Path) -> list[dict[str, int]]:
    """The files under ``root`` (and their inodes) while the stage's file is still in place."""
    seen: list[dict[str, int]] = []
    real = delimited_module._malformed_row_warnings

    def spy(con: duckdb.DuckDBPyConnection, csv_args: str) -> list[str]:
        seen.append({p.name: p.stat().st_ino for p in root.rglob("*") if p.is_file()})
        return real(con, csv_args)

    monkeypatch.setattr(delimited_module, "_malformed_row_warnings", spy)
    return seen


@pytest.mark.parametrize(
    ("name", "data", "delimiter"),
    [
        ("a.csv.gz", gzip.compress(_TEXT.encode()), ","),
        ("a.tsv.gz", gzip.compress(_TEXT.replace(",", "\t").encode()), "\t"),
        (
            "a.gz",
            kit.gzip_member(_TEXT[:5000].encode(), 8) + gzip.compress(_TEXT[5000:].encode()),
            ",",
        ),
        ("a.csv.gz", kit.gzip_member(("\ufeff" + _TEXT).encode(), 4), ","),
    ],
    ids=["csv", "tsv", "two-members-fname", "bom-fextra"],
)
def test_a_gzip_of_utf8_text_is_never_unpacked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, data: bytes, delimiter: str
) -> None:
    # unwrap wrote all of it to the RAM disk for DuckDB to read: a 446 MiB CSV gzipped to
    # 159 MiB held 526 MiB of files at the read, now the upload plus the staged text, 80 MiB.
    work = tmp_path / "call"
    during = _inodes_during_read(monkeypatch, work)
    result = _convert_in_the_call_directory(work, name, data)
    src = work / "upload"
    assert during == [
        {
            "upload": src.stat().st_ino,
            "input": src.stat().st_ino,
            "raw_text.parquet": during[0]["raw_text.parquet"],
        }
    ]  # input: the upload itself, hard-linked in the work directory
    assert src.read_bytes() == data
    assert result.report.delimiter == delimiter
    assert result.rows["city"] == [f"Zürich {i}" for i in range(3000)]
    assert result.report.estimated_tokens_raw == len(gzip.decompress(data)) // 4


@pytest.mark.parametrize(
    "data",
    [
        gzip.compress(_TEXT.encode()) + b"\0" * 8,
        kit.gzip_member(_TEXT.encode(), 16),
        kit.gzip_member(_TEXT.encode(), 2),
        kit.gzip_member(_TEXT.encode(), 1),
        kit.gzip_member(_TEXT.encode("cp1252"), 16),
    ],
    ids=["zero-padding", "comment", "header-crc", "ftext", "comment-cp1252"],
)
def test_a_gzip_duckdb_cannot_read_as_python_does_is_unpacked(tmp_path: Path, data: bytes) -> None:
    # DuckDB 1.5 refuses these headers and the padding Python's gzip module skips: each is
    # unpacked over the input, as unwrap did, and reads as it did.
    result = kit.convert_bytes(tmp_path, "a.csv.gz", data)
    assert result.rows["city"] == [f"Zürich {i}" for i in range(3000)]
    assert result.report.warnings == []


@pytest.mark.parametrize(
    ("data", "detail"),
    [
        (
            gzip.compress(_TEXT.encode())[:-8] + b"\0\0\0\0" + gzip.compress(_TEXT.encode())[-4:],
            "CRC",
        ),
        (gzip.compress(_TEXT.encode())[:-4] + b"\0\0\0\0", "length"),
        (gzip.compress(_TEXT.encode())[:-4], "end-of-stream"),
        (gzip.compress(_TEXT.encode())[:200], "end-of-stream"),
        (gzip.compress(_TEXT.encode()) + b"garbage", "Not a gzipped file"),
        (gzip.compress(_TEXT.encode("cp1252"))[:-8] + b"\0" * 8, "CRC"),
        (gzip.compress(_TEXT.encode("cp1252"))[:-4], "end-of-stream"),
    ],
    ids=[
        "bad-crc",
        "bad-length",
        "truncated-trailer",
        "truncated-body",
        "trailing-garbage",
        "bad-crc-cp1252",
        "truncated-cp1252",
    ],
)
def test_a_corrupt_gzip_is_refused_as_before(tmp_path: Path, data: bytes, detail: str) -> None:
    # DuckDB reads gzip without checking a member's CRC or length, and a truncated one as far
    # as it inflates: rows from a corrupt file would have been converted, and charged for.
    with pytest.raises(InvalidInput, match=f"gzip stream is corrupt or truncated: .*{detail}"):
        kit.convert_bytes(tmp_path, "a.csv.gz", data)


@pytest.mark.parametrize(
    ("encoding", "row"),
    [(None, "1,2\n"), ("utf-8", "1,2\n"), (None, "1,ü\n"), ("cp1252", "1,ü\n")],
    ids=["detected", "named", "detected-cp1252", "named-cp1252"],
)
def test_a_gzip_bomb_of_text_stops_at_the_cap(
    tmp_path: Path, encoding: str | None, row: str
) -> None:
    # Unpacking was what held a gzip bomb to max_decompressed_bytes; DuckDB has no cap, and a
    # transcode from the gzip has only its own.
    data = gzip.compress(b"a,b\n" + row.encode("cp1252") * (1024 * 1024))
    with pytest.raises(InputTooLarge, match="exceeds the 1 MB limit"):
        kit.convert_bytes(
            tmp_path,
            "a.csv.gz",
            data,
            limits=Limits(max_decompressed_bytes=1024 * 1024),
            encoding=encoding,
        )


def test_an_unknown_encoding_is_refused_after_a_corrupt_gzip_as_before(tmp_path: Path) -> None:
    # unwrap unpacked the gzip before to_utf8 looked up the encoding: a corrupt one says so first.
    good = gzip.compress(_CITIES.encode())
    with pytest.raises(InvalidRequest, match="Unknown encoding"):
        kit.convert_bytes(tmp_path, "a.csv.gz", good, encoding="no-such-codec")
    with pytest.raises(InvalidInput, match="gzip stream is corrupt"):
        kit.convert_bytes(tmp_path, "a.csv.gz", good[:-4], encoding="no-such-codec")


def test_a_gzip_in_a_named_encoding_is_transcoded(tmp_path: Path) -> None:
    data = gzip.compress(_CITIES.encode("cp1252"))
    result = kit.convert_bytes(tmp_path, "a.csv.gz", data, encoding="cp1252")
    assert (result.report.encoding, result.rows["city"]) == (
        "cp1252",
        ["Zürich", "Bogotá", "Lagos"],
    )


@pytest.mark.parametrize(
    "data",
    [
        kit.gzip_member(_TEXT[:7].encode(), 8) + kit.gzip_member(_TEXT[7:].encode(), 4 | 8),
        gzip.compress(b"") + gzip.compress(_TEXT.encode()) + gzip.compress(b""),
        gzip.compress("\ufeff".encode()[:2]) + gzip.compress(("\ufeff" + _TEXT).encode()[2:]),
        b"".join(gzip.compress(_TEXT[i : i + 997].encode()) for i in range(0, len(_TEXT), 997)),
        gzip.compress(_TEXT.encode(), compresslevel=0),
    ],
    ids=["fields-in-every-member", "empty-members", "bom-split", "many-members", "stored"],
)
def test_duckdb_reads_a_gzip_the_check_passes_as_python_does(tmp_path: Path, data: bytes) -> None:
    # What read_delimited relies on, on this DuckDB: an upgrade that reads one differently
    # fails here, not in a caller's rows.
    gz = kit.write(tmp_path / "t.gz", data)
    plain = kit.write(tmp_path / "t.csv", gzip.decompress(data))
    assert b"".join(safety.gzip_chunks(gz, 10**9)) == plain.read_bytes()
    con = duckdb.connect()
    args = "header=true, auto_detect=false, columns={'id': 'VARCHAR', 'city': 'VARCHAR'}"
    rows = [
        con.sql(f"SELECT * FROM read_csv('{p}', {args}{c})").fetchall()
        for p, c in [(gz, ", compression='gzip'"), (plain, "")]
    ]
    assert rows[0] == rows[1]
    assert len(rows[0]) == 3000
