"""Regressions (I3): a UTC offset was silently dropped, so different instants became one.

DuckDB's ``TRY_CAST(text AS TIMESTAMP)`` accepts '2025-01-15 10:00:00+05:00' and returns 10:00,
ignoring the offset. The ladder used it for every column that was not 100% offset-bearing, so
'10:00+05:00', '10:00-08:00' and '10:00' (13 hours apart) were written as the same timestamp
with ``rejected_count == 0`` and no note, and the caller paid for it.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit
from aimart_parquet.engine.duck import connect, ident
from aimart_parquet.engine.infer import ColumnPlan, TypeInferrer
from aimart_parquet.engine.models import DateOrder
from aimart_parquet.errors import SchemaViolation

if TYPE_CHECKING:
    import duckdb

REVIEW = ["2025-01-15 10:00:00+05:00", "2025-01-15 10:00:00-08:00", "2025-01-15 10:00:00"]
READ_AS_UTC = "values without a UTC offset were read as UTC"
OFFSETS_REJECTED = "values with a UTC offset were rejected"


def _con(values: list[str | None]) -> duckdb.DuckDBPyConnection:
    con = connect(memory_limit="256MB", threads=1)
    con.execute(f"CREATE TABLE raw ({ident('c')} VARCHAR)")
    con.executemany("INSERT INTO raw VALUES (?)", [(v,) for v in values])
    return con


def _inferrer(con: duckdb.DuckDBPyConnection) -> TypeInferrer:
    return TypeInferrer(
        con, "raw", extra_null_tokens=[], date_order=DateOrder.AUTO, prefer_decimal_comma=False
    )


def _values(con: duckdb.DuckDBPyConnection, expr: str) -> list[Any]:
    return con.sql(f"SELECT {expr} FROM raw").to_arrow_table().column(0).to_pylist()


def infer(values: list[str | None]) -> tuple[ColumnPlan, list[Any]]:
    con = _con(values)
    plan = _inferrer(con).plan(["c"])["c"]
    return plan, _values(con, plan.expr)


def utc(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=dt.UTC)


def as_utc(values: list[Any]) -> list[Any]:
    return [None if v is None else v.astimezone(dt.UTC) for v in values]


class TestMostlyOffsetsMakeInstants:
    def test_the_review_case_keeps_three_distinct_instants(self) -> None:
        plan, out = infer(REVIEW)
        assert plan.type_name == "timestamptz"
        assert as_utc(out) == [utc(2025, 1, 15, 5), utc(2025, 1, 15, 18), utc(2025, 1, 15, 10)]
        assert plan.rejected_count == 0
        assert f"{READ_AS_UTC} (1 of 3)" in plan.notes

    def test_tolerance_no_longer_drops_every_offset(self) -> None:
        """49 offsets + 1 junk value used to skip TIMESTAMPTZ and parse all 49 naive."""
        values = [f"2025-01-15 {h % 24:02d}:00:00+05:00" for h in range(49)] + ["oops"]
        plan, out = infer(values)
        assert plan.type_name == "timestamptz"
        assert as_utc(out)[:2] == [utc(2025, 1, 14, 19), utc(2025, 1, 14, 20)]
        assert out[-1] is None
        assert (plan.rejected_count, plan.rejected_examples) == (1, ["oops"])
        assert not any(READ_AS_UTC in n for n in plan.notes)  # no naive value was read

    def test_naive_values_in_other_formats_are_read_as_utc(self) -> None:
        plan, out = infer(
            ["2025-01-15T10:00:00+01:00", "2025-01-15T11:00:00+01:00", "25/12/2024 10:00"]
        )
        assert plan.type_name == "timestamptz"
        assert as_utc(out) == [utc(2025, 1, 15, 9), utc(2025, 1, 15, 10), utc(2024, 12, 25, 10)]
        assert f"{READ_AS_UTC} (1 of 3)" in plan.notes

    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            # '+05' escaped the old offset regex (it wanted four digits) and lost five hours.
            (["2025-01-15 10:00:00+05", "2025-01-15 11:00:00+05"], [(1, 15, 5), (1, 15, 6)]),
            (
                ["2025-01-15 10:00:00 America/New_York", "2025-07-15 10:00:00 Europe/Paris"],
                [(1, 15, 15), (7, 15, 8)],
            ),
            (["2025-01-15 10:00:00 UTC", "2025-01-15 11:00:00Z"], [(1, 15, 10), (1, 15, 11)]),
        ],
    )
    def test_every_offset_form_duckdb_accepts_is_applied(self, values, expected) -> None:
        plan, out = infer(values)
        assert plan.type_name == "timestamptz"
        assert as_utc(out) == [utc(2025, m, d, h) for m, d, h in expected]
        assert plan.notes == []


class TestMostlyNaiveRejectsTheOffsets:
    def test_under_tolerance_an_offset_is_a_counted_reject(self) -> None:
        values = [f"2025-01-{d:02d} 10:00:00" for d in range(1, 29)] * 2 + [
            "2025-01-15 10:00:00+05:00"
        ]
        plan, out = infer(values)
        assert plan.type_name == "timestamp"
        assert out[0] == dt.datetime(2025, 1, 1, 10)
        assert out[-1] is None  # was 10:00, five hours off
        assert plan.rejected_count == 1
        assert plan.rejected_examples == ["2025-01-15 10:00:00+05:00"]
        assert any(n.startswith(f"{OFFSETS_REJECTED} (1 of 57)") for n in plan.notes)

    def test_few_values_stay_text_rather_than_lose_an_offset(self) -> None:
        values = ["2025-01-15 10:00:00", "2025-01-16 10:00:00", "2025-01-15 10:00:00+05:00"]
        plan, out = infer(values)
        assert plan.type_name == "string"
        assert out == values

    def test_dates_are_never_mistaken_for_offsets(self) -> None:
        """'2025-01-15' ends in '-15', which an offset regex without a time would match."""
        plan, out = infer(["2025-01-15", "2025-01-16"])
        assert (plan.type_name, out) == ("date", [dt.date(2025, 1, 15), dt.date(2025, 1, 16)])


class TestCastText:
    """expected_schema 'timestamp'/'date' on a text column used the same offset-dropping cast."""

    def _cast(self, values: list[str | None], type_name: str, sql_type: str) -> list[Any]:
        con = _con(values)
        return _values(con, _inferrer(con).cast_text("c", type_name, sql_type))

    def test_timestamp_applies_the_offset(self) -> None:
        assert self._cast(REVIEW, "timestamp", "TIMESTAMP") == [
            dt.datetime(2025, 1, 15, 5),
            dt.datetime(2025, 1, 15, 18),
            dt.datetime(2025, 1, 15, 10),
        ]

    def test_date_is_the_utc_date(self) -> None:
        assert self._cast(["2025-01-15 23:00:00-08:00", "2025-01-15"], "date", "DATE") == [
            dt.date(2025, 1, 16),
            dt.date(2025, 1, 15),
        ]


class TestEndToEnd:
    def test_csv_column_is_timestamptz_with_the_note(self, tmp_path) -> None:
        csv = "ts,x\n" + "".join(f"{v},{i}\n" for i, v in enumerate(REVIEW))
        result = kit.convert_bytes(tmp_path, "a.csv", csv)
        ts = result.column("ts")
        assert "tz=" in ts.type
        assert as_utc(result.rows["ts"]) == [
            utc(2025, 1, 15, 5),
            utc(2025, 1, 15, 18),
            utc(2025, 1, 15, 10),
        ]
        assert any(READ_AS_UTC in n for n in ts.notes)

    def test_strict_naive_schema_fails_rather_than_charge_for_dropped_offsets(
        self, tmp_path
    ) -> None:
        rows = [f"2025-01-{d:02d} 10:00:00" for d in range(1, 29)] * 2 + [
            "2025-01-15 10:00:00+05:00"
        ]
        csv = "ts,x\n" + "".join(f"{v},{i}\n" for i, v in enumerate(rows))
        with pytest.raises(SchemaViolation):
            kit.convert_bytes(
                tmp_path,
                "a.csv",
                csv,
                expected_schema=[
                    {"name": "ts", "type": "timestamp"},
                    {"name": "x", "type": "int64"},
                ],
                strict=True,
            )
