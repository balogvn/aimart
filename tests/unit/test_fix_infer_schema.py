"""Regressions in expected_schema: requested decimals and strict mode.

* I5: ``scale: 0`` was read as "unset" (``col.scale or 2``), so DECIMAL(10, 0) became
  DECIMAL(10, 2): 1234567890 overflowed to NULL, 42 became 42.00, and strict=true failed on data
  that matched the requested schema. A scale above the precision crashed in DuckDB (a 500).
* strict=true counted only the cast's own rejects. Values the 98% tolerance had already nulled
  during inference passed, so the caller paid for a column with 'oops' silently made null.
* A column declared ``string`` was the inferred value cast back to text: '1.50' became '1.5',
  '15/01/2025' became '2025-01-15', and values the tolerance had nulled were lost.
* With scale 0 honoured, a decimal cast rounded silently: 12.5 became 13 under strict=true.
"""

from __future__ import annotations

import decimal
import re

import pyarrow as pa
import pytest

import aimart_testkit as kit
from aimart_parquet.errors import InvalidRequest, SchemaViolation

D = decimal.Decimal
WHOLE = {"name": "amt", "type": "decimal", "precision": 10, "scale": 0}


class TestDecimalScaleZero:
    @pytest.mark.parametrize("infer_types", [True, False])  # typed column, then text column
    def test_scale_zero_is_honoured(self, tmp_path, infer_types) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            "amt,code\n1234567890,x\n42,y\n",
            infer_types=infer_types,
            expected_schema=[WHOLE, {"name": "code", "type": "string"}],
            strict=True,  # used to fail: '1 values do not fit decimal'
        )
        assert result.column("amt").type == "decimal128(10, 0)"
        assert result.rows["amt"] == [D("1234567890"), D("42")]
        assert result.column("amt").rejected_count == 0

    def test_missing_column_gets_the_requested_scale(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "a.csv", "code\nx\n", expected_schema=[WHOLE])
        assert result.column("amt").type == "decimal128(10, 0)"

    def test_defaults_are_unchanged(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path, "a.csv", "amt\n12.5\n", expected_schema=[{"name": "amt", "type": "decimal"}]
        )
        assert result.column("amt").type == "decimal128(18, 2)"
        assert result.rows["amt"] == [D("12.50")]


class TestImpossibleDecimal:
    @pytest.mark.parametrize(
        ("column", "message"),
        [
            ({"precision": 4, "scale": 6}, "decimal scale 6 exceeds precision 4"),
            ({"precision": 1}, "decimal scale 2 (the default) exceeds precision 1"),
        ],
    )
    def test_is_an_invalid_request_not_a_crash(self, tmp_path, column, message) -> None:
        with pytest.raises(InvalidRequest, match=re.escape(message)) as info:
            kit.convert_bytes(
                tmp_path,
                "a.csv",
                "amt\n1\n",
                expected_schema=[{"name": "amt", "type": "decimal", **column}],
            )
        assert info.value.http_status == 400
        assert info.value.details == {"column": "amt"}
        assert "scale" in (info.value.hint or "")


class TestStrictCountsInferenceRejects:
    CSV = "v\n" + "".join(f"{i}\n" for i in range(99)) + "oops\n"

    def test_strict_fails_on_a_value_inference_nulled(self, tmp_path) -> None:
        with pytest.raises(SchemaViolation) as info:
            kit.convert_bytes(
                tmp_path,
                "a.csv",
                self.CSV,
                expected_schema=[{"name": "v", "type": "int64"}],
                strict=True,
            )
        assert info.value.details["violations"] == [
            {"column": "v", "problem": "1 values do not fit int64"}
        ]

    def test_cast_rejects_add_to_inference_rejects(self, tmp_path) -> None:
        csv = "v\n" + "".join(f"{i}.5\n" for i in range(98)) + "oops\n7\n"
        with pytest.raises(SchemaViolation) as info:
            kit.convert_bytes(
                tmp_path,
                "a.csv",
                csv,
                expected_schema=[{"name": "v", "type": "int64"}],
                strict=True,
            )
        # 98 fractions rejected by the int cast, plus 'oops' rejected by inference.
        assert info.value.details["violations"] == [
            {"column": "v", "problem": "99 values do not fit int64"}
        ]

    def test_non_strict_still_reports_the_count(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path, "a.csv", self.CSV, expected_schema=[{"name": "v", "type": "int64"}]
        )
        assert result.rows["v"][-1] is None
        assert result.column("v").rejected_count == 1


