"""Regression tests for preview/query results that were wrong but returned 200 (and were charged).

* Duplicate result column names (every ``SELECT *`` join) shifted values under the wrong header.
* Result size was bounded only by DuckDB's buffer manager, which never sees the Arrow batches,
  the input scan or the rendered rows: one query or one tiny Parquet file could take gigabytes.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import textwrap
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import aimart_testkit as kit
from aimart_parquet.engine import tabular
from aimart_parquet.engine.tabular import preview, query
from aimart_parquet.errors import InputTooLarge, InvalidInput, QueryFailed, QueryRejected

JOIN = "SELECT * FROM customers c JOIN orders o ON c.id = o.id ORDER BY c.id"


def run(tables, sql, *, limit=100, fmt="json", max_chars=2000):
    return query(
        tables,
        sql,
        limit=limit,
        fmt=fmt,
        query_timeout=10,
        max_chars=max_chars,
        memory_limit="256MB",
        threads=1,
    )


@pytest.fixture
def joined(tmp_path):
    return {
        "customers": kit.write_parquet(
            tmp_path / "c.parquet", {"id": [1, 2], "name": ["alice", "bob"]}
        ),
        "orders": kit.write_parquet(
            tmp_path / "o.parquet",
            {"id": [1, 2], "name": ["ACME", "Globex"], "amount": [100.0, 200.0]},
        ),
    }


class TestDuplicateColumnNames:
    def test_select_star_join_keeps_every_value_under_its_own_header(self, joined) -> None:
        result = run(joined, JOIN)
        assert result.columns == ["id", "name", "id", "name", "amount"]
        assert result.rows == [[1, "alice", 1, "ACME", 100.0], [2, "bob", 2, "Globex", 200.0]]
        assert result.types == ["int64", "string", "int64", "string", "double"]

    def test_csv(self, joined) -> None:
        result = run(joined, JOIN, fmt="csv")
        assert result.text == "id,name,id,name,amount\n1,alice,1,ACME,100.0\n2,bob,2,Globex,200.0\n"

    def test_markdown(self, joined) -> None:
        result = run(joined, JOIN, fmt="markdown")
        assert result.text == (
            "| id | name | id | name | amount |\n|---|---|---|---|---|\n"
            "| 1 | alice | 1 | ACME | 100.0 |\n| 2 | bob | 2 | Globex | 200.0 |"
        )

    def test_names_differing_only_by_case_and_repeated_expressions(self, joined) -> None:
        result = run(joined, "SELECT 1 AS a, 2 AS a, 3 AS A, 4, 4 FROM customers LIMIT 1")
        assert result.columns == ["a", "a", "A", "4", "4"]
        assert result.rows == [[1, 2, 3, 4, 4]]

    def test_preview_of_a_file_with_repeated_column_names(self, tmp_path) -> None:
        path = tmp_path / "dup.parquet"
        table = pa.Table.from_arrays(
            [pa.array([1, 2]), pa.array(["x", "y"]), pa.array([3, 4])], names=["k", "v", "k"]
        )
        pq.write_table(table, path)
        result = preview(path, columns=None, offset=0, limit=5, fmt="json", max_chars=99)
        assert result.columns == ["k", "v", "k"]
        assert result.rows == [[1, "x", 3], [2, "y", 4]]


def _peak_rss_growth_mb(code: str, *args: str) -> tuple[float, str]:
    """Run ``code`` in a fresh interpreter; return (peak RSS growth in MB, its stdout)."""
    script = (
        textwrap.dedent(
            """
        import resource, sys
        def peak():
            # bytes on macOS, KiB on Linux
            scale = 1 if sys.platform == "darwin" else 1024
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale / 2**20
        import pyarrow, pyarrow.parquet, duckdb
        from aimart_parquet.engine.tabular import preview, query
        before = peak()
        """
        )
        + textwrap.dedent(code)
        + "\nprint(f'GROWTH {peak() - before:.0f}')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script, *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout
    growth = float(out.rsplit("GROWTH ", 1)[1])
    return growth, out


def _dictionary_bomb(path: Path, rows: int = 20_000, width: int = 65_536) -> Path:
    """A few hundred bytes on disk; ``rows * width`` bytes once decoded as plain strings.

    store_schema=False drops the Arrow schema, so readers see a string column, not a dictionary.
    """
    values = pa.DictionaryArray.from_arrays(
        pa.array([0] * rows, pa.int32()), pa.array(["x" * width])
    )
    pq.write_table(
        pa.table({"s": values}),
        path,
        store_schema=False,
        row_group_size=rows,
        compression="zstd",
    )
    return path


needs_resource = pytest.mark.skipif(
    sys.platform == "win32", reason="peak RSS comes from the resource module"
)


class TestMemoryBounds:
    @needs_resource
    def test_preview_of_a_dictionary_bomb_decodes_only_a_few_rows(self, tmp_path) -> None:
        bomb = _dictionary_bomb(tmp_path / "bomb.parquet")
        assert bomb.stat().st_size < 1024  # 1.3 GB of text in under a kilobyte
        growth, out = _peak_rss_growth_mb(
            """
            from pathlib import Path
            for limit in (1, 500):
                r = preview(Path(sys.argv[1]), columns=None, offset=0, limit=limit, fmt="json",
                            max_chars=2000)
                print(r.row_count, r.total_rows, len(r.rows[0][0]))
            """,
            str(bomb),
        )
        assert "1 20000 2000" in out
        assert "500 20000 2000" in out
        # Before: pads.dataset().take() decoded the whole row group, 2.3 GB for one row.
        assert growth < 300, out

    @needs_resource
    def test_huge_text_cells_are_cut_inside_duckdb(self, tmp_path) -> None:
        data = kit.write_parquet(tmp_path / "t.parquet", {"id": [1]})
        growth, out = _peak_rss_growth_mb(
            """
            from pathlib import Path
            r = query({"data": Path(sys.argv[1])},
                      "SELECT repeat('x', 1000000) AS s FROM range(1000)", limit=1000,
                      fmt="json", query_timeout=60, max_chars=2000, memory_limit="1GB",
                      threads=1)
            print(r.row_count, len(r.rows[0][0]), r.types)
            """,
            str(data),
        )
        assert "1000 2000 ['string']" in out
        # Before: 1 GB of strings went through DuckDB's result chunks and Arrow batches, outside
        # memory_limit, then into Python: 2.3 GB peak.
        assert growth < 300, out

    def test_text_and_blob_cells_render_exactly_as_before(self, joined) -> None:
        result = run(
            joined,
            "SELECT repeat('é', 50) AS t, repeat('ab'::BLOB, 50) AS b, 'short' AS s, "
            "NULL::VARCHAR AS n FROM customers ORDER BY id LIMIT 1",
            max_chars=9,
        )
        assert result.rows == [["é" * 8 + "…", (b"ab" * 50).hex()[:9], "short", None]]
        assert result.types == ["string", "binary", "string", "string"]

    def test_the_cut_keeps_order_names_and_trailing_comments(self, joined) -> None:
        result = run(
            joined,
            "SELECT c.name, o.name, c.id FROM customers c JOIN orders o USING (id) "
            "ORDER BY c.id DESC -- newest first",
            max_chars=3,
        )
        assert result.columns == ["name", "name", "id"]
        assert result.rows == [["bob", "Gl…", 2], ["al…", "AC…", 1]]

    def test_a_query_the_wrapper_cannot_bind_runs_as_written(self, joined, monkeypatch) -> None:
        real = tabular.validate_select

        def rejects_the_wrapper(sql: str) -> str:
            if sql.startswith("SELECT left("):
                raise QueryRejected("no", hint="no")
            return real(sql)

        monkeypatch.setattr(tabular, "validate_select", rejects_the_wrapper)
        result = run(joined, "SELECT name FROM customers ORDER BY id", max_chars=3)
        assert result.rows == [["al…"], ["bob"]]  # still cut, by render, as before

    @pytest.mark.parametrize("sql", ["DESCRIBE customers", "SUMMARIZE customers"])
    def test_describe_and_summarize_still_work(self, joined, sql) -> None:
        result = run(joined, sql)
        assert result.rows is not None
        assert [r[0] for r in result.rows] == ["id", "name"]

    def test_query_result_over_the_byte_cap_is_refused_unpaid(self, joined, monkeypatch) -> None:
        monkeypatch.setattr(tabular, "MAX_RESULT_BYTES", 100_000)
        # Long lists are not cut (round 2: nested text is), so 1000 integers a row reach the cap.
        with pytest.raises(InputTooLarge) as info:
            run(joined, "SELECT range(1000) AS l FROM range(1000)", limit=1000, max_chars=50)
        assert info.value.http_status == 413
        assert info.value.details == {"limit_bytes": 100_000}
        assert info.value.hint is not None
        assert "limit" in info.value.hint
        # Text is counted after the cut, top-level or nested, so the same volume is fine.
        for sql in (
            "SELECT repeat('x', 1000) AS s FROM range(1000)",
            "SELECT [repeat('x', 1000)] AS l FROM range(1000)",
        ):
            assert run(joined, sql, limit=1000, max_chars=50).row_count == 1000

    def test_preview_over_the_byte_cap_is_refused_unpaid(self, tmp_path, monkeypatch) -> None:
        # Integers: nested text is cut (round 2), so it no longer reaches the cap.
        path = kit.write_parquet(tmp_path / "l.parquet", {"l": [list(range(1000))] * 200})
        monkeypatch.setattr(tabular, "MAX_RESULT_BYTES", 100_000)
        with pytest.raises(InputTooLarge):
            preview(path, columns=None, offset=0, limit=200, fmt="json", max_chars=50)
        assert (
            preview(path, columns=None, offset=0, limit=5, fmt="json", max_chars=50).row_count == 5
        )


class TestPreviewReadsTheRightRows:
    @pytest.mark.parametrize("row_group_size", [100, 1000, 37])
    @pytest.mark.parametrize(("offset", "limit"), [(0, 5), (63, 3), (130, 200), (995, 50)])
    def test_offsets_across_batches_and_row_groups(
        self, tmp_path, row_group_size, offset, limit
    ) -> None:
        path = kit.write_parquet(
            tmp_path / "n.parquet",
            {"i": list(range(1000)), "s": [f"v{i}" for i in range(1000)]},
            row_group_size=row_group_size,
        )
        result = preview(
            path, columns=["s", "i"], offset=offset, limit=limit, fmt="json", max_chars=9
        )
        expected = list(range(offset, min(offset + limit, 1000)))
        assert result.rows == [[f"v{i}", i] for i in expected]
        assert result.columns == ["s", "i"]
        assert result.truncated == (offset + limit < 1000)

    def test_dictionary_typed_column_is_cut_too(self, tmp_path) -> None:
        path = tmp_path / "d.parquet"
        pq.write_table(pa.table({"s": pa.array(["abcdef", "xy"] * 3).dictionary_encode()}), path)
        result = preview(path, columns=None, offset=0, limit=3, fmt="json", max_chars=3)
        assert result.rows == [["ab…"], ["xy"], ["ab…"]]

    def test_corrupt_data_page_is_a_typed_error(self, tmp_path) -> None:
        good = kit.write_parquet(tmp_path / "g.parquet", {"id": [1, 2], "x": ["a", "b"]})
        data = bytearray(good.read_bytes())
        offset = pq.ParquetFile(good).metadata.row_group(0).column(1).data_page_offset
        data[offset : offset + 20] = b"\xff" * 20
        bad = kit.write(tmp_path / "bad.parquet", bytes(data))
        with pytest.raises(InvalidInput, match="corrupt") as info:
            preview(bad, columns=None, offset=0, limit=5, fmt="json", max_chars=9)
        assert str(tmp_path) not in info.value.message


class TestQueryInputs:
    def test_a_table_that_is_not_parquet_is_named_and_typed(self, tmp_path, joined) -> None:
        csv_path = kit.write(tmp_path / "bank.csv", b"a,b\n1,2\n")
        with pytest.raises(InvalidInput) as info:
            run({**joined, "bank": csv_path}, "SELECT 1")
        assert info.value.message == "Table 'bank' is not a readable Parquet file."
        assert str(tmp_path) not in info.value.message
        assert info.value.hint is not None
        assert "artifact_id" in info.value.hint


def _duckdb_parquet(path: Path, select: str) -> Path:
    duckdb.connect().execute(f"COPY ({select}) TO '{path}' (FORMAT parquet)")
    return path


@pytest.fixture
def scans(monkeypatch):
    """Which inputs ``query`` gave DuckDB's own reader (True) or the Arrow scan (False)."""
    seen: list[bool] = []
    real = tabular._native_view

    def spy(*args):
        view = real(*args)
        seen.append(view is not None)
        return view

    monkeypatch.setattr(tabular, "_native_view", spy)
    return seen


