"""Describe a Parquet file from its footer alone — no row data is read.

This is the cheapest useful thing an agent can do with a dataset: learn the columns, types,
null counts, ranges and labels for a few hundred tokens instead of reading millions of rows.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.parquet as pq

from aimart_parquet.engine.models import ColumnStats, InspectResult
from aimart_parquet.errors import InvalidInput

if TYPE_CHECKING:
    from pathlib import Path

_MAX_VALUE_CHARS = 100
_MAX_KV_CHARS = 2000
_HIDDEN_KV = {"ARROW:schema", "pandas"}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()[:_MAX_VALUE_CHARS]
    text = str(value)
    return text if len(text) <= _MAX_VALUE_CHARS else text[: _MAX_VALUE_CHARS - 1] + "…"


def type_name(t: pa.DataType) -> str:
    return str(t).replace("large_string", "string")


def inspect_parquet(path: Path) -> InspectResult:
    try:
        pf = pq.ParquetFile(path)
    except (pa.ArrowInvalid, OSError) as exc:
        raise InvalidInput(
            f"Not a readable Parquet file: {exc}", hint="Convert the file first."
        ) from exc
    md = pf.metadata
    schema = pf.schema_arrow
    leaf_index = {md.schema.column(j).path: j for j in range(md.num_columns)}
    columns: list[ColumnStats] = []
    for field in schema:
        meta = {k.decode(): v.decode() for k, v in (field.metadata or {}).items()}
        value_labels = None
        if "value_labels" in meta:
            try:
                value_labels = json.loads(meta["value_labels"])
            except json.JSONDecodeError:
                value_labels = None
        stats = ColumnStats(
            name=field.name,
            type=type_name(field.type),
            nullable=field.nullable,
            label=meta.get("label"),
            value_labels=value_labels,
        )
        j = leaf_index.get(field.name)
        if j is not None:
            nulls, lo, hi, counted, ranged = 0, None, None, True, True
            for rg in range(md.num_row_groups):
                st = md.row_group(rg).column(j).statistics
                if st is None:
                    counted = ranged = False
                    break
                nulls += st.null_count if st.has_null_count else 0
                if st.has_min_max and ranged:
                    try:
                        lo = st.min if lo is None or st.min < lo else lo
                        hi = st.max if hi is None or st.max > hi else hi
                    except (TypeError, ValueError, OverflowError):
                        # Incomparable, or past what Python holds: an offset applied to a
                        # '9999-12-31 23:59:59-05:00' sentinel is in year 10000, and reading it
                        # as a datetime raised OverflowError, a bare 500 from convert. The
                        # range is unknown; the null count is still exact.
                        ranged = False
            if counted:
                stats.null_count = nulls
            if counted and ranged:
                stats.min = _jsonable(lo)
                stats.max = _jsonable(hi)
        columns.append(stats)
    kv = {
        k.decode(): v.decode(errors="replace")
        for k, v in (md.metadata or {}).items()
        if k.decode() not in _HIDDEN_KV
    }
    kv = {
        k: (v if len(v) <= _MAX_KV_CHARS else v[: _MAX_KV_CHARS - 1] + "…") for k, v in kv.items()
    }
    return InspectResult(
        rows=md.num_rows,
        row_groups=md.num_row_groups,
        columns=columns,
        size_bytes=path.stat().st_size,
        created_by=md.created_by,
        key_value_metadata=kv,
    )