class TestStringKeepsTheText:
    """Declaring a column string asks for its text, not the inferred value cast back to text."""

    def test_inferred_columns_keep_their_original_text(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            "a;b;c;n\n1.50;1.234;15/01/2025;1\n2.25;2.500;16/01/2025;NA\n",
            expected_schema=[{"name": name, "type": "string"} for name in "abcn"],
            strict=True,
        )
        # Were '1.5', '1234' (decimal-comma file) and '2025-01-15': rewritten, and charged.
        assert result.rows == {
            "a": ["1.50", "2.25"],
            "b": ["1.234", "2.500"],
            "c": ["15/01/2025", "16/01/2025"],
            "n": ["1", "NA"],  # as in any text column, 'NA' is data unless listed in null_values
        }
        for column in result.report.columns:
            assert column.type == "string"
            assert (column.inferred_from_text, column.notes) == (False, [])

    def test_values_the_tolerance_nulled_come_back(self, tmp_path) -> None:
        """'oops' among 99 dates was nulled by inference, and strict passed: a lost value, charged."""
        csv = "d\n" + "".join(f"2025-01-{1 + i % 28:02d}\n" for i in range(99)) + "oops\n"
        result = kit.convert_bytes(
            tmp_path, "a.csv", csv, expected_schema=[{"name": "d", "type": "string"}], strict=True
        )
        assert result.rows["d"][0] == "2025-01-01"
        assert result.rows["d"][-1] == "oops"
        assert result.column("d").rejected_count == 0

    def test_null_values_still_apply(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            "n\n1\nNA\n",
            null_values=["NA"],
            expected_schema=[{"name": "n", "type": "string"}],
        )
        assert result.rows["n"] == ["1", None]


class TestDecimalNeverRoundsSilently:
    """RV3: DuckDB rounds to the requested scale without a word.

    With scale 0 honoured, {decimal, precision 10, scale 0} turned 12.5 into 13 and 0.4 into 0
    with no reject, and strict passed: the caller paid for rounded values. The int64 cast in
    the same function already makes a fraction a counted reject; a decimal must too, at every
    scale ('12.345' at scale 2), and from the text itself, not from a DOUBLE that lost digits.
    """

    SCHEMA = (
        {"name": "amount", "type": "decimal", "precision": 10, "scale": 0},
        {"name": "x", "type": "int64"},
    )
    CSV = "amount,x\n12.5,1\n42,2\n0.4,3\n"

    @pytest.mark.parametrize("infer_types", [True, False])  # typed column, then text column
    def test_strict_fails_instead_of_charging_for_rounded_values(
        self, tmp_path, infer_types
    ) -> None:
        with pytest.raises(SchemaViolation) as info:
            kit.convert_bytes(
                tmp_path,
                "a.csv",
                self.CSV,
                infer_types=infer_types,
                expected_schema=list(self.SCHEMA),
                strict=True,
            )
        assert info.value.details["violations"] == [
            {"column": "amount", "problem": "2 values do not fit decimal"}
        ]

    @pytest.mark.parametrize("infer_types", [True, False])
    def test_non_strict_counts_the_rounded_values(self, tmp_path, infer_types) -> None:
        result = kit.convert_bytes(
            tmp_path, "a.csv", self.CSV, infer_types=infer_types, expected_schema=list(self.SCHEMA)
        )
        assert result.rows["amount"] == [None, D("42"), None]  # were 13, 42, 0
        amount = result.column("amount")
        assert (amount.rejected_count, amount.rejected_examples) == (2, ["0.4", "12.5"])

    @pytest.mark.parametrize("infer_types", [True, False])
    def test_every_scale(self, tmp_path, infer_types) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            "amount,x\n12.345,1\n12.340,2\n12.3,3\n-0.125,4\n",
            infer_types=infer_types,
            expected_schema=[{"name": "amount", "type": "decimal"}],  # DECIMAL(18, 2)
        )
        assert result.rows["amount"] == [None, D("12.34"), D("12.30"), None]  # 12.35, -0.13
        assert result.column("amount").rejected_count == 2

    @pytest.mark.parametrize("precision", [18, 38])  # 38: no wider decimal to compare with
    def test_long_values_are_read_exactly(self, tmp_path, precision) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            "amount,x\n1234567890123456.78,1\n0.10,2\n12.345,3\n",
            expected_schema=[
                {"name": "amount", "type": "decimal", "precision": precision, "scale": 2}
            ],
        )
        # The first was 1234567890123456.80: inferred as DOUBLE, then cast.
        assert result.rows["amount"] == [D("1234567890123456.78"), D("0.10"), None]

    def test_the_text_convention_still_applies(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            'amount;x\n"1.234,56";a\n12,5;b\n7;c\n',
            expected_schema=[{"name": "amount", "type": "decimal", "precision": 10, "scale": 2}],
            strict=False,
        )
        assert result.rows["amount"] == [D("1234.56"), D("12.50"), D("7.00")]
        assert result.column("amount").rejected_count == 0

    def test_typed_sources(self, tmp_path) -> None:
        src = kit.write_parquet(
            tmp_path / "in.parquet",
            {
                "dec": pa.array([D("12.3400000000"), D("12.3456789012")], pa.decimal128(38, 10)),
                "dbl": pa.array([12.5, 42.0], pa.float64()),
                "big": pa.array([7, 2**40], pa.int64()),
            },
        )
        result = kit.convert_path(
            src,
            expected_schema=[
                {"name": "dec", "type": "decimal", "precision": 18, "scale": 2},
                {"name": "dbl", "type": "decimal", "precision": 10, "scale": 0},
                {"name": "big", "type": "decimal", "precision": 18, "scale": 2},
            ],
        )
        assert result.rows == {
            "dec": [D("12.34"), None],  # was 12.35
            "dbl": [None, D("42")],  # was 13
            "big": [D("7.00"), D(2**40).quantize(D("0.01"))],
        }
        assert [c.rejected_count for c in result.report.columns] == [1, 1, 0]

    def test_non_numbers_cast_as_before(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            "flag,x\nyes,1\nno,2\n",
            expected_schema=[{"name": "flag", "type": "decimal"}],
        )
        assert result.rows["flag"] == [D("1.00"), D("0.00")]


