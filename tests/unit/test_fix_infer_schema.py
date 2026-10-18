"""Regressions in expected_schema: requested decimals and strict mode.

* I5: ``scale: 0`` was read as "unset" (``col.scale or 2``), so DECIMAL(10, 0) became
  DECIMAL(10, 2): 1234567890 overflowed to NULL, 42 became 42.00, and strict=true failed on data
  that matched the requested schema. A scale above the precision crashed in DuckDB (a 500).
* strict=true counted only the cast's own rejects. Values the 98% tolerance had already nulled
  during inference passed, so the caller paid for a column with 'oops' silently made null.
"""

from __future__ import annotations

import decimal
import re

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