def _as_arrow_scan(monkeypatch) -> None:
    """Register every input as an Arrow dataset, as query() did before round 3."""
    monkeypatch.setattr(tabular, "_native_view", lambda *args: None)


@pytest.fixture
def typed(tmp_path):
    """Types DuckDB's reader widens (cast back exactly), written by pyarrow and by DuckDB."""
    stamp = dt.datetime(2024, 1, 2, 3, 4, 5, 123000)
    arrow = pa.table(
        {
            "id": [1, 2, 3],
            "ms": pa.array(
                [stamp, None, dt.datetime(1969, 12, 31, 23, 59, 59)], pa.timestamp("ms")
            ),
            "s": pa.array([stamp.replace(microsecond=0), None, None], pa.timestamp("s")),
            "fixed": pa.array([[1, 2], None, [3, None]], pa.list_(pa.int64(), 2)),
            "nested": pa.array(
                [{"t": [stamp]}, None, {"t": None}],
                pa.struct([("t", pa.list_(pa.timestamp("ms")))]),
            ),
            "cat": pa.array(["a", None, "a"]).dictionary_encode(),
            "": [7, 8, 9],
        }
    )
    pq.write_table(arrow, tmp_path / "a.parquet", row_group_size=2)
    duck = _duckdb_parquet(
        tmp_path / "d.parquet",
        """SELECT i AS id, ('{"k": ' || i || '}')::JSON AS j, ['[1]'::JSON] AS jl,
                  MAP {'m': '{}'::JSON} AS jm, '2024-01-02 03:04:05.123'::TIMESTAMP_MS AS ms,
                  ('01:02:0' || i || '+00')::TIMETZ AS tz
           FROM range(1, 4) t(i)""",
    )
    return {"data": tmp_path / "a.parquet", "d": duck}


