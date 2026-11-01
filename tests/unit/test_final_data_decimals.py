"""Regression from the final data review: the decimal convention came from the delimiter alone.

A semicolon file that writes decimal points (price '398.53', weight '16.469' kg) had every
weight read as 16469, 1000x too large, in a 200: the weights fit both conventions, and the
semicolon decided, although the prices in the same rows can only be read with a decimal
point. The expected_schema cast did the same ('48.137;1.5' gave v=48137.0 beside w=1.5).
The file's own number columns decide now; the delimiter only when none of them can.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

import aimart_testkit as kit
from aimart_parquet.engine.duck import connect, ident
from aimart_parquet.engine.infer import TypeInferrer
from aimart_parquet.engine.models import DateOrder

if TYPE_CHECKING:
    import duckdb

AMBIGUOUS = "ambiguous thousands/decimal separator"


def _shop_export(rnd: random.Random, price: str) -> tuple[str, list[float]]:
    rows = ["date;sku;price_eur;weight_kg"]
    weights = []
    for i in range(40):
        w = rnd.randrange(1000, 49999) / 1000
        weights.append(w)
        p = f"{rnd.randrange(100, 99999) / 100:.2f}"
        rows.append(f"2025-03-{1 + i % 28:02d};SKU{i:03d};{price.format(p)};{w:.3f}")
    return "\n".join(rows) + "\n", weights


def test_a_semicolon_file_with_decimal_points_keeps_three_decimal_values(tmp_path) -> None:
    text, weights = _shop_export(random.Random(4), "{}")
    result = kit.convert_bytes(tmp_path, "shop_export.csv", text)
    assert result.column("weight_kg").type == "double"  # was int64, every value 1000x
    assert result.rows["weight_kg"] == pytest.approx(weights)
    assert result.column("price_eur").type == "double"
    note = next(n for n in result.column("weight_kg").notes if n.startswith(AMBIGUOUS))
    assert "decimal point" in note


def test_a_semicolon_file_with_decimal_commas_still_reads_dot_groups_as_thousands(
    tmp_path,
) -> None:
    text, weights = _shop_export(random.Random(4), "{}")
    text = "\n".join(
        line
        if i == 0
        else ";".join(
            [*line.split(";")[:2], line.split(";")[2].replace(".", ","), line.split(";")[3]]
        )
        for i, line in enumerate(text.splitlines())
    )
    result = kit.convert_bytes(tmp_path, "shop_eu.csv", text + "\n")
    assert result.column("weight_kg").type == "int64"  # '16.469' grams in a comma file
    assert result.rows["weight_kg"] == [round(w * 1000) for w in weights]
    assert any(n.startswith(AMBIGUOUS) for n in result.column("weight_kg").notes)


def test_with_no_evidence_the_semicolon_still_decides(tmp_path) -> None:
    result = kit.convert_bytes(tmp_path, "eu.csv", "Preis;Menge\n1,234;3\n5,678;4\n")
    assert result.rows["preis"] == pytest.approx([1.234, 5.678])


def test_a_comma_file_with_decimal_commas_reads_dot_groups_as_thousands(tmp_path) -> None:
    result = kit.convert_bytes(
        tmp_path, "q.csv", 'price,weight\n"12,50",1.234\n"7,25",2.500\n"3,10",12.750\n'
    )
    assert result.rows["price"] == pytest.approx([12.5, 7.25, 3.1])
    assert result.rows["weight"] == [1234, 2500, 12750]


@pytest.mark.parametrize("infer_types", [True, False])
def test_the_expected_schema_cast_decides_the_same_way(tmp_path, infer_types) -> None:
    # dot3_semicolon_float.csv: v=48137.0 beside w=1.5 in the same row.
    result = kit.convert_bytes(
        tmp_path,
        "dot3_semicolon_float.csv",
        "v;w\n48.137;1.5\n12.250;2.25\n7.001;0.5\n",
        infer_types=infer_types,
        expected_schema=[{"name": "v", "type": "float64"}, {"name": "w", "type": "float64"}],
    )
    assert result.rows["v"] == pytest.approx([48.137, 12.25, 7.001])
    assert result.rows["w"] == pytest.approx([1.5, 2.25, 0.5])


def _con(columns: dict[str, list[str | None]]) -> duckdb.DuckDBPyConnection:
    con = connect(memory_limit="256MB", threads=1)
    con.execute(f"CREATE TABLE raw ({', '.join(f'{ident(c)} VARCHAR' for c in columns)})")
    rows = list(zip(*columns.values(), strict=True))
    con.executemany(f"INSERT INTO raw VALUES ({', '.join('?' for _ in columns)})", rows)
    return con


def _read(columns: dict[str, list[str | None]], *, decimal_comma: bool) -> dict[str, list[Any]]:
    con = _con(columns)
    inferrer = TypeInferrer(
        con,
        "raw",
        extra_null_tokens=[],
        date_order=DateOrder.AUTO,
        prefer_decimal_comma=decimal_comma,
    )
    plans = inferrer.plan(list(columns))
    return {
        c: con.sql(f"SELECT {p.expr} FROM raw").to_arrow_table().column(0).to_pylist()
        for c, p in plans.items()
    }


class TestEvidence:
    AMBIGUOUS: ClassVar[list[str | None]] = ["16.469", "7.761", "26.956"]

    @pytest.mark.parametrize(
        "evidence",
        [
            ["398.53", "1.5", "20.1"],  # a fraction that is not three digits
            ["0.250", "0.125", "0.500"],  # a leading zero: never a thousands group
            ["1,234.5", "2,000.25", "3.5"],  # US grouping with decimals
        ],
    )
    def test_values_only_a_decimal_point_reads(self, evidence) -> None:
        out = _read({"a": self.AMBIGUOUS, "b": evidence}, decimal_comma=True)
        assert out["a"] == pytest.approx([16.469, 7.761, 26.956])

    @pytest.mark.parametrize("evidence", [["12,5", "3,75", "1,0"], ["1.234,5", "2.000,25", "7"]])
    def test_values_only_a_decimal_comma_reads(self, evidence) -> None:
        out = _read({"a": self.AMBIGUOUS, "b": evidence}, decimal_comma=False)
        assert out["a"] == [16469, 7761, 26956]

    def test_text_columns_are_no_evidence(self) -> None:
        # 'v1.5' or '1.5 kg' in a column that is not a number column says nothing.
        out = _read(
            {"a": self.AMBIGUOUS, "b": ["1.5 kg", "release 2.5", "n/a or 3.25"]},
            decimal_comma=True,
        )
        assert out["a"] == [16469, 7761, 26956]

    def test_a_tie_falls_back_to_the_delimiter(self) -> None:
        columns = {"a": self.AMBIGUOUS, "b": ["1.5", "2.5", "3.5"], "c": ["1,5", "2,5", "3,5"]}
        assert _read(columns, decimal_comma=True)["a"] == [16469, 7761, 26956]
        assert _read(columns, decimal_comma=False)["a"] == pytest.approx([16.469, 7.761, 26.956])
