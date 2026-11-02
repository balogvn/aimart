"""CSV memory stays flat as files grow: the job cap is 700 MB, max_decompressed_bytes 256 MB.

Measured on e8d228e: a 200 MB CSV peaked at ~700 MB resident and a 450 MB one at 1.4 GB, all
of it DuckDB (Arrow and Python held almost nothing): the text loaded into an uncompressed
VARCHAR table, 256 MB of CSV buffers, batches a 2-thread ordered COPY held back, and one hash
join per null-token IN list. Each test here pins one of them. Fixed, the 450 MB file peaks at
~350 MB in process, about the same as a 20 MB one; in the isolated child at 0348e94, 161 MiB
resident (its files, 622 MiB, are what reach the job cap now).
"""

from __future__ import annotations

import datetime as dt
import random
from typing import TYPE_CHECKING, Any

import duckdb
import pytest

import aimart_testkit as kit
from aimart_parquet import service as service_module
from aimart_parquet.artifacts import build_store
from aimart_parquet.engine import convert as convert_module
from aimart_parquet.engine.duck import connect, ident
from aimart_parquet.engine.infer import TypeInferrer
from aimart_parquet.engine.limits import Limits, size_bytes
from aimart_parquet.engine.models import ConvertOptions, DateOrder, InputFormat
from aimart_parquet.engine.readers.delimited import read_delimited
from aimart_parquet.errors import InputTooLarge
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