class TestQueryInputsAreReadByDuckDB:
    """Round 3: every input was an Arrow dataset, whose scan decodes whole row groups as plain
    values outside memory_limit: SELECT count(*) took 3.3 GB on a 260-byte file. DuckDB's own
    reader keeps dictionaries and reads 2048 rows at a time; it is used wherever it gives the
    Arrow scan's names and types, so queries see exactly what they saw before.
    """

    @needs_resource
    @pytest.mark.parametrize(("rows", "width"), [(20_000, 65_536), (128, 16 * 1024 * 1024)])
    def test_a_dictionary_bomb_input_stays_small(self, tmp_path, rows: int, width: int) -> None:
        bomb = _dictionary_bomb(tmp_path / "bomb.parquet", rows, width)
        assert bomb.stat().st_size < 4096
        growth, out = _peak_rss_growth_mb(
            """
            from pathlib import Path
            for sql in ("SELECT count(*) FROM data", "SELECT * FROM data LIMIT 1",
                        "SELECT * FROM data"):
                r = query({"data": Path(sys.argv[1])}, sql, limit=1000, fmt="json",
                          query_timeout=60, max_chars=2000, memory_limit="512MB", threads=1)
                print("ROWS", r.row_count, r.rows[0][0] if r.types == ["int64"] else
                      len(r.rows[0][0]))
            """,
            str(bomb),
        )
        assert f"ROWS 1 {rows}" in out
        assert "ROWS 1 2000" in out
        assert f"ROWS {min(rows, 1000)} 2000" in out
        # Before: 3.3 GB (20,000 x 64 KB) and 4.1 GB (128 x 16 MB), even for SELECT count(*).
        assert growth < 300, out

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM data ORDER BY id",
            "SELECT * FROM d ORDER BY id",
            "DESCRIBE data",
            "DESCRIBE d",
            "SELECT id, ms FROM data WHERE ms > '2000-01-01' ORDER BY id",
            "SELECT d.j, d.jl[1], data.cat FROM data JOIN d USING (id) WHERE d.j->>'k' = '1'",
            "SELECT count(*), count(cat), min(fixed), max(nested) FROM data",
        ],
    )
    def test_results_are_those_of_the_arrow_scan(self, typed, scans, monkeypatch, sql) -> None:
        native = run(typed, sql)
        assert scans == [True, True]
        _as_arrow_scan(monkeypatch)
        assert native == run(typed, sql)

    @pytest.mark.parametrize(
        "column",
        [
            # INT96 nanoseconds: DuckDB's reader cuts them to microseconds.
            pa.array([1704164645000000001, None], pa.timestamp("ns")),
            pa.array([5, None], pa.duration("s")),  # DuckDB reads BIGINT, not INTERVAL
            pa.array([[5], None], pa.list_(pa.duration("us"))),
            pa.StructArray.from_arrays([pa.array([1, 2]), pa.array([3, 4])], names=["k", "k"]),
        ],
        ids=["int96", "duration", "nested-duration", "repeated-field"],
    )
    def test_types_duckdb_reads_differently_keep_the_arrow_scan(
        self, tmp_path, scans, monkeypatch, column: pa.Array
    ) -> None:
        path = tmp_path / "t.parquet"
        pq.write_table(
            pa.table({"id": [1, 2], "c": column}), path, use_deprecated_int96_timestamps=True
        )
        result = run({"data": path}, "SELECT * FROM data ORDER BY id")
        assert scans == [False]
        _as_arrow_scan(monkeypatch)
        assert result == run({"data": path}, "SELECT * FROM data ORDER BY id")
        if pa.types.is_timestamp(column.type):
            assert result.rows[0][1] == "2024-01-02T03:04:05.000000001"

    def test_uuid_and_interval_keep_the_arrow_scan(self, tmp_path, scans, monkeypatch) -> None:
        path = _duckdb_parquet(
            tmp_path / "u.parquet",
            "SELECT 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::UUID AS u, INTERVAL 3 DAY AS i",
        )
        result = run({"data": path}, "SELECT * FROM data")
        assert scans == [False]
        _as_arrow_scan(monkeypatch)
        assert result == run({"data": path}, "SELECT * FROM data")

    def test_repeated_column_names_in_the_file_are_queryable(self, tmp_path, scans) -> None:
        # The Arrow scan refused the file: "Can't unify schema with duplicate field names".
        path = tmp_path / "t.parquet"
        names = ["a", "a", "A", ""]
        pq.write_table(pa.Table.from_arrays([pa.array([i]) for i in range(4)], names=names), path)
        result = run({"data": path}, "SELECT * FROM data")
        assert scans == [True]
        assert result.columns == ["a", "a_1", "A_2", "v3"]
        assert result.rows == [[0, 1, 2, 3]]

    def test_an_input_on_another_filesystem_is_copied(self, joined, scans, monkeypatch) -> None:
        def cross_device(src, dst):
            raise OSError(18, "Invalid cross-device link")

        monkeypatch.setattr(tabular.os, "link", cross_device)
        assert run(joined, JOIN).rows == [
            [1, "alice", 1, "ACME", 100.0],
            [2, "bob", 2, "Globex", 200.0],
        ]
        assert scans == [True, True]

    def test_a_big_input_on_another_filesystem_keeps_the_arrow_scan(
        self, joined, scans, monkeypatch
    ) -> None:
        def cross_device(src, dst):
            raise OSError(18, "Invalid cross-device link")

        monkeypatch.setattr(tabular.os, "link", cross_device)
        monkeypatch.setattr(tabular, "_MAX_INPUT_COPY", 10)
        assert run(joined, JOIN).rows == [
            [1, "alice", 1, "ACME", 100.0],
            [2, "bob", 2, "Globex", 200.0],
        ]
        assert scans == [False, False]

    def test_a_column_chunk_too_big_to_read_whole_keeps_the_arrow_scan(
        self, tmp_path, scans, monkeypatch
    ) -> None:
        # DuckDB's reader allocates a column chunk whole: 95 MB of images under memory_limit=256MB
        # failed with 'Out of Memory Error: failed to allocate data of size 128.0 MiB'.
        path = tmp_path / "t.parquet"
        pq.write_table(
            pa.table({"id": range(40), "img": [bytes([i]) * 4000 for i in range(40)]}),
            path,
            row_group_size=20,
            compression="none",
        )
        monkeypatch.setattr(tabular, "_MAX_NATIVE_CHUNK", 60_000)  # one chunk is 80 KB
        result = run({"data": path}, "SELECT id, octet_length(img) AS n FROM data WHERE id > 37")
        assert result.rows == [[38, 4000], [39, 4000]]
        monkeypatch.setattr(tabular, "_MAX_NATIVE_CHUNK", 100_000)
        assert run({"data": path}, "SELECT count(*) FROM data").rows == [[40]]
        assert scans == [True]  # only the second query tried DuckDB's reader

    def test_sql_reads_only_the_query_s_own_links(self, tmp_path, joined, monkeypatch) -> None:
        made: list[Path] = []
        real = tabular.tempfile.mkdtemp

        def mkdtemp(**kwargs):
            made.append(Path(real(**kwargs)))
            return str(made[-1])

        monkeypatch.setattr(tabular.tempfile, "mkdtemp", mkdtemp)
        with pytest.raises(QueryFailed, match="Permission Error") as info:
            run(joined, f"SELECT * FROM read_parquet('{joined['customers']}')")
        assert str(tmp_path) not in info.value.message
        views = run(joined, "SELECT view_name FROM duckdb_views() WHERE NOT internal ORDER BY 1")
        assert views.rows == [["customers"], ["orders"]]
        assert made
        assert not any(p.exists() for p in made)  # the links went with the query's directory
        assert joined["customers"].exists()


