"""SPSS (.sav/.zsav/.por), Stata (.dta) and SAS (.sas7bdat/.xpt) via ReadStat.

These formats carry what CSV exports throw away: variable labels ("Q12: How satisfied...") and
value labels (1 = "Male", 2 = "Female"). Preserving them is the point of supporting the format,
so they travel into Parquet field metadata (``label``, ``value_labels``), and on request into
the data itself (``value_labels='apply'`` or ``'both'``).

Rows are read in chunks and turned into Arrow arrays immediately, so peak Python memory tracks
the chunk, not the file: ReadStat's dict output is one Python object per cell (~30x the bytes).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.compute as pc
import pyreadstat

from aimart_parquet.engine.duck import ident
from aimart_parquet.engine.models import InputFormat, ValueLabelMode
from aimart_parquet.engine.readers import RAW_TABLE, RawColumn, RawTable
from aimart_parquet.errors import InvalidInput

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import duckdb

    from aimart_parquet.engine.limits import Limits
    from aimart_parquet.engine.models import ConvertOptions

_CHUNK_ROWS = 100_000

_READERS: dict[InputFormat, Callable[..., Any]] = {
    InputFormat.SAV: pyreadstat.read_sav,
    InputFormat.ZSAV: pyreadstat.read_sav,
    InputFormat.POR: pyreadstat.read_por,
    InputFormat.DTA: pyreadstat.read_dta,
    InputFormat.SAS7BDAT: pyreadstat.read_sas7bdat,
    InputFormat.XPT: pyreadstat.read_xport,
}


def _code_key(code: Any) -> str:
    if isinstance(code, float) and code.is_integer():
        return str(int(code))
    return str(code)


def _to_arrow(values: list[Any]) -> pa.Array:
    try:
        return pa.array(values)
    except (pa.ArrowInvalid, pa.ArrowTypeError):
        return pa.array([None if v is None else str(v) for v in values], type=pa.string())


def _concat(chunks: list[pa.Array]) -> pa.ChunkedArray:
    """Join per-chunk arrays whose inferred types may differ (an all-null chunk is null-typed)."""
    types = {c.type for c in chunks if not pa.types.is_null(c.type)}
    if not types:
        return (
            pa.chunked_array(chunks, type=pa.string())
            if chunks
            else pa.chunked_array([], pa.string())
        )
    if len(types) > 1:
        target: pa.DataType = pa.string()
    else:
        target = types.pop()
    fixed = [
        pa.nulls(len(c), target)
        if pa.types.is_null(c.type)
        else (c if c.type == target else pc.cast(c, target))
        for c in chunks
    ]
    return pa.chunked_array(fixed, type=target)


def _maybe_int(arr: pa.ChunkedArray) -> pa.ChunkedArray:
    """SPSS/Stata/SAS store every number as a double; give integral columns an integer type."""
    if not pa.types.is_floating(arr.type) or arr.null_count == len(arr):
        return arr
    valid = pc.drop_null(arr)
    if not pc.all(pc.equal(valid, pc.floor(valid))).as_py():
        return arr
    if pc.max(pc.abs(valid)).as_py() >= 2**53:
        return arr
    return pc.cast(arr, pa.int64())


def _label_lookup(
    arr: pa.ChunkedArray, mapping: dict[str, str]
) -> tuple[pa.ChunkedArray, pa.ChunkedArray]:
    """(codes as text, label text or null) for every value."""
    as_text = pc.cast(arr, pa.string())
    keys = pa.array(list(mapping.keys()), type=pa.string())
    labels = pa.array(list(mapping.values()), type=pa.string())
    return as_text, pc.take(labels, pc.index_in(as_text, value_set=keys))


def read_statistical(
    con: duckdb.DuckDBPyConnection,
    path: Path,
    workdir: Path,
    options: ConvertOptions,
    fmt: InputFormat,
    limits: Limits,
) -> RawTable:
    reader = _READERS[fmt]
    kwargs: dict[str, Any] = {"output_format": "dict"}
    if options.encoding and fmt is not InputFormat.XPT:
        kwargs["encoding"] = options.encoding
    chunks: dict[str, list[pa.Array]] = {}
    meta: Any = None
    offset = 0
    try:
        while True:
            data, meta = reader(str(path), row_offset=offset, row_limit=_CHUNK_ROWS, **kwargs)
            n = len(next(iter(data.values()), []))
            for name, values in data.items():
                chunks.setdefault(name, []).append(_to_arrow(values))
            del data
            offset += n
            if n < _CHUNK_ROWS:
                break
    except (pyreadstat.ReadstatError, pyreadstat.PyreadstatError, ValueError, OverflowError) as exc:
        raise InvalidInput(
            f"Could not read the {fmt.value} file: {exc}",
            hint="Check the file is not truncated; pass options.encoding for garbled text.",
        ) from exc
    names: list[str] = list(meta.column_names) if meta is not None else []
    if not names:
        raise InvalidInput("The file has no variables.", hint="Send a file with data.")
    labels: dict[str, str | None] = dict(meta.column_names_to_labels or {})
    value_labels: dict[str, dict[Any, str]] = dict(meta.variable_value_labels or {})

    arrays: dict[str, pa.ChunkedArray] = {}
    columns: list[RawColumn] = []
    mode = options.value_labels
    for i, name in enumerate(names):
        arr = _maybe_int(_concat(chunks.pop(name, [])))
        mapping = {_code_key(k): str(v) for k, v in value_labels.get(name, {}).items()}
        label = labels.get(name) or None
        source = f"c{i}"
        if mapping and mode is not ValueLabelMode.METADATA:
            as_text, looked_up = _label_lookup(arr, mapping)
            if mode is ValueLabelMode.APPLY:
                arr = pc.coalesce(looked_up, as_text)  # unlabelled codes keep their code
        arrays[source] = arr
        columns.append(
            RawColumn(
                source=source,
                original=name,
                text=pa.types.is_string(arr.type),
                label=label,
                value_labels=mapping or None,
            )
        )
        if mapping and mode is ValueLabelMode.BOTH:
            label_source = f"c{i}_label"
            arrays[label_source] = looked_up
            columns.append(
                RawColumn(source=label_source, original=f"{name}_label", text=True, label=label)
            )
    con.register("raw_arrow", pa.table(arrays))
    con.execute(f"CREATE TABLE {ident(RAW_TABLE)} AS SELECT * FROM raw_arrow")
    con.unregister("raw_arrow")
    encoding = getattr(meta, "file_encoding", None)
    return RawTable(
        table=RAW_TABLE,
        columns=columns,
        source_format=fmt,
        encoding=str(encoding) if encoding else None,
    )


def labels_json(value_labels: dict[str, str]) -> str:
    return json.dumps(value_labels, ensure_ascii=False, sort_keys=True)
