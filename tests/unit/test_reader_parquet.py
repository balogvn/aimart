"""Parquet in: names re-normalized, string columns re-typed, native types kept, labels kept."""

from __future__ import annotations

import datetime as dt

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import aimart_testkit as kit
from aimart_parquet.errors import InvalidInput


def test_parquet_input_is_cleaned(tmp_path) -> None:
    schema = pa.schema(
        [
            pa.field("Order ID", pa.string()),
            pa.field("Amount", pa.large_string()),
            pa.field("When", pa.date32(), metadata={b"label": b"Order date"}),
            pa.field("Qty", pa.int32()),
        ]
    )
    table = pa.table(
        {
            "Order ID": ["001", "002"],
            "Amount": ["1,234.5", "7"],
            "When": [dt.date(2024, 1, 1), dt.date(2024, 1, 2)],
            "Qty": [1, 2],
        },
        schema=schema,
    )
    src = tmp_path / "in.parquet"
    pq.write_table(table, src)
    result = kit.convert_path(src, filename=None)
    assert result.report.source_format == "parquet"
    assert result.rows == {
        "order_id": ["001", "002"],
        "amount": [1234.5, 7.0],
        "when": [dt.date(2024, 1, 1), dt.date(2024, 1, 2)],
        "qty": [1, 2],
    }
    assert result.column("qty").type == "int32"  # native types pass through untouched
    assert result.column("when").label == "Order date"


def test_parquet_reconversion_can_validate_a_schema(tmp_path) -> None:
    src = kit.write_parquet(tmp_path / "in.parquet", {"v": [1.0, 2.0, 2.5]})
    result = kit.convert_path(src, expected_schema=[{"name": "v", "type": "int64"}])
    assert result.rows == {"v": [1, 2, None]}  # 2.5 is a counted reject, not a rounded 2
    assert result.column("v").rejected_count == 1


def test_unreadable_parquet(tmp_path) -> None:
    src = kit.write(tmp_path / "bad.parquet", b"PAR1 this is not really parquet PAR1")
    with pytest.raises(InvalidInput, match="Not a readable Parquet file"):
        kit.convert_path(src)
