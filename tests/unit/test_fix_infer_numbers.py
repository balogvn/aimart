"""Regressions: numbers read with the wrong separator convention became wrong values, charged.

Each case below returned a plausible number that was 10x or 1000x off (or a different number
entirely) with ``rejected_count == 0``, so the caller paid for corrupted data:

* I1: under the 98% tolerance, misfits were cast with the winning style's separator rules
  ('1.5' in an EU column -> 15.0, '2,5' in a US column -> 25.0).
* I2: ``expected_schema`` casts of text columns rewrote separators in every value
  ('12,5' in a comma CSV -> int 125).
* I4: dot-grouped thousands in a decimal-comma file ('1.234') were read as 1.234.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit
from aimart_parquet.engine.duck import connect, ident
from aimart_parquet.engine.infer import ColumnPlan, TypeInferrer
from aimart_parquet.engine.models import DateOrder
from aimart_parquet.errors import SchemaViolation

if TYPE_CHECKING:
    import duckdb

AMBIGUOUS = "ambiguous thousands/decimal separator"


def _con(values: list[str | None]) -> duckdb.DuckDBPyConnection:
    con = connect(memory_limit="256MB", threads=1)
    con.execute(f"CREATE TABLE raw ({ident('c')} VARCHAR)")
    con.executemany("INSERT INTO raw VALUES (?)", [(v,) for v in values])
    return con


def _inferrer(con: duckdb.DuckDBPyConnection, *, decimal_comma: bool) -> TypeInferrer:
    return TypeInferrer(
        con,
        "raw",
        extra_null_tokens=[],
        date_order=DateOrder.AUTO,
        prefer_decimal_comma=decimal_comma,
    )


def _values(con: duckdb.DuckDBPyConnection, expr: str) -> list[Any]:
    return con.sql(f"SELECT {expr} FROM raw").to_arrow_table().column(0).to_pylist()


def infer(values: list[str | None], *, decimal_comma: bool = False) -> tuple[ColumnPlan, list]:
    con = _con(values)
    plan = _inferrer(con, decimal_comma=decimal_comma).plan(["c"])["c"]
    return plan, _values(con, plan.expr)


def cast(values: list[str | None], type_name: str, sql_type: str, *, decimal_comma: bool) -> list:
    con = _con(values)
    return _values(
        con, _inferrer(con, decimal_comma=decimal_comma).cast_text("c", type_name, sql_type)
    )


class TestTolerantMisfitsAreRejectsNotWrongNumbers:
    """I1: the 2% misfits must become counted nulls, never numbers in the wrong convention."""

    @pytest.mark.parametrize(
        ("good", "misfit", "decimal_comma", "expected"),
        [
            ("1.234,50", "1.5", True, 1234.5),  # EU column: '1.5' was 15.0
            ("1,234.50", "2,5", False, 1234.5),  # US column: '2,5' was 25.0
            ("1,234.50", "1.234,56", False, 1234.5),  # US column: was 1.23456
            ("1 234,50", "1.5", False, 1234.5),  # space column: '1.5' was 1.5 via '.' -> '.'
            ("12.5", "1_000", False, 12.5),  # DuckDB's DOUBLE cast reads '1_000' as 1000
        ],
    )
    def test_float_columns(self, good, misfit, decimal_comma, expected) -> None:
        plan, out = infer([good] * 49 + [misfit], decimal_comma=decimal_comma)
        assert plan.type_name == "float64"
        assert out[0] == pytest.approx(expected)
        assert out[-1] is None
        assert plan.rejected_count == 1
        assert plan.rejected_examples == [misfit]

    def test_eu_integer_column(self) -> None:
        """'1.5' -> '15' passed the integer regex gate, which only looked at the converted text."""
        plan, out = infer(["1.234.567"] * 49 + ["1.5"], decimal_comma=True)
        assert plan.type_name == "int64"
        assert out[0] == 1_234_567
        assert out[-1] is None
        assert (plan.rejected_count, plan.rejected_examples) == (1, ["1.5"])

    def test_us_integer_column(self) -> None:
        """'12,5' -> '125' passed the same gate in a US column (two groups, so EU cannot fit)."""
        plan, out = infer(["1,234,567"] * 49 + ["12,5"])
        assert plan.type_name == "int64"
        assert out[0] == 1_234_567
        assert out[-1] is None
        assert (plan.rejected_count, plan.rejected_examples) == (1, ["12,5"])

    def test_misfits_are_rejects_end_to_end(self, tmp_path) -> None:
        rows = "".join(f'"1.234,50";{i}\n' for i in range(49)) + "1.5;99\n"
        result = kit.convert_bytes(tmp_path, "a.csv", "amount;id\n" + rows)
        assert result.rows["amount"][0] == pytest.approx(1234.5)
        assert result.rows["amount"][-1] is None  # was 15.0
        amount = result.column("amount")
        assert (amount.rejected_count, amount.rejected_examples) == (1, ["1.5"])

    def test_ties_under_tolerance_follow_the_dialect(self) -> None:
        """'1,234' fits US and EU equally: the dialect decides, as it does without misfits."""
        values = [f"{i},234" for i in range(1, 50)] + ["oops"]
        us, us_out = infer(values, decimal_comma=False)
        eu, eu_out = infer(values, decimal_comma=True)
        assert (us.type_name, us_out[0]) == ("int64", 1234)
        assert eu.type_name == "float64"
        assert eu_out[0] == pytest.approx(1.234)
        assert AMBIGUOUS in " ".join(us.notes)
        assert AMBIGUOUS in " ".join(eu.notes)
        assert us.rejected_examples == eu.rejected_examples == ["oops"]


class TestDotGroupedThousands:
    """I4: in a decimal-comma file '1.234' is one thousand two hundred thirty-four."""

    def test_decimal_comma_dialect_reads_dot_groups_as_thousands(self) -> None:
        plan, out = infer(["1.234", "2.500", "12.750"], decimal_comma=True)
        assert (plan.type_name, out) == ("int64", [1234, 2500, 12750])
        assert any(n.startswith(AMBIGUOUS) and "decimal comma" in n for n in plan.notes)

    def test_decimal_point_dialect_reads_them_as_decimals_and_says_so(self) -> None:
        plan, out = infer(["1.234", "2.500", "12.750"], decimal_comma=False)
        assert plan.type_name == "float64"
        assert out == pytest.approx([1.234, 2.5, 12.75])
        assert any(n.startswith(AMBIGUOUS) and "decimal point" in n for n in plan.notes)

    def test_a_plain_decimal_settles_it_for_the_decimal_point(self) -> None:
        """'2.5' cannot be dot-grouped, so the column is plain decimals: no guess, no note."""
        plan, out = infer(["1.234", "2.5"], decimal_comma=True)
        assert plan.type_name == "float64"
        assert out == pytest.approx([1.234, 2.5])
        assert not any(AMBIGUOUS in n for n in plan.notes)

    def test_integers_alone_are_not_ambiguous(self) -> None:
        plan, out = infer(["1", "234", "5000"], decimal_comma=True)
        assert (plan.type_name, out) == ("int64", [1, 234, 5000])
        assert plan.notes == []

    def test_a_leading_zero_group_is_never_thousands(self) -> None:
        """Nobody writes 125 as '0.125' or '0,125': those are decimals in either convention."""
        plan, out = infer(["0.125", "1.500"], decimal_comma=True)
        assert plan.type_name == "float64"
        assert out == pytest.approx([0.125, 1.5])
        us, us_out = infer(["0,125", "12,5"], decimal_comma=False)
        assert us.type_name == "float64"
        assert us_out == pytest.approx([0.125, 12.5])

    def test_us_and_eu_ties_carry_the_note(self) -> None:
        plan, _ = infer(["1,234", "5,678"], decimal_comma=False)
        assert any(n.startswith(AMBIGUOUS) and "decimal point" in n for n in plan.notes)

    def test_a_near_rival_reading_is_reported(self) -> None:
        """One '12,5' makes EU the only full fit, but US fits 98%: the reader must be told."""
        plan, out = infer([f"{i},234" for i in range(1, 50)] + ["12,5"])
        assert plan.type_name == "float64"
        assert out[-1] == pytest.approx(12.5)
        assert any(n.startswith(AMBIGUOUS) and "decimal comma" in n for n in plan.notes)

    def test_end_to_end_semicolon_csv(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "a.csv", "amount;x\n1.234;a\n2.500;b\n12.750;c\n")
        assert result.report.delimiter == ";"
        assert result.rows["amount"] == [1234, 2500, 12750]
        assert any(AMBIGUOUS in n for n in result.column("amount").notes)


class TestCastTextKeepsTheConvention:
    """I2: expected_schema casts rewrite separators only for values in the file's convention."""

    @pytest.mark.parametrize(
        ("type_name", "sql_type"), [("int64", "BIGINT"), ("float64", "DOUBLE")]
    )
    def test_comma_decimals_in_a_comma_file_are_rejects(self, type_name, sql_type) -> None:
        out = cast(["12,5", "3,75", "abc"], type_name, sql_type, decimal_comma=False)
        assert out == [None, None, None]  # were 125 / 375 (10x and 100x off)

    def test_us_grouping_in_a_semicolon_file_is_a_reject(self) -> None:
        out = cast(["1,234.5", "1.234,5", "2,5"], "float64", "DOUBLE", decimal_comma=True)
        assert out == pytest.approx([None, 1234.5, 2.5])  # '1,234.5' was 1.2345

    def test_decimal_target(self) -> None:
        out = cast(["1,234.5", "12,5"], "decimal", "DECIMAL(10, 2)", decimal_comma=False)
        assert [None if v is None else float(v) for v in out] == [1234.5, None]

    def test_valid_values_still_cast(self) -> None:
        assert cast(["1,234", "12", "1 234", "$5"], "int64", "BIGINT", decimal_comma=False) == [
            1234,
            12,
            1234,
            5,
        ]
        assert cast(["1.234", "1,5", "2.5"], "float64", "DOUBLE", decimal_comma=True) == (
            pytest.approx([1234.0, 1.5, 2.5])
        )

    def test_strict_schema_fails_instead_of_charging_for_10x_values(self, tmp_path) -> None:
        csv = 'v,w\n"12,5",a\n"3,75",b\nabc,c\n'
        with pytest.raises(SchemaViolation) as info:
            kit.convert_bytes(
                tmp_path,
                "a.csv",
                csv,
                expected_schema=[{"name": "v", "type": "int64"}],
                strict=True,
            )
        assert {"column": "v", "problem": "3 values do not fit int64"} in info.value.details[
            "violations"
        ]

    def test_non_strict_schema_counts_the_rejects(self, tmp_path) -> None:
        csv = 'v,w\n"12,5",a\n"3,75",b\nabc,c\n'
        result = kit.convert_bytes(
            tmp_path, "a.csv", csv, expected_schema=[{"name": "v", "type": "float64"}]
        )
        assert result.rows["v"] == [None, None, None]
        v = result.column("v")
        assert v.rejected_count == 3
        assert v.rejected_examples == ["12,5", "3,75", "abc"]  # were [], a count without values

    @pytest.mark.parametrize(
        ("csv", "rows", "note"),
        [
            ("v;w\n1.234;a\n2,5;b\n", [1234.0, 2.5], "decimal comma, so '1.234' is 1234.0"),
            ('v,w\n"1,234",a\n2.5,b\n', [1234.0, 2.5], "decimal point, so '1,234' is 1234.0"),
            ("v,w\n1.234,a\n2.5,b\n", [1.234, 2.5], "decimal point, so '1.234' is 1.234"),
        ],
    )
    def test_the_dialect_choice_is_reported(self, tmp_path, csv, rows, note) -> None:
        """A per-value cast cannot see the column: the dialect decided, and the report says so."""
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            csv,
            infer_types=False,
            expected_schema=[{"name": "v", "type": "float64"}],
        )
        assert result.rows["v"] == pytest.approx(rows)
        notes = result.column("v").notes
        assert any(n.startswith(AMBIGUOUS) and note in n for n in notes), notes
        assert any("declare it as string in expected_schema" in n for n in notes)

    def test_unambiguous_values_carry_no_note(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            "v;w\n1.234,5;a\n2,5;b\n12;c\n",
            infer_types=False,
            expected_schema=[{"name": "v", "type": "float64"}],
        )
        assert result.rows["v"] == pytest.approx([1234.5, 2.5, 12.0])
        assert result.column("v").notes == ["cast to float64 per expected_schema"]

    def test_typed_column_cast_rejects_have_examples(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path, "a.csv", "v,w\n1.5,a\n2,b\n", expected_schema=[{"name": "v", "type": "int64"}]
        )
        assert result.rows["v"] == [None, 2]
        assert (result.column("v").rejected_count, result.column("v").rejected_examples) == (
            1,
            ["1.5"],
        )
