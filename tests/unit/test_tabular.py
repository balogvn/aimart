"""preview and query: compact rendering, and a SQL sandbox that holds."""

from __future__ import annotations

import datetime as dt
import decimal

import pyarrow as pa
import pytest

import aimart_testkit as kit
from aimart_parquet.engine.tabular import _cell, preview, query, render, validate_select
from aimart_parquet.errors import InvalidInput, InvalidRequest, QueryFailed, QueryRejected, Timeout


@pytest.fixture
def parquet(tmp_path):
    return kit.write_parquet(
        tmp_path / "t.parquet",
        {
            "id": list(range(10)),
            "region": ["North", "South"] * 5,
            "units": [i * 10 for i in range(10)],
            "note": ["a|b\nc"] * 10,
            "when": [dt.date(2024, 1, i + 1) for i in range(10)],
        },
        row_group_size=4,
    )


def run(tables, sql, *, limit=100, fmt="json", timeout=5.0, max_chars=2000):
    return query(
        tables,
        sql,
        limit=limit,
        fmt=fmt,
        query_timeout=timeout,
        max_chars=max_chars,
        memory_limit="256MB",
        threads=1,
    )


class TestPreview:
    def test_offset_and_limit(self, parquet) -> None:
        result = preview(parquet, columns=None, offset=3, limit=2, fmt="json", max_chars=100)
        assert result.rows is not None
        assert [r[0] for r in result.rows] == [3, 4]
        assert result.row_count == 2
        assert result.total_rows == 10
        assert result.truncated
        assert result.columns == ["id", "region", "units", "note", "when"]
        assert result.types == ["int64", "string", "int64", "string", "date32[day]"]

    def test_final_page_is_not_truncated(self, parquet) -> None:
        result = preview(parquet, columns=None, offset=8, limit=5, fmt="json", max_chars=100)
        assert result.row_count == 2
        assert not result.truncated

    def test_offset_past_the_end(self, parquet) -> None:
        result = preview(parquet, columns=None, offset=100, limit=5, fmt="json", max_chars=100)
        assert (result.row_count, result.rows, result.truncated) == (0, [], False)

    def test_column_subset_is_case_insensitive(self, parquet) -> None:
        result = preview(
            parquet, columns=["UNITS", "id"], offset=0, limit=1, fmt="json", max_chars=9
        )
        assert result.columns == ["units", "id"]
        assert result.rows == [[0, 0]]

    def test_unknown_column_lists_the_real_ones(self, parquet) -> None:
        with pytest.raises(InvalidRequest) as info:
            preview(parquet, columns=["nope"], offset=0, limit=1, fmt="json", max_chars=9)
        assert info.value.details == {"columns": ["id", "region", "units", "note", "when"]}

    def test_markdown_escapes_pipes_and_newlines(self, parquet) -> None:
        result = preview(
            parquet, columns=["id", "note"], offset=0, limit=2, fmt="markdown", max_chars=99
        )
        assert result.rows is None
        assert result.text == "| id | note |\n|---|---|\n| 0 | a\\|b c |\n| 1 | a\\|b c |"

    def test_csv(self, parquet) -> None:
        result = preview(
            parquet, columns=["id", "note"], offset=0, limit=1, fmt="csv", max_chars=99
        )
        assert result.text == 'id,note\n0,"a|b\nc"\n'

    def test_long_cells_are_truncated(self, parquet) -> None:
        result = preview(parquet, columns=["region"], offset=0, limit=1, fmt="json", max_chars=3)
        assert result.rows == [["No…"]]

    def test_not_parquet(self, tmp_path) -> None:
        path = kit.write(tmp_path / "x", b"nope")
        with pytest.raises(InvalidInput):
            preview(path, columns=None, offset=0, limit=1, fmt="json", max_chars=9)


class TestRender:
    def test_cell_conversions(self) -> None:
        assert _cell(None, 5) is None
        assert _cell(dt.time(1, 2), 9) == "01:02:00"
        assert _cell(dt.timedelta(seconds=5), 9) == "0:00:05"
        # Decimals become JSON numbers: integral ones exact ints (sum() over BIGINT is HUGEINT),
        # fractional ones floats; integers beyond 2^53 stay strings so JS clients keep every digit.
        assert _cell(decimal.Decimal("1.50"), 9) == 1.5
        assert _cell(decimal.Decimal("200"), 9) == 200
        assert _cell(decimal.Decimal(2**60), 30) == str(2**60)
        assert _cell(b"\x00\xff\x10", 4) == "00ff"
        assert _cell([1, "abcdef"], 3) == [1, "ab…"]
        assert _cell({"k": "abcdef"}, 3) == {"k": "ab…"}

    def test_nested_values_render_as_json_in_text_formats(self) -> None:
        table = pa.table({"l": [[1, 2]], "s": [{"a": "é"}], "n": [None]})
        md = render(table, fmt="markdown", truncated=False, max_chars=99)
        assert md.text == '| l | s | n |\n|---|---|---|\n| [1, 2] | {"a": "é"} |  |'
        csv_out = render(table, fmt="csv", truncated=False, max_chars=99)
        assert csv_out.text == 'l,s,n\n"[1, 2]","{""a"": ""é""}",\n'


