"""Regressions from the final data review: the null-token test planned as a hash join.

DuckDB 1.5.6 plans ``x IN (<5 or more constants>)`` as a MARK hash join against the list. The
type ladder tests every value against 16 null tokens, several times per column, so:

* a 100-row CSV with 30 date, 60 integer or 80 decimal columns (10-50 KB) ran out of
  DuckDB's 1 GB in the final COPY, a 500 'Internal error, retry' that a retry cannot fix;
* the join does not keep insertion order with two threads: a CSV or NDJSON file with more
  than one 122,880-row group was written out of source order, so preview offset 0 and
  ``SELECT * FROM data LIMIT 3`` returned rows 122,881.. to a caller who paid for the first.
"""

from __future__ import annotations

import json
import random
from typing import TYPE_CHECKING

import pyarrow.parquet as pq
import pytest

import aimart_testkit as kit
from aimart_parquet.engine.duck import connect, ident
from aimart_parquet.engine.infer import TypeInferrer
from aimart_parquet.engine.models import DateOrder
from aimart_parquet.engine.tabular import preview

if TYPE_CHECKING:
    from pathlib import Path

    import duckdb


@pytest.mark.parametrize(("kind", "cols"), [("date", 30), ("decimal", 80), ("integer", 60)])
def test_a_small_wide_csv_converts(tmp_path: Path, kind: str, cols: int) -> None:
    rnd = random.Random(3)
    gens = {
        "date": lambda: f"2025-{rnd.randrange(1, 13):02d}-{rnd.randrange(1, 28):02d}",
        "decimal": lambda: f"{rnd.uniform(0, 100):.2f}",
        "integer": lambda: str(rnd.randrange(1, 100000)),
    }
    lines = [",".join(f"c{i}" for i in range(cols))]
    lines += [",".join(gens[kind]() for _ in range(cols)) for _ in range(100)]
    result = kit.convert_bytes(tmp_path, f"{kind}_{cols}.csv", "\n".join(lines) + "\n")
    assert result.report.rows == 100  # was OutOfMemoryException in the final COPY: a 500
    expected = {"date": "date32[day]", "decimal": "double", "integer": "int64"}[kind]
    assert [c.type for c in result.report.columns] == [expected] * cols


def _con(columns: dict[str, list[str | None]]) -> duckdb.DuckDBPyConnection:
    con = connect(memory_limit="256MB", threads=2)
    names = list(columns)
    con.execute(f"CREATE TABLE raw ({', '.join(f'{ident(n)} VARCHAR' for n in names)})")
    rows = list(zip(*columns.values(), strict=True))
    con.executemany(f"INSERT INTO raw VALUES ({', '.join('?' for _ in names)})", rows)
    return con


def test_no_generated_expression_plans_a_join() -> None:
    """Every rung, the text plan and every expected_schema cast: EXPLAIN shows no join.

    A join in a column expression is what made wide files run out of memory and wrote rows
    out of order; this fails for any new ``IN (...)`` over constants, whatever its length.
    """
    columns: dict[str, list[str | None]] = {
        "b": ["yes", "no", "NA"],
        "i": ["1", "2", "-"],
        "f": ["1.5", "2.25", "n/a"],
        "eu": ["1.234,5", "12,5", ""],
        "d": ["2025-01-15", "16/01/2025", "null"],
        "ts": ["2025-01-15 10:00:00", "2025-01-15 10:00:00+05:00", "2025-01-15 10:00:00 EST"],
        "t": ["north", "NA", "x"],
    }
    con = _con(columns)
    inferrer = TypeInferrer(
        con,
        "raw",
        extra_null_tokens=["x", "y"],
        date_order=DateOrder.AUTO,
        prefer_decimal_comma=False,
    )
    plans = inferrer.plan(list(columns))
    exprs = [p.expr for p in plans.values()] + [inferrer.text_plan(s).expr for s in columns]
    targets = [
        ("int64", "BIGINT"),
        ("float64", "DOUBLE"),
        ("decimal", "DECIMAL(10, 2)"),
        ("bool", "BOOLEAN"),
        ("date", "DATE"),
        ("timestamp", "TIMESTAMP"),
        ("timestamptz", "TIMESTAMPTZ"),
    ]
    exprs += [inferrer.cast_text(s, t, sql) for s in columns for t, sql in targets]
    assert {p.type_name for p in plans.values()} >= {"bool", "int64", "float64", "date"}
    for expr in exprs:
        plan = "\n".join(str(r[1]) for r in con.sql(f"EXPLAIN SELECT {expr} FROM raw").fetchall())
        assert "JOIN" not in plan, expr


N_ROWS = 226_303  # two row groups of 122,880 and then some


def _ids(path: Path) -> list[int]:
    return pq.read_table(path, columns=["id"]).column("id").to_pylist()


@pytest.mark.parametrize("fmt", ["csv", "ndjson"])
def test_rows_keep_source_order_past_one_row_group(tmp_path: Path, fmt: str) -> None:
    if fmt == "csv":
        data = "id,name\n" + "".join(f"{i},customer {i % 977}\n" for i in range(1, N_ROWS + 1))
    else:
        data = "".join(
            json.dumps({"id": i, "name": f"customer {i % 977}", "n": "NA" if i % 7 else str(i)})
            + "\n"
            for i in range(1, N_ROWS + 1)
        )
    result = kit.convert_bytes(tmp_path, f"rows.{fmt}", data)
    assert result.report.rows == N_ROWS
    assert _ids(result.path) == list(range(1, N_ROWS + 1))  # rows 122,881.. came first
    first = preview(result.path, columns=None, offset=0, limit=1, fmt="json", max_chars=100)
    assert first.rows is not None
    assert first.rows[0][0] == 1