@pytest.mark.parametrize(
    ("have", "want", "exact"),
    [
        ("TIMESTAMP", "TIMESTAMP_MS", True),
        ("TIMESTAMP", "TIMESTAMP_S", True),
        ("TIMESTAMP", "TIMESTAMP_NS", False),  # INT96: the nanoseconds are gone
        ("JSON", "VARCHAR", True),
        ("VARCHAR", "JSON", False),
        ("TIME WITH TIME ZONE", "TIME", True),
        ("BIGINT", "INTERVAL", False),
        ("UUID", "BLOB", False),
        ("INTEGER[]", "INTEGER[2]", True),
        ("INTEGER[3]", "INTEGER[2]", False),
        ("INTEGER[]", "BIGINT[]", False),
        ("TIMESTAMP[]", "TIMESTAMP_MS[]", True),
        ("STRUCT(a JSON, b INTEGER)", "STRUCT(a VARCHAR, b INTEGER)", True),
        ("STRUCT(a JSON)", "STRUCT(b VARCHAR)", False),
        ("STRUCT(a INTEGER, b INTEGER)", "STRUCT(a INTEGER)", False),
        ("MAP(VARCHAR, JSON)", "MAP(VARCHAR, VARCHAR)", True),
        ("MAP(VARCHAR, INTEGER)", "STRUCT(key VARCHAR, value INTEGER)", False),
        ("UNION(a JSON)", "UNION(a VARCHAR)", False),
    ],
)
def test_only_exact_casts_back_to_the_arrow_scan_types(have: str, want: str, exact: bool) -> None:
    con = duckdb.connect()
    kinds = con.sql(f"SELECT NULL::{have} AS have, NULL::{want} AS want").types
    assert tabular._widens(kinds[0], kinds[1]) is exact


