"""Format readers. Each one loads its input into a DuckDB table and describes its columns.

The contract keeps every format on one downstream path: whatever the source, the pipeline gets
a table name plus a ``RawColumn`` per column saying where the header came from, whether its
values are text still waiting for type inference, and any labels the source format carried.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    import duckdb

    from aimart_parquet.engine.limits import Limits
    from aimart_parquet.engine.models import ConvertOptions, InputFormat


@dataclass
class RawColumn:
    source: str  # column name inside the raw DuckDB table
    original: str  # header as it appeared in the input
    infer: bool = False  # text values that should climb the type ladder
    text: bool = False  # string column (trim, empty -> null) even when not inferred
    label: str | None = None
    value_labels: dict[str, str] | None = None


@dataclass
class RawTable:
    table: str
    columns: list[RawColumn]
    source_format: InputFormat
    encoding: str | None = None
    delimiter: str | None = None
    sheet: str | None = None
    available_sheets: list[str] | None = None
    prefer_decimal_comma: bool = False
    text_bytes: int | None = None
    warnings: list[str] = field(default_factory=list)


class Reader(Protocol):
    def __call__(
        self,
        con: duckdb.DuckDBPyConnection,
        path: Path,
        workdir: Path,
        options: ConvertOptions,
        fmt: InputFormat,
        limits: Limits,
    ) -> RawTable: ...


RAW_TABLE = "raw"
