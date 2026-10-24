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


class TestCastReport:
    @pytest.mark.parametrize("type_name", ["timestamp", "date"])
    def test_the_utc_conversion_is_reported(self, tmp_path, type_name) -> None:
        csv = "ts,x\n" + "".join(f"{v},{i}\n" for i, v in enumerate(REVIEW))
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            csv,
            infer_types=False,
            expected_schema=[{"name": "ts", "type": type_name}],
        )
        notes = result.column("ts").notes
        assert "values with a UTC offset were converted to UTC (2 of 3)" in notes

    def test_naive_values_carry_no_note(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            "ts,x\n2025-01-15 10:00:00,a\n2025-01-15,b\n",
            infer_types=False,
            expected_schema=[{"name": "ts", "type": "timestamp"}],
        )
        assert result.column("ts").notes == ["cast to timestamp per expected_schema"]

    @pytest.mark.parametrize("infer_types", [False, True])  # text cast, then typed cast
    def test_naive_values_cast_to_timestamptz_say_utc_was_assumed(
        self, tmp_path, infer_types
    ) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            "ts,x\n2025-01-15 10:00:00,a\n2025-01-16 11:00:00,b\n",
            infer_types=infer_types,
            expected_schema=[{"name": "ts", "type": "timestamptz"}],
        )
        assert as_utc(result.rows["ts"]) == [utc(2025, 1, 15, 10), utc(2025, 1, 16, 11)]
        assert f"{READ_AS_UTC} (2 of 2)" in result.column("ts").notes

    def test_offsets_cast_to_timestamptz_need_no_note(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            "ts,x\n2025-01-15 10:00:00+05:00,a\n2025-01-16 11:00:00Z,b\n",
            infer_types=False,
            expected_schema=[{"name": "ts", "type": "timestamptz"}],
        )
        assert as_utc(result.rows["ts"]) == [utc(2025, 1, 15, 5), utc(2025, 1, 16, 11)]
        assert result.column("ts").notes == ["cast to timestamptz per expected_schema"]


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