_NESTED = {
    "list": "SELECT [repeat('x', 1000000)] AS s FROM range(1000)",
    "struct": "SELECT {'a': repeat('x', 1000000)} AS s FROM range(1000)",
    "map": "SELECT map([1], [repeat('x', 1000000)]) AS s FROM range(1000)",
    "map_keys": "SELECT map([repeat('x', 1000000)], [1]) AS s FROM range(1000)",
    "union": "SELECT union_value(s := repeat('x', 1000000)) AS s FROM range(1000)",
    "deep": "SELECT {'a': {'b': [map(['k'], [repeat('x', 1000000)])]}, 'c': 1} AS s FROM range(1000)",
}


class TestNestedTextIsCutInsideDuckDB:
    """Round 2: only top-level VARCHAR/BLOB was cut, so the same value one level down was not.

    DuckDB built its full vectors and Arrow converted each 256-row batch before the byte cap was
    checked: 1.2 GB (1.4 GB for the union) for 1000 rows of one 1 MB string, then a 413.
    Round 3: map keys were never cut (cut keys can collide, which DuckDB refuses): 1.5 GB.
    """

    @needs_resource
    @pytest.mark.parametrize("shape", sorted(_NESTED))
    def test_a_long_string_inside_a_nested_value(self, tmp_path, shape: str) -> None:
        data = kit.write_parquet(tmp_path / "t.parquet", {"id": [1]})
        growth, out = _peak_rss_growth_mb(
            """
            from pathlib import Path
            r = query({"data": Path(sys.argv[1])}, sys.argv[2], limit=1000, fmt="json",
                      query_timeout=60, max_chars=2000, memory_limit="512MB", threads=1)
            print("ROWS", r.row_count, len(str(r.rows[-1][0])))
            """,
            str(data),
            _NESTED[shape],
        )
        assert "ROWS 1000 " in out, out
        # Before: 1208 MB (list, struct, map), 1454 MB (union), then InputTooLarge; 1520 MB
        # for map keys.
        assert growth < 300, out

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT [repeat('y', 20), NULL] AS l FROM range(2) UNION ALL SELECT NULL "
            "UNION ALL SELECT []",
            "SELECT {'a': repeat('x', 20), 'b': 1, 'it''s \"q\"': 'éééééééééééé'} AS s "
            "UNION ALL SELECT NULL",
            "SELECT map([1, 2], [repeat('m', 30), NULL]) AS m UNION ALL SELECT NULL "
            "UNION ALL SELECT map([], [])",
            "SELECT map([repeat('k', 30)], [1]) AS long_keys",
            # Keys that are equal once cut, a key one past the cut, keys inside a list.
            "SELECT map([repeat('k', 30), repeat('k', 31), repeat('k', 8), repeat('k', 9), "
            "'kkkkkkk…1'], [1, 2, 3, 4, 5]) AS m UNION ALL SELECT NULL",
            "SELECT [map([repeat('é', 20) || i::VARCHAR, 'é'], [i, 0])] AS l FROM range(12) t(i)",
            "SELECT union_value(s := repeat('z', 20))::UNION(s VARCHAR, n INT) AS u "
            "UNION ALL SELECT NULL UNION ALL SELECT union_value(n := 3)",
            "SELECT ['abcdefghijkl', NULL]::VARCHAR[2] AS a UNION ALL SELECT NULL",
            'SELECT \'{"k": "vvvvvvvvvvvvvvvvv"}\'::JSON AS j, [\'{"a": "bbbbbbbbbbbbb"}\'::JSON] AS jl',
            "SELECT {'k': map(['a'], [[repeat('q', 30)]])} AS deep, [[repeat('b', 9)::BLOB]] AS b",
            "SELECT row('abcdefghijklmnop', 1) AS r",
            "SELECT [{'t': repeat('t', 40), 'n': i}] AS l, i FROM range(3) t(i) ORDER BY i DESC",
        ],
    )
    def test_values_render_exactly_as_without_the_cut(self, joined, monkeypatch, sql) -> None:
        cut = run(joined, sql, max_chars=7)
        monkeypatch.setattr(tabular, "_clipped", lambda con, text, max_chars: text)
        assert cut == run(joined, sql, max_chars=7)

    def test_the_cut_is_bounded_and_falls_back_to_the_top_level(self, joined, monkeypatch) -> None:
        sql = "SELECT [repeat('l', 50)] AS l, repeat('t', 50) AS t"
        wrapped = tabular._clipped(tabular.connect(memory_limit="64MB", threads=1), sql, 9)
        assert "list_transform" in wrapped
        assert "left(#2, 10)" in wrapped
        monkeypatch.setattr(tabular, "_MAX_CUT_SQL", 10)
        wrapped = tabular._clipped(tabular.connect(memory_limit="64MB", threads=1), sql, 9)
        assert "list_transform" not in wrapped  # too long: the top level only
        assert "left(#2, 10)" in wrapped
        assert run(joined, sql, max_chars=9).rows == [[["l" * 8 + "…"], "t" * 8 + "…"]]


