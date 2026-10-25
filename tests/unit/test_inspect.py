"""inspect: everything comes from the footer; no row data is read."""

from __future__ import annotations

import datetime as dt
import decimal
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import aimart_testkit as kit
from aimart_parquet.engine import inspect as inspect_module
from aimart_parquet.engine.inspect import _jsonable, inspect_parquet, type_name
from aimart_parquet.errors import InvalidInput


def test_stats_span_every_row_group(tmp_path) -> None:
    path = kit.write_parquet(
        tmp_path / "t.parquet",
        {"id": list(range(10)), "name": [None if i % 4 == 0 else f"n{i}" for i in range(10)]},
        row_group_size=4,
    )
    result = inspect_parquet(path)
    assert (result.rows, result.row_groups) == (10, 3)
    ids, names = result.columns
    assert (ids.name, ids.type, ids.min, ids.max, ids.null_count) == ("id", "int64", 0, 9, 0)
    assert (names.min, names.max, names.null_count) == ("n1", "n9", 3)
    assert result.size_bytes == path.stat().st_size
    assert result.created_by is not None


def test_does_not_read_row_data(tmp_path, monkeypatch) -> None:
    path = kit.write_parquet(tmp_path / "t.parquet", {"a": [1, 2]})

    def boom(*_a, **_k):
        raise AssertionError("inspect must not read rows")

    monkeypatch.setattr(pq.ParquetFile, "read", boom)
    monkeypatch.setattr(pq.ParquetFile, "read_row_group", boom)
    monkeypatch.setattr(pq, "read_table", boom)
    assert inspect_parquet(path).rows == 2


def test_labels_and_value_labels_come_from_field_metadata(tmp_path) -> None:
    src = kit.survey_file(tmp_path / "s.sav", "sav")
    converted = kit.convert_path(src)
    result = inspect_parquet(converted.path)
    gender = next(c for c in result.columns if c.name == "gender")
    assert gender.label == "Respondent gender"
    assert gender.value_labels == {"1": "Male", "2": "Female"}
    assert "aimart.columns" in result.key_value_metadata
    assert "ARROW:schema" not in result.key_value_metadata  # internal blobs are hidden


def test_malformed_value_labels_are_ignored(tmp_path) -> None:
    field = pa.field("g", pa.int64(), metadata={b"value_labels": b"{not json", b"label": b"G"})
    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"g": [1]}, schema=pa.schema([field])), path)
    (col,) = inspect_parquet(path).columns
    assert col.label == "G"
    assert col.value_labels is None


def test_long_kv_values_are_truncated_visibly(tmp_path) -> None:
    path = tmp_path / "t.parquet"
    table = pa.table({"a": [1]}).replace_schema_metadata({"note": "x" * 5000, "small": "y"})
    pq.write_table(table, path)
    kv = inspect_parquet(path).key_value_metadata
    assert kv["small"] == "y"
    assert len(kv["note"]) == 2000
    assert kv["note"].endswith("…")


def test_nested_columns_have_no_leaf_stats(tmp_path) -> None:
    path = kit.write_parquet(tmp_path / "t.parquet", {"l": [[1, 2]], "s": [{"a": 1}]})
    lst, struct = inspect_parquet(path).columns
    assert lst.type.startswith("list<")
    assert (lst.null_count, lst.min, lst.max) == (None, None, None)
    assert struct.type.startswith("struct<")


def test_missing_statistics_leave_stats_unknown(tmp_path) -> None:
    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"a": [1, 2]}), path, write_statistics=False)
    (col,) = inspect_parquet(path).columns
    assert (col.null_count, col.min, col.max) == (None, None, None)


def test_large_string_is_reported_as_string(tmp_path) -> None:
    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"s": pa.array(["a"], type=pa.large_string())}), path)
    assert inspect_parquet(path).columns[0].type == "string"
    assert type_name(pa.large_string()) == "string"


def test_not_parquet(tmp_path) -> None:
    path = kit.write(tmp_path / "x.parquet", b"a,b\n1,2\n")
    with pytest.raises(InvalidInput, match="Not a readable Parquet file"):
        inspect_parquet(path)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (3, 3),
        (1.5, 1.5),
        (dt.date(2024, 1, 2), "2024-01-02"),
        (dt.datetime(2024, 1, 2, 3, 4), "2024-01-02T03:04:00"),
        (decimal.Decimal("1.50"), "1.50"),
        (b"abc", "abc"),
        (b"\xff\xfe", "fffe"),
        ("x" * 150, "x" * 99 + "…"),
    ],
)
def test_jsonable(value, expected) -> None:
    assert _jsonable(value) == expected


def test_stats_with_incomparable_types_are_dropped(tmp_path, monkeypatch) -> None:
    """A min/max comparison that raises must degrade to 'unknown', not crash."""
    path = kit.write_parquet(tmp_path / "t.parquet", {"a": [1, 2, 3]}, row_group_size=1)

    class Weird:
        def __lt__(self, other):
            raise TypeError

        __gt__ = __lt__

    real = pq.ParquetFile

    class Patched(real):  # type: ignore[misc, valid-type]
        @property
        def metadata(self):
            md = super().metadata

            class RG:
                def __init__(self, rg):
                    self._rg = rg

                def column(self, j):
                    st = self._rg.column(j).statistics

                    class S:
                        has_null_count = True
                        null_count = st.null_count
                        has_min_max = True
                        min = Weird()
                        max = Weird()

                    class C:
                        statistics = S()

                    return C()

            class MD:
                num_rows = md.num_rows
                num_row_groups = md.num_row_groups
                num_columns = md.num_columns
                schema = md.schema
                created_by = md.created_by
                metadata = md.metadata

                def row_group(self, i):
                    return RG(md.row_group(i))

            return MD()

    monkeypatch.setattr(inspect_module.pq, "ParquetFile", Patched)
    (col,) = inspect_parquet(path).columns
    assert (col.min, col.max) == (None, None)
    assert col.null_count == 0  # counted without comparing anything
    json.dumps(col.model_dump())


def test_values_beyond_python_dates_degrade_to_unknown(tmp_path) -> None:
    """RV4: Python stops at year 9999; Parquet and DuckDB do not.

    An offset applied to a '9999-12-31 23:59:59-05:00' sentinel lands in year 10000, and
    reading the footer max as a Python datetime raised OverflowError: convert failed with a
    bare 500 ('Internal error ... Retry') on a valid file.
    """
    past_9999 = 253402300800 * 10**6  # 10000-01-01 00:00 UTC, in microseconds
    path = tmp_path / "t.parquet"
    pq.write_table(
        pa.table(
            {
                "tz": pa.array([past_9999, 0, None], pa.timestamp("us", tz="UTC")),
                "naive": pa.array([2**63 - 1, None, None], pa.timestamp("us")),  # 'infinity'
                "day": pa.array([3_000_000, 0, None], pa.date32()),
                "ok": pa.array([dt.date(2025, 1, 15), None, None], pa.date32()),
            }
        ),
        path,
        row_group_size=1,
    )
    columns = {c.name: c for c in inspect_parquet(path).columns}
    for name in ("tz", "naive", "day"):
        assert (columns[name].min, columns[name].max) == (None, None), name
    assert [columns[n].null_count for n in ("tz", "naive", "day", "ok")] == [1, 2, 1, 2]
    assert (columns["ok"].min, columns["ok"].max) == ("2025-01-15", "2025-01-15")
