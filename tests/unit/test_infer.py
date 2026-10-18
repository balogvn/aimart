"""The type ladder, rung by rung, including the rules that keep data from being corrupted."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

import pytest

from aimart_parquet.engine.duck import connect, ident
from aimart_parquet.engine.infer import (
    DEFAULT_NULL_TOKENS,
    MIN_ROWS_FOR_TOLERANCE,
    ColumnPlan,
    TypeInferrer,
)
from aimart_parquet.engine.models import DateOrder

if TYPE_CHECKING:
    import duckdb


def _table(columns: dict[str, list[str | None]]) -> duckdb.DuckDBPyConnection:
    # The production connection: UTC, no file access, configuration locked.
    con = connect(memory_limit="256MB", threads=1)
    defs = ", ".join(f"{ident(c)} VARCHAR" for c in columns)
    con.execute(f"CREATE TABLE raw ({defs})")
    rows = list(zip(*columns.values(), strict=True))
    if rows:
        marks = ", ".join("?" for _ in columns)
        con.executemany(f"INSERT INTO raw VALUES ({marks})", rows)
    return con


def infer(
    values: list[str | None],
    *,
    nulls: list[str] | None = None,
    order: DateOrder = DateOrder.AUTO,
    decimal_comma: bool = False,
) -> tuple[ColumnPlan, list[Any]]:
    con = _table({"c": values})
    inferrer = TypeInferrer(
        con,
        "raw",
        extra_null_tokens=nulls or [],
        date_order=order,
        prefer_decimal_comma=decimal_comma,
    )
    plan = inferrer.plan(["c"])["c"]
    return plan, _values(con, plan.expr)


def _values(con: duckdb.DuckDBPyConnection, expr: str) -> list[Any]:
    return con.sql(f"SELECT {expr} AS v FROM raw").to_arrow_table().column(0).to_pylist()


class TestBool:
    def test_true_false_tokens(self) -> None:
        plan, out = infer(["true", "FALSE", "Yes", "n", "T", "f", None, ""])
        assert plan.type_name == "bool"
        assert out == [True, False, True, False, True, False, None, None]

    def test_zero_one_is_an_integer_not_a_bool(self) -> None:
        plan, out = infer(["0", "1", "1"])
        assert plan.type_name == "int64"
        assert out == [0, 1, 1]


class TestIntegers:
    def test_plain_integers(self) -> None:
        plan, out = infer(["1", "-2", "+3", " 4 "])
        assert (plan.type_name, out) == ("int64", [1, -2, 3, 4])
        assert plan.inferred

    def test_a_decimal_makes_the_column_float_never_a_rounded_int(self) -> None:
        """DuckDB casts '12.7' to BIGINT 13; the ladder must not."""
        plan, out = infer(["1", "2", "12.7"])
        assert plan.type_name == "float64"
        assert out == [1.0, 2.0, 12.7]

    def test_leading_zero_ids_stay_text(self) -> None:
        plan, out = infer(["00123", "00456", "789"])
        assert plan.type_name == "string"
        assert out == ["00123", "00456", "789"]
        assert any("leading zeros" in n for n in plan.notes)
        assert not plan.inferred

    def test_a_lone_zero_is_not_a_leading_zero(self) -> None:
        plan, _ = infer(["0", "10", "0.5"])
        assert plan.type_name == "float64"

    @pytest.mark.parametrize("digits", [16, 19, 25])
    def test_long_digit_strings_stay_text(self, digits: int) -> None:
        value = "9" * digits
        plan, out = infer([value, "1"])
        assert plan.type_name == "string"
        assert out == [value, "1"]

    def test_fifteen_digits_is_still_a_number(self) -> None:
        plan, out = infer(["123456789012345", "2"])
        assert plan.type_name == "int64"
        assert out == [123456789012345, 2]


class TestFloats:
    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            (["1.5", ".25", "-3", "1e3", "2.5E-1"], [1.5, 0.25, -3.0, 1000.0, 0.25]),
            (["1,234.5", "12.5", "1,000,000"], [1234.5, 12.5, 1_000_000.0]),
        ],
    )
    def test_plain_and_us(self, values, expected) -> None:
        plan, out = infer(values)
        assert plan.type_name == "float64"
        assert out == pytest.approx(expected)

    def test_us_thousands_without_decimals_are_integers(self) -> None:
        plan, out = infer(["1,234,567", "12,000", "5"])
        assert plan.type_name == "int64"
        assert out == [1_234_567, 12_000, 5]
        assert "parsed us-style number formatting" in plan.notes

    def test_eu_decimals(self) -> None:
        plan, out = infer(["1.234,56", "12,5", "7"], decimal_comma=True)
        assert plan.type_name == "float64"
        assert out == pytest.approx([1234.56, 12.5, 7.0])
        assert "parsed eu-style number formatting" in plan.notes

    def test_eu_formatting_is_detected_without_the_semicolon_hint(self) -> None:
        plan, out = infer(["1.234,56", "2.000,1"])
        assert plan.type_name == "float64"
        assert out == pytest.approx([1234.56, 2000.1])

    def test_ambiguous_thousands_follow_the_dialect_hint(self) -> None:
        # "1,234" is US one-thousand-two-hundred or EU one-point-two-three-four.
        us_plan, us_out = infer(["1,234", "5,678"], decimal_comma=False)
        eu_plan, eu_out = infer(["1,234", "5,678"], decimal_comma=True)
        assert (us_plan.type_name, us_out) == ("int64", [1234, 5678])
        assert eu_plan.type_name == "float64"
        assert eu_out == pytest.approx([1.234, 5.678])

    def test_space_grouped_numbers(self) -> None:
        plan, out = infer(["1 234,5", "2 000", "12"])
        assert plan.type_name == "float64"
        assert out == pytest.approx([1234.5, 2000.0, 12.0])
        assert "parsed space-style number formatting" in plan.notes

    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            (["$1,234.50", "$12", "€3"], [1234.5, 12.0, 3.0]),
            (["-£5", "£ 6.5", "7 ¥"], [-5.0, 6.5, 7.0]),
            (["₦100", "₦2,000.5"], [100.0, 2000.5]),
        ],
    )
    def test_currency_symbols_are_stripped(self, values, expected) -> None:
        plan, out = infer(values)
        assert plan.type_name in {"float64", "int64"}
        assert out == pytest.approx(expected)
        assert "stripped currency symbols" in plan.notes


class TestTolerance:
    def test_under_twenty_values_one_misfit_keeps_text(self) -> None:
        plan, out = infer([str(i) for i in range(10)] + ["oops"])
        assert plan.type_name == "string"
        assert out[-1] == "oops"

    def test_ninety_eight_percent_fit_wins_and_counts_rejects(self) -> None:
        values = [str(i) for i in range(99)] + ["oops"]
        plan, out = infer(values)
        assert plan.type_name == "int64"
        assert plan.rejected_count == 1
        assert plan.rejected_examples == ["oops"]
        assert out[-1] is None
        assert out[:3] == [0, 1, 2]

    def test_rejected_examples_are_capped_at_three_distinct_values(self) -> None:
        values = [str(i) for i in range(400)] + ["a", "b", "c", "d", "a"]
        plan, _ = infer(values)
        assert plan.type_name == "int64"
        assert plan.rejected_count == 5
        assert plan.rejected_examples == ["a", "b", "c"]

    def test_below_ninety_eight_percent_stays_text(self) -> None:
        values = [str(i) for i in range(MIN_ROWS_FOR_TOLERANCE)] + ["x", "y"]
        plan, _ = infer(values)
        assert plan.type_name == "string"

    def test_tolerated_decimal_misfits_in_an_int_column_make_it_float(self) -> None:
        """One '12.7' among integers must keep its value: the column becomes float, no reject."""
        values = [str(i) for i in range(99)] + ["12.7"]
        plan, out = infer(values)
        assert plan.type_name == "float64"
        assert plan.rejected_count == 0
        assert out[-1] == 12.7


class TestNulls:
    @pytest.mark.parametrize("token", ["", "NA", "n/a", "N.A.", "NaN", "null", "None", "-", "#N/A"])
    def test_default_null_tokens_in_typed_columns(self, token: str) -> None:
        plan, out = infer(["1", token, "3"])
        assert plan.type_name == "int64"
        assert out == [1, None, 3]

    def test_na_in_a_text_column_is_kept(self) -> None:
        """NA may be Namibia: text columns only null '' and caller-listed tokens."""
        plan, out = infer(["NA", "FR", "DE", ""])
        assert plan.type_name == "string"
        assert out == ["NA", "FR", "DE", None]

    def test_caller_null_tokens_apply_to_text_too(self) -> None:
        plan, out = infer(["NA", "FR", "unknown"], nulls=["NA", " Unknown "])
        assert plan.type_name == "string"
        assert out == [None, "FR", None]

    def test_caller_null_tokens_apply_to_typed_columns(self) -> None:
        plan, out = infer(["1", "missing!", "3"], nulls=["missing!"])
        assert plan.type_name == "int64"
        assert out == [1, None, 3]

    def test_all_empty(self) -> None:
        plan, out = infer(["", None, "  "])
        assert plan.type_name == "string"
        assert "all values empty" in plan.notes
        assert out == [None, None, None]

    def test_default_tokens_are_lowercase(self) -> None:
        assert all(t == t.lower() for t in DEFAULT_NULL_TOKENS)


class TestTemporal:
    def test_iso_dates(self) -> None:
        plan, out = infer(["2024-01-15", "2024/02/20", "2024.03.05"])
        assert plan.type_name == "date"
        assert out == [dt.date(2024, 1, 15), dt.date(2024, 2, 20), dt.date(2024, 3, 5)]
        assert plan.notes == []

    def test_unambiguous_day_first(self) -> None:
        plan, out = infer(["25/12/2024", "13/01/2025"])
        assert plan.type_name == "date"
        assert out == [dt.date(2024, 12, 25), dt.date(2025, 1, 13)]
        assert plan.notes == []

    def test_unambiguous_month_first(self) -> None:
        plan, out = infer(["12/25/2024", "01/13/2025"])
        assert plan.type_name == "date"
        assert out == [dt.date(2024, 12, 25), dt.date(2025, 1, 13)]

    def test_ambiguous_dates_assume_day_first_and_say_so(self) -> None:
        plan, out = infer(["03/04/2025", "05/06/2025"])
        assert plan.type_name == "date"
        assert out == [dt.date(2025, 4, 3), dt.date(2025, 6, 5)]
        assert any("ambiguous day/month order" in n for n in plan.notes)

    def test_date_order_mdy_flips_ambiguous_dates(self) -> None:
        plan, out = infer(["03/04/2025", "05/06/2025"], order=DateOrder.MDY)
        assert out == [dt.date(2025, 3, 4), dt.date(2025, 5, 6)]
        assert plan.notes == []

    def test_date_order_dmy_refuses_month_first_only_data(self) -> None:
        plan, _ = infer(["12/25/2024", "01/13/2025"], order=DateOrder.DMY)
        assert plan.type_name == "string"

    def test_date_order_mdy_refuses_day_first_only_data(self) -> None:
        plan, _ = infer(["25/12/2024"], order=DateOrder.MDY)
        assert plan.type_name == "string"

    def test_ymd_is_treated_like_iso_first(self) -> None:
        plan, out = infer(["2024-01-15"], order=DateOrder.YMD)
        assert (plan.type_name, out) == ("date", [dt.date(2024, 1, 15)])

    def test_month_names(self) -> None:
        plan, out = infer(["15 Jan 2024", "03-Feb-2024", "1 March 2024"])
        assert plan.type_name == "date"
        assert out == [dt.date(2024, 1, 15), dt.date(2024, 2, 3), dt.date(2024, 3, 1)]

    def test_times_make_a_timestamp(self) -> None:
        plan, out = infer(["2024-01-15 10:30:00", "2024-01-16 00:00:00"])
        assert plan.type_name == "timestamp"
        assert out == [dt.datetime(2024, 1, 15, 10, 30), dt.datetime(2024, 1, 16)]

    def test_twelve_hour_clock(self) -> None:
        plan, out = infer(["25/12/2024 03:15 PM", "26/12/2024 09:00 AM"])
        assert plan.type_name == "timestamp"
        assert out == [dt.datetime(2024, 12, 25, 15, 15), dt.datetime(2024, 12, 26, 9, 0)]

    def test_offsets_make_timestamptz(self) -> None:
        plan, out = infer(["2024-01-15T10:30:00Z", "2024-01-15T10:30:00+02:00"])
        assert plan.type_name == "timestamptz"
        assert [v.astimezone(dt.UTC).replace(tzinfo=None) for v in out] == [
            dt.datetime(2024, 1, 15, 10, 30),
            dt.datetime(2024, 1, 15, 8, 30),
        ]

    def test_an_even_mix_of_offsets_and_naive_values_stays_text(self) -> None:
        """Neither reading fits both: the offset is never dropped, and UTC is not assumed."""
        values = ["2024-01-15T10:30:00+02:00", "2024-01-15 10:30:00"]
        plan, out = infer(values)
        assert plan.type_name == "string"
        assert out == values

    def test_date_rejects_are_counted_under_tolerance(self) -> None:
        values = [f"2024-01-{d:02d}" for d in range(1, 29)] * 4 + ["2024-02-30"]
        plan, out = infer(values)
        assert plan.type_name == "date"
        assert plan.rejected_count == 1
        assert plan.rejected_examples == ["2024-02-30"]
        assert out[-1] is None

    def test_text_that_merely_mentions_a_month_stays_text(self) -> None:
        plan, out = infer(["may the force", "be with you"])
        assert plan.type_name == "string"
        assert out == ["may the force", "be with you"]


class TestTextPlan:
    def test_text_is_trimmed(self) -> None:
        plan, out = infer(["  padded  ", "x"])
        assert plan.type_name == "string"
        assert out == ["padded", "x"]

    def test_plan_batches_many_columns(self) -> None:
        """More than one SQL batch (40 columns) must still give every column its own plan."""
        columns = {f"c{i}": [str(i), str(i + 1)] for i in range(45)}
        columns["c44"] = ["x", "y"]
        con = _table(columns)
        inferrer = TypeInferrer(
            con, "raw", extra_null_tokens=[], date_order=DateOrder.AUTO, prefer_decimal_comma=False
        )
        plans = inferrer.plan(list(columns))
        assert {n for n, p in plans.items() if p.type_name == "int64"} == {
            f"c{i}" for i in range(44)
        }
        assert plans["c44"].type_name == "string"

    def test_temporal_batches_many_columns(self) -> None:
        columns = {f"d{i}": ["2024-01-01", "2024-01-02"] for i in range(41)}
        con = _table(columns)
        inferrer = TypeInferrer(
            con, "raw", extra_null_tokens=[], date_order=DateOrder.AUTO, prefer_decimal_comma=False
        )
        assert {p.type_name for p in inferrer.plan(list(columns)).values()} == {"date"}


class TestCastText:
    """Best-effort casts used when expected_schema mandates a type for a text column."""

    def _cast(self, values, type_name, sql_type, *, decimal_comma=False, order=DateOrder.AUTO):
        con = _table({"c": values})
        inferrer = TypeInferrer(
            con,
            "raw",
            extra_null_tokens=[],
            date_order=order,
            prefer_decimal_comma=decimal_comma,
        )
        return _values(con, inferrer.cast_text("c", type_name, sql_type))

    def test_int_cast_rejects_fractions(self) -> None:
        assert self._cast(["1", "2.5", "1,000", "x"], "int64", "BIGINT") == [1, None, 1000, None]

    def test_float_cast_handles_both_styles(self) -> None:
        assert self._cast(["1.5", "1,234.5"], "float64", "DOUBLE") == [1.5, 1234.5]
        assert self._cast(["1,5", "1.234,5"], "float64", "DOUBLE", decimal_comma=True) == [
            1.5,
            1234.5,
        ]

    def test_bool_cast_accepts_zero_one(self) -> None:
        assert self._cast(["1", "0", "yes", "maybe"], "bool", "BOOLEAN") == [
            True,
            False,
            True,
            None,
        ]

    def test_date_and_timestamp_casts(self) -> None:
        assert self._cast(["25/12/2024"], "date", "DATE") == [dt.date(2024, 12, 25)]
        assert self._cast(["12/25/2024"], "date", "DATE", order=DateOrder.MDY) == [
            dt.date(2024, 12, 25)
        ]
        assert self._cast(["2024-01-01 10:00:00"], "timestamp", "TIMESTAMP") == [
            dt.datetime(2024, 1, 1, 10)
        ]

    def test_timestamptz_and_string_casts(self) -> None:
        (value,) = self._cast(["2024-01-01T00:00:00Z"], "timestamptz", "TIMESTAMPTZ")
        assert value.astimezone(dt.UTC) == dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
        assert self._cast([" x "], "string", "VARCHAR") == ["x"]