class TestValuesWithoutAPythonObject:
    """to_pylist() raised on these, outside every handler: a bare 500 for preview and query."""

    def test_nanosecond_times_in_a_query(self, joined) -> None:
        result = run(
            joined,
            "SELECT '2024-01-01 00:00:00.000000001'::TIMESTAMP_NS AS t, "
            "'2024-01-01 00:00:00.5'::TIMESTAMP_NS AS whole, 1 AS n",
        )
        assert result.rows == [["2024-01-01T00:00:00.000000001", "2024-01-01T00:00:00.500000", 1]]
        assert result.types == ["timestamp[ns]", "timestamp[ns]", "int32"]

    def test_nanosecond_columns_in_a_preview(self, tmp_path) -> None:
        path = tmp_path / "ns.parquet"
        table = pa.table(
            {
                "ts": pa.array([1, None], pa.timestamp("ns", "UTC")),
                "t": pa.array([1, 2_000], pa.time64("ns")),
                "d": pa.array([1, 3], pa.duration("ns")),
                "l": pa.array([[1], []], pa.list_(pa.timestamp("ns"))),
            }
        )
        pq.write_table(table, path, version="2.6", coerce_timestamps=None)
        result = preview(path, columns=None, offset=0, limit=5, fmt="json", max_chars=99)
        assert result.rows == [
            [
                "1970-01-01T00:00:00.000000001Z",
                "00:00:00.000000001",
                "1 ns",
                ["1970-01-01T00:00:00.000000001"],
            ],
            [None, "00:00:00.000002000", "3 ns", []],
        ]

    def test_struct_fields_with_repeated_or_empty_names(self, tmp_path, joined) -> None:
        assert run(joined, "SELECT row('abc', 1) AS r").rows == [[{"field_1": "abc", "field_2": 1}]]
        path = tmp_path / "s.parquet"
        kids = [pa.array([1, None]), pa.array(["x", "y"]), pa.array([3, 4])]
        st = pa.StructArray.from_arrays(kids, names=["k", "v", "k"], mask=pa.array([False, True]))
        pq.write_table(pa.table({"s": st, "n": [1, 2]}), path)
        result = preview(path, columns=None, offset=0, limit=5, fmt="csv", max_chars=99)
        assert result.text == 's,n\n"{""k"": 1, ""v"": ""x"", ""k_3"": 3}",1\n,2\n'

    def test_nested_and_encoded_values_render_too(self) -> None:
        ns = pa.array([1, None], pa.timestamp("ns"))
        st = pa.StructArray.from_arrays(
            [
                pa.array(["a", "b"]).dictionary_encode(),
                pa.ExtensionArray.from_storage(pa.uuid(), pa.array([b"0" * 16] * 2, pa.binary(16))),
                ns,
            ],
            names=["d", "u", "t"],
        )
        table = pa.table(
            {
                "m": pa.MapArray.from_arrays([0, 1, 1], pa.array(["k"]), ns.slice(0, 1)),
                "f": pa.FixedSizeListArray.from_arrays(pa.array([1, 2, 3, 4], pa.time64("ns")), 2),
                "s": st,
            }
        )
        result = tabular.render(table, fmt="json", truncated=False, max_chars=99)
        assert result.rows == [
            [
                [["k", "1970-01-01T00:00:00.000000001"]],
                ["00:00:00.000000001", "00:00:00.000000002"],
                {"d": "a", "u": (b"0" * 16).hex(), "t": "1970-01-01T00:00:00.000000001"},
            ],
            [
                [],
                ["00:00:00.000000003", "00:00:00.000000004"],
                {"d": "b", "u": (b"0" * 16).hex(), "t": None},
            ],
        ]

    def test_a_value_that_still_cannot_be_shown_is_a_typed_error(self, monkeypatch) -> None:
        def fails(arr: pa.Array) -> pa.Array:
            raise pa.ArrowInvalid("no")

        monkeypatch.setattr(tabular, "_displayable", fails)
        table = pa.table({"t": pa.array([1], pa.timestamp("ns"))})
        with pytest.raises(InvalidInput, match="Column 't' of type timestamp") as info:
            tabular.render(table, fmt="json", truncated=False, max_chars=99)
        assert info.value.hint is not None
        assert "CAST" in info.value.hint


def test_preview_errors_never_name_the_server_path(tmp_path) -> None:
    # Arrow quotes paths with ' (DuckDB with "): "Failed to open local file '/srv/...'".
    for path in (tmp_path / "missing.parquet", tmp_path):
        with pytest.raises(InvalidInput, match="Not a readable Parquet file") as info:
            preview(path, columns=None, offset=0, limit=5, fmt="json", max_chars=9)
        assert str(tmp_path) not in info.value.message
