"""Parquet in, Parquet out: re-normalize names, re-type string columns, re-validate schemas."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq

from aimart_parquet.engine.duck import ident
from aimart_parquet.engine.models import InputFormat
from aimart_parquet.engine.readers import RAW_TABLE, RawColumn, RawTable
from aimart_parquet.errors import InvalidInput

if TYPE_CHECKING:
    from pathlib import Path

    import duckdb

    from aimart_parquet.engine.limits import Limits
    from aimart_parquet.engine.models import ConvertOptions


def read_parquet_input(
    con: duckdb.DuckDBPyConnection,
    path: Path,
    workdir: Path,
    options: ConvertOptions,
    fmt: InputFormat,
    limits: Limits,
) -> RawTable:
    try:
        table = pq.read_table(path)
    except (pa.ArrowInvalid, OSError) as exc:
        raise InvalidInput(
            f"Not a readable Parquet file: {exc}", hint="Re-export the file."
        ) from exc
    columns: list[RawColumn] = []
    renamed: dict[str, pa.ChunkedArray] = {}
    for i, field in enumerate(table.schema):
        source = f"c{i}"
        renamed[source] = table.column(i)
        meta = {k.decode(): v.decode() for k, v in (field.metadata or {}).items()}
        is_text = pa.types.is_string(field.type) or pa.types.is_large_string(field.type)
        columns.append(
            RawColumn(
                source=source,
                original=field.name,
                infer=is_text,
                text=is_text,
                label=meta.get("label"),
            )
        )
    con.register("raw_arrow", pa.table(renamed))
    con.execute(f"CREATE TABLE {ident(RAW_TABLE)} AS SELECT * FROM raw_arrow")
    con.unregister("raw_arrow")
    return RawTable(table=RAW_TABLE, columns=columns, source_format=InputFormat.PARQUET)