class TestNamedZonesDoNotLeakIntoNaiveValues:
    """RV1: DuckDB's text-to-TIMESTAMPTZ cast leaks a zone name into later rows.

    Within one vector, a value such as '09:00 EST' sets the cast's calendar zone, and every
    later value without a zone of its own is read in that zone. Zones were detected by
    comparing that cast with the TIMESTAMP cast, so naive values after a zone name were taken
    for zoned ones and shifted by hours (dates by a day), with no reject and no note, or a
    false one, and strict passed.
    """

    EST = "2025-01-01 09:00:00 EST"
    NAIVE = [f"2025-01-{d:02d} 10:00:00" for d in range(2, 29)] * 2  # 54 values

    def _cast(self, values: list[str | None], type_name: str, sql_type: str) -> list[Any]:
        con = _con(values)
        return _values(con, _inferrer(con).cast_text("c", type_name, sql_type))

    def test_inference_keeps_naive_values_after_a_zone_name(self) -> None:
        plan, out = infer([self.EST, *self.NAIVE])
        assert plan.type_name == "timestamp"
        assert out[1:] == [dt.datetime.fromisoformat(v) for v in self.NAIVE]  # were 15:00
        assert out[0] is None
        assert (plan.rejected_count, plan.rejected_examples) == (1, [self.EST])
        assert any(n.startswith(f"{OFFSETS_REJECTED} (1 of 55)") for n in plan.notes)

    def test_strict_naive_schema_fails_instead_of_charging_for_shifted_times(
        self, tmp_path
    ) -> None:
        csv = "ts,x\n" + "".join(f"{v},{i}\n" for i, v in enumerate([self.EST, *self.NAIVE]))
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

    def test_instants_read_naive_values_as_utc_not_as_the_last_zone(self) -> None:
        plan, out = infer(
            [
                "2025-01-15 10:00:00 Asia/Tokyo",
                "2025-01-15 10:00:00+05:00",
                "2025-01-15 10:00:00+01:00",
                "2025-01-15 10:00:00",
            ]
        )
        assert plan.type_name == "timestamptz"
        assert as_utc(out) == [
            utc(2025, 1, 15, 1),
            utc(2025, 1, 15, 5),
            utc(2025, 1, 15, 9),
            utc(2025, 1, 15, 10),  # was 01:00, read in Tokyo time
        ]
        assert f"{READ_AS_UTC} (1 of 4)" in plan.notes

    def test_a_column_of_zones_reads_each_value_in_its_own_zone(self) -> None:
        plan, out = infer(
            [
                "2025-01-15 10:00:00 EST",
                "2025-01-15 10:00:00 UTC",
                "2025-01-15 10:00:00+05:00",
                "2025-01-15 10:00:00 Asia/Tokyo",
                "2025-01-15 10:00:00Z",
            ]
        )
        assert plan.type_name == "timestamptz"
        assert as_utc(out) == [
            utc(2025, 1, 15, 15),
            utc(2025, 1, 15, 10),
            utc(2025, 1, 15, 5),
            utc(2025, 1, 15, 1),
            utc(2025, 1, 15, 10),
        ]

    def test_timestamp_cast_converts_only_the_zoned_value(self) -> None:
        out = self._cast(
            ["2025-01-15 10:00:00 America/New_York", "2025-01-15 10:00:00", "2025-01-16 10:00:00"],
            "timestamp",
            "TIMESTAMP",
        )
        assert out == [  # were all 15:00
            dt.datetime(2025, 1, 15, 15),
            dt.datetime(2025, 1, 15, 10),
            dt.datetime(2025, 1, 16, 10),
        ]

    def test_date_cast_keeps_the_naive_dates(self) -> None:
        out = self._cast(
            ["2025-01-15 10:00:00 Asia/Tokyo", "2025-01-15", "2025-01-16"], "date", "DATE"
        )
        assert out == [dt.date(2025, 1, 15), dt.date(2025, 1, 15), dt.date(2025, 1, 16)]  # off by 1

    def test_timestamptz_cast_reads_naive_values_as_utc(self) -> None:
        out = self._cast(
            ["2025-01-15 10:00:00", self.EST, "2025-01-15 10:00:00", "15/01/2025 10:00"],
            "timestamptz",
            "TIMESTAMPTZ",
        )
        assert as_utc(out) == [
            utc(2025, 1, 15, 10),
            utc(2025, 1, 1, 14),
            utc(2025, 1, 15, 10),  # was 15:00, read as EST
            utc(2025, 1, 15, 10),
        ]

    @pytest.mark.parametrize(
        ("type_name", "note"),
        [
            ("timestamp", "values with a UTC offset were converted to UTC (1 of 3)"),
            ("timestamptz", f"{READ_AS_UTC} (2 of 3)"),
        ],
    )
    def test_the_report_counts_what_was_converted(self, tmp_path, type_name, note) -> None:
        """The notes said '3 of 3' converted, and '1 of 4' read as UTC when 3 of 4 were."""
        rows = ["2025-01-15 10:00:00 America/New_York", "2025-01-15 10:00:00", "2025-01-16"]
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            "ts,x\n" + "".join(f"{v},{i}\n" for i, v in enumerate(rows)),
            infer_types=False,
            expected_schema=[{"name": "ts", "type": type_name}],
        )
        assert note in result.column("ts").notes

    def test_across_vectors_every_value_matches_its_own_reading(self) -> None:
        """DuckDB works in 2048-row vectors: zone names at and around the boundaries."""
        zoned = {
            0: ("2025-01-15 10:00:00 EST", utc(2025, 1, 15, 15)),
            2047: ("2025-01-15 10:00:00 Asia/Tokyo", utc(2025, 1, 15, 1)),
            2048: ("2025-01-15 10:00:00 Europe/Paris", utc(2025, 1, 15, 9)),
            3000: ("2025-01-15 10:00:00-08:00", utc(2025, 1, 15, 18)),
            4500: ("2025-01-15 10:00:00 Etc/GMT+5", utc(2025, 1, 15, 15)),
        }
        values = [
            zoned[i][0] if i in zoned else f"2025-01-15 {i % 24:02d}:00:00" for i in range(5000)
        ]
        con = _con(values)
        inferrer = _inferrer(con)
        cast = as_utc(_values(con, inferrer.cast_text("c", "timestamptz", "TIMESTAMPTZ")))
        naive = _values(con, inferrer.cast_text("c", "timestamp", "TIMESTAMP"))
        for i, (got, wall) in enumerate(zip(cast, naive, strict=True)):
            want = zoned[i][1] if i in zoned else utc(2025, 1, 15, i % 24)
            assert got == want, (i, values[i], got)
            assert wall == want.replace(tzinfo=None), (i, values[i], wall)