class TestValidateSelect:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "  select * from data;  ",
            "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
            "DESCRIBE data",
            "SUMMARIZE data",
            "FROM data SELECT id",
        ],
    )
    def test_read_only_selects_pass(self, sql: str) -> None:
        assert validate_select(sql) == sql.strip().rstrip(";").strip()

    @pytest.mark.parametrize(
        ("sql", "message"),
        [
            ("", "empty"),
            ("  ;  ", "empty"),
            ("SELEC 1", "syntax error"),
            ("SELECT 1; SELECT 2", "exactly one statement; got 2"),
            ("SELECT 1; DROP TABLE data", "exactly one statement"),
            ("DROP TABLE data", "got DROP"),
            ("DELETE FROM data", "got DELETE"),
            ("INSERT INTO data VALUES (1)", "got INSERT"),
            ("UPDATE data SET id = 1", "got UPDATE"),
            ("CREATE TABLE x AS SELECT 1", "got CREATE"),
            ("COPY data TO '/tmp/x.csv'", "got COPY"),
            ("ATTACH '/tmp/x.db' AS x", "got ATTACH"),
            ("SET enable_external_access = true", "got SET"),
            ("INSTALL httpfs", "Only read-only SELECT"),
            ("LOAD httpfs", "Only read-only SELECT"),
            ("EXPLAIN SELECT 1", "got EXPLAIN"),
            ("EXPORT DATABASE '/tmp/x'", "Only read-only SELECT"),
        ],
    )
    def test_everything_else_is_rejected(self, sql: str, message: str) -> None:
        with pytest.raises(QueryRejected, match=message) as info:
            validate_select(sql)
        assert info.value.http_status == 400


class TestQuery:
    def test_aggregate_over_data(self, parquet) -> None:
        result = run(
            {"data": parquet},
            "SELECT region, sum(units) AS units FROM data GROUP BY 1 ORDER BY 1",
        )
        assert result.columns == ["region", "units"]
        assert result.rows is not None
        assert [[r[0], int(r[1])] for r in result.rows] == [["North", 200], ["South", 250]]
        assert not result.truncated
        assert result.total_rows is None

    def test_integer_aggregates_are_json_numbers(self, parquet) -> None:
        result = run({"data": parquet}, "SELECT sum(units) AS units FROM data")
        assert result.rows == [[450]]

    def test_row_cap_sets_truncated(self, parquet) -> None:
        result = run({"data": parquet}, "SELECT id FROM data ORDER BY id", limit=3)
        assert result.rows == [[0], [1], [2]]
        assert result.truncated
        assert result.row_count == 3

    def test_exactly_limit_rows_is_not_truncated(self, parquet) -> None:
        result = run({"data": parquet}, "SELECT id FROM data", limit=10)
        assert result.row_count == 10
        assert not result.truncated

    def test_multiple_tables(self, parquet, tmp_path) -> None:
        other = kit.write_parquet(tmp_path / "o.parquet", {"id": [1, 2], "x": ["a", "b"]})
        result = run(
            {"data": parquet, "other": other},
            "SELECT d.id, o.x FROM data d JOIN other o USING (id) ORDER BY 1",
        )
        assert result.rows == [[1, "a"], [2, "b"]]

    def test_formats(self, parquet) -> None:
        md = run({"data": parquet}, "SELECT id FROM data LIMIT 1", fmt="markdown")
        assert md.text == "| id |\n|---|\n| 0 |"
        csv_out = run({"data": parquet}, "SELECT id FROM data LIMIT 1", fmt="csv")
        assert csv_out.text == "id\n0\n"

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM read_csv('/etc/passwd')",
            "SELECT * FROM read_text('/etc/hosts')",
            "SELECT * FROM glob('/*')",
            "SELECT * FROM read_parquet('{path}')",
        ],
    )
    def test_file_functions_are_blocked_even_for_our_own_file(self, parquet, sql) -> None:
        with pytest.raises(QueryFailed) as info:
            run({"data": parquet}, sql.format(path=parquet))
        assert str(parquet) not in info.value.message
        assert "/etc/" not in info.value.message
        assert info.value.hint is not None
        assert '"data"' in info.value.hint  # tells the agent what it *can* query

    def test_settings_cannot_be_changed_from_sql(self, parquet) -> None:
        with pytest.raises(QueryRejected):
            run({"data": parquet}, "SET enable_external_access = true")
        with pytest.raises(QueryFailed):
            run({"data": parquet}, "SELECT * FROM duckdb_settings() WHERE set_config('a', 'b')")

    def test_error_messages_scrub_server_paths(self, parquet) -> None:
        with pytest.raises(QueryFailed) as info:
            run({"data": parquet}, f"SELECT * FROM read_parquet('{parquet.parent}/x.parquet')")
        assert str(parquet.parent) not in info.value.message
        assert '"<file>"' in info.value.message

    def test_unknown_table(self, parquet) -> None:
        with pytest.raises(QueryFailed, match="nope"):
            run({"data": parquet}, "SELECT * FROM nope")

    def test_runaway_query_is_interrupted(self, parquet) -> None:
        with pytest.raises(Timeout) as info:
            run(
                {"data": parquet},
                "SELECT count(*) FROM range(100000000) a, range(100000000) b",
                timeout=0.2,
            )
        assert info.value.http_status == 504
        assert "0.2 seconds" in info.value.message

    def test_settings_do_not_leak_server_paths(self, parquet) -> None:
        from pathlib import Path

        result = run({"data": parquet}, "SELECT current_setting('allowed_directories')::VARCHAR")
        assert str(Path.cwd()) not in str(result.rows)