class TestDecimalFromAnInferredNumber:
    """A number inference read from text is read again from its text for a decimal target.

    The first version of that reset the column to its raw text, so the 'NA' and 'n/a' that
    inference had read as nulls became counted rejects (strict refused a column that fits the
    schema), and the report lost what inference had said ('stripped currency symbols').
    """

    SCHEMA = ({"name": "amount", "type": "decimal"}, {"name": "x", "type": "int64"})

    @pytest.mark.parametrize("strict", [False, True])
    def test_null_tokens_stay_nulls(self, tmp_path, strict) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            "amount,x\n12.50,1\nNA,2\n0.40,3\nn/a,4\n",
            expected_schema=list(self.SCHEMA),
            strict=strict,  # failed: '2 values do not fit decimal'
        )
        assert result.rows["amount"] == [D("12.50"), None, D("0.40"), None]
        amount = result.column("amount")
        assert (amount.rejected_count, amount.rejected_examples, amount.null_count) == (0, [], 2)

    def test_the_inference_report_is_kept(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            'amount;x\n"€1.234,56";1\n"€12,50";2\n"€7";3\n',
            expected_schema=list(self.SCHEMA),
        )
        assert result.rows["amount"] == [D("1234.56"), D("12.50"), D("7.00")]
        amount = result.column("amount")
        assert amount.inferred_from_text
        assert amount.notes == [
            "parsed eu-style number formatting",
            "stripped currency symbols",
            "cast to decimal per expected_schema",
        ]

    @pytest.mark.parametrize("strict", [False, True])
    def test_tolerance_misfits_and_rounding_are_both_counted(self, tmp_path, strict) -> None:
        cells = [f"{i}.25" for i in range(60)] + ["oops", "1.255", "NA"]  # 61 of 62 fit
        csv = "amount,x\n" + "".join(f"{c},{i}\n" for i, c in enumerate(cells))
        if strict:
            with pytest.raises(SchemaViolation) as info:
                kit.convert_bytes(
                    tmp_path, "a.csv", csv, expected_schema=list(self.SCHEMA), strict=True
                )
            assert info.value.details["violations"] == [
                {"column": "amount", "problem": "2 values do not fit decimal"}
            ]
            return
        result = kit.convert_bytes(tmp_path, "a.csv", csv, expected_schema=list(self.SCHEMA))
        assert result.rows["amount"][:2] == [D("0.25"), D("1.25")]
        assert result.rows["amount"][60:] == [None, None, None]
        amount = result.column("amount")
        assert (amount.rejected_count, amount.rejected_examples) == (2, ["oops", "1.255"])
