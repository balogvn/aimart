"""Load numeric matrices from Parquet for the quantum tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as pads
import pyarrow.parquet as pq

from aimart_parquet.errors import InvalidInput, InvalidRequest

if TYPE_CHECKING:
    from pathlib import Path


def _is_numeric(t: pa.DataType) -> bool:
    return bool(pa.types.is_integer(t) or pa.types.is_floating(t) or pa.types.is_decimal(t))


def _is_temporal(t: pa.DataType) -> bool:
    return bool(pa.types.is_date(t) or pa.types.is_timestamp(t))


def open_schema(path: Path) -> pa.Schema:
    try:
        return pq.ParquetFile(path).schema_arrow
    except (pa.ArrowInvalid, OSError) as exc:
        raise InvalidInput(
            f"Not a readable Parquet file: {exc}",
            hint="Run convert_to_parquet first and pass its artifact_id.",
        ) from exc


def pick_columns(schema: pa.Schema, requested: list[str] | None, *, what: str) -> list[str]:
    numeric = [f.name for f in schema if _is_numeric(f.type)]
    if requested is None:
        if not numeric:
            raise InvalidInput(
                "The file has no numeric columns.",
                hint="Convert with type inference on, or pass expected_schema to cast columns to numbers.",
            )
        return numeric
    lowered = {f.name.lower(): f for f in schema}
    chosen: list[str] = []
    for name in requested:
        field = lowered.get(name.lower())
        if field is None or not _is_numeric(field.type):
            raise InvalidRequest(
                f"{what} {name!r} is not a numeric column of this file.",
                hint="Call inspect_parquet to list columns and types.",
                numeric_columns=numeric[:100],
            )
        chosen.append(field.name)
    return list(dict.fromkeys(chosen))


def first_temporal(schema: pa.Schema) -> str | None:
    return next((f.name for f in schema if _is_temporal(f.type)), None)


def load_matrix(
    path: Path,
    columns: list[str],
    *,
    extra: list[str] | None = None,
    max_rows: int | None = None,
    sort_by: str | None = None,
) -> tuple[np.ndarray, pa.Table | None, int]:
    """(rows x columns float64 matrix without null rows, extra columns for kept rows, rows dropped)."""
    wanted = list(dict.fromkeys([*columns, *(extra or []), *([sort_by] if sort_by else [])]))
    dataset = pads.dataset(path)
    table = dataset.head(max_rows, columns=wanted) if max_rows else dataset.to_table(columns=wanted)
    if sort_by:
        table = table.sort_by(sort_by)
    matrix = (
        np.column_stack(
            [pc.cast(table.column(c), pa.float64()).to_numpy(zero_copy_only=False) for c in columns]
        )
        if columns
        else np.empty((table.num_rows, 0))
    )
    keep = np.isfinite(matrix).all(axis=1)
    dropped = int((~keep).sum())
    kept_extra = table.select(extra).filter(pa.array(keep)) if extra else None
    return matrix[keep], kept_extra, dropped
