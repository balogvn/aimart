"""Regressions from the final data review: zone abbreviations read as other zones, charged.

DuckDB's TIMESTAMPTZ cast reads a zone name with ICU, which resolves three-letter abbreviations
through legacy Java IDs, not their everyday meaning: BST is Asia/Dhaka (+6, not British Summer
Time +1), AST Alaska (not Atlantic -4), ART Africa/Cairo (not Argentina -3), NST Pacific/
Auckland (not Newfoundland -3:30), ECT Europe/Paris (not Ecuador -5), SST Pacific/Guadalcanal
(not Samoa -11). It also applies the rules of the zone it picked: PST is Los Angeles, so 'PST'
in July read as -7. A UK log ('09:30 GMT' in winter, '09:30 BST' in summer) came back as
timestamptz with every summer instant five hours early, a 200 with no note; 'UTC+05:30' read
as +05:00. Before the zone rung, such columns stayed text.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit
from aimart_parquet.engine.duck import connect, ident
from aimart_parquet.engine.infer import ZONE_ABBREVIATIONS, ColumnPlan, TypeInferrer
from aimart_parquet.engine.models import DateOrder
from aimart_parquet.errors import SchemaViolation

if TYPE_CHECKING:
    import duckdb

UTC = dt.UTC
UNREAD = "not read as a zone"


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


def cast(values: list[str | None], type_name: str, sql_type: str) -> list[Any]:
    con = _con(values)
    return _values(con, _inferrer(con).cast_text("c", type_name, sql_type))


def as_utc(values: list[Any]) -> list[Any]:
    return [None if v is None else v.astimezone(UTC) for v in values]


def utc(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=UTC)


UK_YEAR = [f"2025-{m:02d}-15 09:30:00 {'BST' if 4 <= m <= 10 else 'GMT'}" for m in range(1, 13)]


class TestTheReviewFiles:
    def test_a_uk_log_stays_text_and_says_why(self, tmp_path) -> None:
        csv = "id,logged_at\n" + "".join(f"{i},{v}\n" for i, v in enumerate(UK_YEAR, 1))
        result = kit.convert_bytes(tmp_path, "uk_year.csv", csv)
        column = result.column("logged_at")
        assert column.type == "string"  # was timestamp[tz=UTC], 7 of 12 five hours early
        assert result.rows["logged_at"] == UK_YEAR
        assert any(UNREAD in n and "'BST'" in n for n in column.notes), column.notes

    @pytest.mark.parametrize("abbr", ["BST", "AST", "ART", "NST", "ECT", "SST", "IST", "CST"])
    def test_an_abbreviation_with_another_meaning_in_icu_is_not_a_zone(
        self, tmp_path, abbr
    ) -> None:
        values = [f"2025-01-{d:02d} 09:00:00 {abbr}" for d in range(6, 9)]
        result = kit.convert_bytes(
            tmp_path,
            f"z_{abbr}.csv",
            "id,at\n" + "".join(f"{i},{v}\n" for i, v in enumerate(values)),
        )
        assert result.column("at").type == "string"
        assert result.rows["at"] == values
        assert any(f"'{abbr}'" in n for n in result.column("at").notes)

    def test_json_strings_are_read_the_same_way(self, tmp_path) -> None:
        rows = [
            {"id": 1, "at": "2025-07-07 12:00:00 BST"},
            {"id": 2, "at": "2025-07-07 13:00:00 BST"},
        ]
        result = kit.convert_bytes(tmp_path, "zone_names.json", json.dumps(rows))
        assert result.rows["at"] == [r["at"] for r in rows]  # 12:00 BST was 06:00 UTC

    def test_summer_pst_is_not_read_with_daylight_time(self, tmp_path) -> None:
        values = ["2025-01-15 09:00:00 PST", "2025-07-15 09:00:00 PST"]
        result = kit.convert_bytes(tmp_path, "pst.csv", "at\n" + "".join(f"{v}\n" for v in values))
        assert result.rows["at"] == values  # July was read as -07:00


class TestUnreadZonesAreRejectsNotShiftedInstants:
    def test_one_bst_among_gmt_values_is_a_counted_reject(self) -> None:
        values = [f"2025-01-{d:02d} 09:30:00 GMT" for d in range(1, 29)] * 2 + [
            "2025-07-15 09:30:00 BST"
        ]
        plan, out = infer(values)
        assert plan.type_name == "timestamptz"
        assert as_utc(out[:2]) == [utc(2025, 1, 1, 9, 30), utc(2025, 1, 2, 9, 30)]
        assert out[-1] is None  # was 03:30 UTC
        assert (plan.rejected_count, plan.rejected_examples) == (1, ["2025-07-15 09:30:00 BST"])
        assert any(UNREAD in n and "'BST'" in n for n in plan.notes), plan.notes

    @pytest.mark.parametrize("type_name", ["timestamptz", "timestamp", "date"])
    def test_expected_schema_casts_reject_them(self, type_name) -> None:
        sql = {"timestamptz": "TIMESTAMPTZ", "timestamp": "TIMESTAMP", "date": "DATE"}[type_name]
        out = cast(["2025-07-15 09:30:00 BST", "2025-07-15 09:30:00+01:00"], type_name, sql)
        assert out[0] is None
        assert out[1] is not None

    def test_strict_fails_rather_than_charge_for_a_shifted_time(self, tmp_path) -> None:
        with pytest.raises(SchemaViolation):
            kit.convert_bytes(
                tmp_path,
                "uk.csv",
                "at\n2025-01-15 09:30:00 GMT\n2025-07-15 09:30:00 BST\n",
                expected_schema=[{"name": "at", "type": "timestamptz"}],
                strict=True,
            )

    @pytest.mark.parametrize("offset", ["UTC+05:30", "UTC-03:30", "UTC+0530", "UTC+5:30"])
    def test_utc_with_minutes_is_not_cut_to_whole_hours(self, offset) -> None:
        # ICU read 'UTC+05:30' as +05:00: India half an hour off.
        (value,) = cast([f"2025-01-15 12:00:00 {offset}"], "timestamptz", "TIMESTAMPTZ")
        assert value is None


class TestReadZones:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("2025-07-15 12:00:00 EST", utc(2025, 7, 15, 17)),
            ("2025-07-15 12:00:00 est", utc(2025, 7, 15, 17)),
            ("2025-07-15 12:00:00 MST", utc(2025, 7, 15, 19)),
            ("1967-07-15 12:00:00 MST", utc(1967, 7, 15, 19)),  # ICU: -06:00 (Phoenix, 1967)
            ("2025-07-15 12:00:00 HST", utc(2025, 7, 15, 22)),
            ("2025-07-15 12:00:00 JST", utc(2025, 7, 15, 3)),
            ("1950-07-15 12:00:00 JST", utc(1950, 7, 15, 3)),  # ICU: +10:00 (Japan, 1950)
            ("2025-07-15 12:00:00 GMT", utc(2025, 7, 15, 12)),
            ("2025-07-15T12:00:00.250 EST", utc(2025, 7, 15, 17, 0, 0, 250000)),
            ("2025-07-15 12:00:00 UTC", utc(2025, 7, 15, 12)),
            ("2025-07-15 12:00:00 UTC+5", utc(2025, 7, 15, 7)),
            ("2025-07-15 12:00:00 UTC-03:00", utc(2025, 7, 15, 15)),
            ("2025-07-15 12:00:00 Europe/London", utc(2025, 7, 15, 11)),
            ("2025-07-15 12:00:00 america/new_york", utc(2025, 7, 15, 16)),
            ("2025-07-15 12:00:00 America/Argentina/Buenos_Aires", utc(2025, 7, 15, 15)),
            ("2025-07-15 12:00:00 America/St_Johns", utc(2025, 7, 15, 14, 30)),
            ("2025-07-15 12:00:00 Etc/GMT+5", utc(2025, 7, 15, 17)),
            # The IANA names of the US zones in POSIX form, with the summer time they name.
            ("2025-07-15 12:00:00 EST5EDT", utc(2025, 7, 15, 16)),
            ("2025-01-15 12:00:00 cst6cdt", utc(2025, 1, 15, 18)),
            ("2025-07-15 12:00:00 MST7MDT", utc(2025, 7, 15, 18)),
            ("2025-01-15 12:00:00 PST8PDT", utc(2025, 1, 15, 20)),
            ("2025-07-15 12:00:00+05:30", utc(2025, 7, 15, 6, 30)),
            ("2025-07-15 12:00:00Z", utc(2025, 7, 15, 12)),
        ],
    )
    def test_unambiguous_zones(self, text, expected) -> None:
        plan, out = infer([text])
        assert plan.type_name == "timestamptz"
        assert as_utc(out) == [expected]
        assert as_utc(cast([text], "timestamptz", "TIMESTAMPTZ")) == [expected]

    @pytest.mark.parametrize("name", sorted(ZONE_ABBREVIATIONS))
    def test_each_listed_abbreviation_means_what_icu_reads_today(self, name) -> None:
        """The list against DuckDB's own ICU, winter and summer: ICU agrees on current dates."""
        offset = ZONE_ABBREVIATIONS[name]
        con = connect(memory_limit="256MB", threads=1)
        for day in ("2025-01-15", "2025-07-15"):
            row = con.execute(
                "SELECT TRY_CAST(? AS TIMESTAMPTZ) = TRY_CAST(? AS TIMESTAMPTZ)",
                [f"{day} 12:00:00 {name}", f"{day} 12:00:00{offset}"],
            ).fetchone()
            assert row == (True,), (name, day)

    def test_a_column_of_mixed_zones(self) -> None:
        plan, out = infer(
            [
                "2025-07-15 10:00:00 EST",
                "2025-07-15 10:00:00 Europe/Paris",
                "2025-07-15 10:00:00+05:00",
                "2025-07-15 10:00:00 JST",
            ]
        )
        assert plan.type_name == "timestamptz"
        assert as_utc(out) == [
            utc(2025, 7, 15, 15),
            utc(2025, 7, 15, 8),
            utc(2025, 7, 15, 5),
            utc(2025, 7, 15, 1),
        ]
