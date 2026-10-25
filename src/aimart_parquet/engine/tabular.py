"""Read rows back out of Parquet for an LLM: previews and sandboxed read-only SQL.

The SQL path runs caller-written queries, so it gets the strictest connection in the codebase:
no filesystem access at all (data arrives only as registered Arrow datasets), exactly one
statement which must be a SELECT, a row cap, and a watchdog that interrupts DuckDB at the
deadline. Results are rendered compactly — JSON rows, a Markdown table, or CSV — with long
cells truncated, because every byte returned is a token the agent pays to read.

Memory: DuckDB's ``memory_limit`` covers only its own buffer manager, never the Arrow batches it
hands back, the Python rows ``render`` builds, or a preview's Parquet decode. So both paths
decode in bounded batches, cut long text (top-level or nested in lists, structs, maps and
unions) before it is kept, inside DuckDB for queries, and refuse a result over
``MAX_RESULT_BYTES`` with a typed error instead of growing until the process dies. Preview reads
dictionary-encoded text as dictionaries, so a value that every row points at is decoded once.
Still unbounded here: DuckDB's Arrow scan of a query's input (a dictionary-bomb Parquet file
is decoded a whole row group at a time once registered), and single pages that inflate to
gigabytes; only the job's memory cap in the isolated worker stops those.
"""

from __future__ import annotations

import csv
import datetime as dt
import decimal
import io
import json
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as pads
import pyarrow.parquet as pq
from duckdb.sqltypes import DuckDBPyType

from aimart_parquet.engine.duck import connect, ident, literal
from aimart_parquet.engine.inspect import type_name
from aimart_parquet.engine.models import TabularResult
from aimart_parquet.errors import (
    InputTooLarge,
    InvalidInput,
    InvalidRequest,
    QueryFailed,
    QueryRejected,
    Timeout,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable

OutputFormat = Literal["json", "markdown", "csv"]
TABLE_NAME_PATTERN = r"^[a-z_][a-z0-9_]{0,62}$"
_MAX_SAFE_INT = 2**53
_PATH_IN_MESSAGE = re.compile(r'"/[^"]*"')
# A preview or query result larger than this (after long cells are cut) is refused, unpaid.
# 1000 rows of 30 full-length text columns still fit; nothing that big is readable by an agent.
MAX_RESULT_BYTES = 64 * 1024 * 1024
# Preview decodes about this many bytes per batch, sized per row group from the footer.
# pads.dataset().take() decoded whole row groups: a 258-byte file (20,000 rows, a dictionary of
# one 64 KB string) took 2.3 GB to preview one row. Fixed 64-row batches did not help a 776-byte
# file of 128 rows pointing at one 16 MB string (1.4 GB), and made a deep offset into a 40M-row
# group take 625,000 Python iterations. Text with a dictionary page is now read as a dictionary,
# each value decoded once, so rows are cheap and batches can be large (see _rows_from).
_PREVIEW_BATCH_BYTES = 4 * 1024 * 1024
_PREVIEW_MAX_BATCH_ROWS = 65_536
# With text or binary columns at most this many: the footer's sizes are the writer's word (a
# file can understate them), and a dictionary that grows (see _rows_from) is caught early.
_PREVIEW_TEXT_BATCH_ROWS = 4096
# Reading a row group again as plain text (after its dictionary grew, see _rows_from) may decode
# each dictionary-encoded row as its longest value; a preview that would decode more than this
# is refused instead of burning minutes of CPU (a 4 MB value x 8 columns x 20,000 rows is 640 GB).
_PREVIEW_REREAD_BYTES = 1024 * 1024 * 1024
# Rows per Arrow batch pulled from a query result; the byte cap is checked between batches.
_QUERY_BATCH_ROWS = 256
# Types nested deeper than this, or whose cut would need more SQL than this, are cut at the top
# level only inside DuckDB; the byte cap still holds.
_MAX_CUT_DEPTH = 16
_MAX_CUT_SQL = 256 * 1024


def _cell(value: Any, max_chars: int) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, decimal.Decimal):
        # sum() over BIGINT is HUGEINT -> decimal128(38, 0): agents expect a JSON number.
        if value == value.to_integral_value():
            as_int = int(value)
            return (
                as_int if abs(as_int) <= _MAX_SAFE_INT else str(value)
            )  # beyond 2^53 JS loses digits
        return float(value)
    if isinstance(value, bytes):
        return value.hex()[:max_chars]
    if isinstance(value, (list, tuple)):
        return [_cell(v, max_chars) for v in value]
    if isinstance(value, dict):
        return {str(k): _cell(v, max_chars) for k, v in value.items()}
    text = str(value)
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _text_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _is_text(kind: pa.DataType) -> bool:
    return bool(pa.types.is_string(kind) or pa.types.is_large_string(kind))


def _is_bytes(kind: pa.DataType) -> bool:
    return bool(pa.types.is_binary(kind) or pa.types.is_large_binary(kind))


def _needs_cut(kind: pa.DataType) -> bool:
    """Whether ``_cut`` has anything to do for this type: text, binary or a dictionary inside."""
    if pa.types.is_dictionary(kind) or _is_text(kind) or _is_bytes(kind):
        return True
    return any(_needs_cut(kind.field(i).type) for i in range(kind.num_fields))


def _cut(arr: pa.Array, want: pa.DataType, max_chars: int) -> pa.Array:
    """``arr`` as type ``want``, with text and binary values cut to what ``_cell`` can show.

    Recurses into lists, structs and maps. ``want`` differs from ``arr.type`` only where preview
    read text as a dictionary; those values come back as ``want``'s plain or dictionary type.
    """
    kind = arr.type
    if not _needs_cut(kind):
        return arr if kind == want else arr.cast(want)
    if pa.types.is_dictionary(kind):
        return _cut_dictionary(arr, want, max_chars)
    if _is_text(kind):
        arr = pc.utf8_slice_codeunits(arr, 0, max_chars + 1)  # code points, like len()
    elif _is_bytes(kind):
        arr = pc.binary_slice(arr, 0, (max_chars + 1) // 2)  # _cell shows it as hex
    elif pa.types.is_map(kind):
        offsets, entries = _rebased(arr)
        return pa.MapArray.from_arrays(
            offsets,
            _cut(entries.field(0), want.key_type, max_chars),
            _cut(entries.field(1), want.item_type, max_chars),
            type=want,
            mask=arr.is_null() if arr.null_count else None,
        )
    elif pa.types.is_list(kind) or pa.types.is_large_list(kind):
        offsets, values = _rebased(arr)
        return type(arr).from_arrays(
            offsets,
            _cut(values, want.value_type, max_chars),
            type=want,
            mask=arr.is_null() if arr.null_count else None,
        )
    elif pa.types.is_fixed_size_list(kind):
        size = kind.list_size
        values = arr.values.slice(arr.offset * size, len(arr) * size)
        return pa.FixedSizeListArray.from_arrays(
            _cut(values, want.value_type, max_chars),
            type=want,
            mask=arr.is_null() if arr.null_count else None,
        )
    elif pa.types.is_struct(kind):
        children = [_cut(arr.field(i), want.field(i).type, max_chars) for i in range(len(want))]
        return pa.StructArray.from_arrays(
            children,
            fields=list(want),
            mask=arr.is_null() if arr.null_count else None,
        )
    return arr if arr.type == want else arr.cast(want)


def _rebased(arr: pa.ListArray | pa.LargeListArray | pa.MapArray) -> tuple[pa.Array, pa.Array]:
    """(offsets from 0, the child values those offsets cover) of a possibly sliced list array."""
    offsets = arr.offsets  # .values ignores the slice; .offsets does not
    start, end = offsets[0].as_py(), offsets[-1].as_py()
    if start:
        offsets = pc.subtract(offsets, pa.scalar(start, offsets.type))
    return offsets, arr.values.slice(start, end - start)


def _cut_dictionary(arr: pa.DictionaryArray, want: pa.DataType, max_chars: int) -> pa.Array:
    """Only the dictionary values these rows use, cut; as ``want`` (a dictionary or its values).

    A dictionary column (pandas 'category', Polars Categorical) hands every batch the whole
    row-group dictionary: counted once per batch, 200,000 categories (8.8 MB) refused a 9.6 MB
    file with 413 at limit=500. Decoding instead would copy a 16 MB value once per row.
    """
    indices = arr.indices
    used = pc.unique(indices.drop_null() if indices.null_count else indices)
    value_type = want.value_type if pa.types.is_dictionary(want) else want
    values = _cut(arr.dictionary.take(used), value_type, max_chars)
    positions = pc.index_in(indices, value_set=used)  # a null index stays null
    if pa.types.is_dictionary(want):
        return pa.DictionaryArray.from_arrays(
            positions.cast(want.index_type), values, ordered=want.ordered
        )
    return values.take(positions)


def _collect(
    batches: Iterable[pa.RecordBatch],
    rows: int,
    max_chars: int,
    schema: pa.Schema | None = None,
) -> list[pa.RecordBatch]:
    """Keep up to ``rows`` rows, cut (as ``schema`` if given), and refuse over MAX_RESULT_BYTES."""
    kept: list[pa.RecordBatch] = []
    seen = size = 0
    if rows <= 0:
        return kept
    for batch in batches:
        part = batch.slice(0, rows - seen)
        want = schema if schema is not None else part.schema
        clipped = pa.RecordBatch.from_arrays(
            [
                _cut(arr, field.type, max_chars)
                for arr, field in zip(part.columns, want, strict=True)
            ],
            schema=want,
        )
        size += clipped.nbytes
        if size > MAX_RESULT_BYTES:
            raise InputTooLarge(
                f"The result is over {MAX_RESULT_BYTES // (1024 * 1024)} MB after long cells "
                f"were cut to {max_chars} characters.",
                hint="Select fewer columns or shorter lists, or lower limit; you were not charged.",
                limit_bytes=MAX_RESULT_BYTES,
            )
        kept.append(clipped)
        seen += clipped.num_rows
        if seen >= rows:
            break
    return kept


def _python_values(column: pa.ChunkedArray, name: str) -> list[Any]:
    try:
        return column.to_pylist()  # type: ignore[no-any-return]
    except (ValueError, pa.ArrowException):
        # Nanosecond times that are not whole microseconds ('...00.000000001'::TIMESTAMP_NS),
        # and structs with repeated or empty field names (row('a', 1), or a Parquet file), have
        # no Python object; to_pylist() raised and the caller got a bare 500.
        pass
    try:
        return [v for chunk in column.chunks for v in _displayable(chunk).to_pylist()]
    except (ValueError, pa.ArrowException) as exc:
        raise InvalidInput(
            f"Column {name!r} of type {type_name(column.type)} cannot be shown.",
            hint="Cast it to text (CAST(x AS VARCHAR)) or leave it out; you were not charged.",
        ) from exc


def _displayable(arr: pa.Array) -> pa.Array:
    """``arr`` with nanosecond times as text and struct field names made unique, at any depth."""
    kind = arr.type
    mask = arr.is_null() if arr.null_count else None
    if pa.types.is_dictionary(kind):
        return _displayable(arr.dictionary_decode())
    if isinstance(arr, pa.ExtensionArray):
        return _displayable(arr.storage)
    if pa.types.is_timestamp(kind) and kind.unit == "ns":  # 'T', like datetime.isoformat()
        return pc.replace_substring(arr.cast(pa.string()), " ", "T", max_replacements=1)
    if pa.types.is_time64(kind) and kind.unit == "ns":
        return arr.cast(pa.string())
    if pa.types.is_duration(kind) and kind.unit == "ns":
        return pc.binary_join_element_wise(arr.cast(pa.string()), " ns", "")
    if pa.types.is_struct(kind):
        seen: set[str] = set()
        names = []
        for i in range(kind.num_fields):
            field = kind.field(i).name
            names.append(field if field and field not in seen else f"{field or 'field'}_{i + 1}")
            seen.add(names[-1])
        children = [_displayable(arr.field(i)) for i in range(kind.num_fields)]
        return pa.StructArray.from_arrays(children, names=names, mask=mask)
    if pa.types.is_map(kind):
        offsets, entries = _rebased(arr)
        keys, items = _displayable(entries.field(0)), _displayable(entries.field(1))
        return pa.MapArray.from_arrays(offsets, keys, items, mask=mask)
    if pa.types.is_list(kind) or pa.types.is_large_list(kind):
        offsets, values = _rebased(arr)
        return type(arr).from_arrays(offsets, _displayable(values), mask=mask)
    if pa.types.is_fixed_size_list(kind):
        size = kind.list_size
        values = arr.values.slice(arr.offset * size, len(arr) * size)
        return pa.FixedSizeListArray.from_arrays(_displayable(values), size, mask=mask)
    return arr


def render(
    table: pa.Table, *, fmt: OutputFormat, truncated: bool, max_chars: int, total: int | None = None
) -> TabularResult:
    columns = table.column_names
    # By position, never by name: every SELECT * join repeats names (id, name, id, name), and
    # to_pylist()'s dicts kept only the last of each, so values shifted under the wrong headers.
    values = [_python_values(table.column(i), columns[i]) for i in range(table.num_columns)]
    rows = [[_cell(v, max_chars) for v in row] for row in zip(*values, strict=True)]
    result = TabularResult(
        columns=columns,
        types=[type_name(f.type) for f in table.schema],
        row_count=len(rows),
        truncated=truncated,
        total_rows=total,
    )
    if fmt == "json":
        result.rows = rows
    elif fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows([[_text_cell(v) for v in r] for r in rows])
        result.text = buf.getvalue()
    else:

        def esc(v: Any) -> str:
            return _text_cell(v).replace("|", "\\|").replace("\n", " ")

        lines = [
            "| " + " | ".join(esc(c) for c in columns) + " |",
            "|" + "|".join("---" for _ in columns) + "|",
        ]
        lines += ["| " + " | ".join(esc(v) for v in r) + " |" for r in rows]
        result.text = "\n".join(lines)
    return result


def _pick_columns(names: list[str], wanted: list[str]) -> list[str]:
    """Each wanted name as the file spells it: an exact match, else the one differing by case.

    ``{n.lower(): n}`` kept the last of ``a``/``A``, so asking for ``a`` returned column ``A``.
    """
    exact = set(names)
    by_fold: dict[str, set[str]] = {}
    for name in names:
        by_fold.setdefault(name.lower(), set()).add(name)
    picked: list[str] = []
    unknown: list[str] = []
    for name in wanted:
        candidates = {name} if name in exact else by_fold.get(name.lower(), set())
        if not candidates:
            unknown.append(name)
        elif len(candidates) > 1:
            raise InvalidRequest(
                f"Column {name!r} matches {len(candidates)} columns that differ only by "
                "letter case.",
                hint="Spell the column exactly as inspect lists it.",
                columns=sorted(candidates),
            )
        else:
            picked.append(next(iter(candidates)))
    if unknown:
        raise InvalidRequest(
            f"Unknown columns: {unknown}.",
            hint="Call inspect to list columns.",
            columns=names[:200],
        )
    return picked


def preview(
    path: Path,
    *,
    columns: list[str] | None,
    offset: int,
    limit: int,
    fmt: OutputFormat,
    max_chars: int,
) -> TabularResult:
    try:
        pf = pq.ParquetFile(path)
        names = pf.schema_arrow.names
    except (pa.ArrowException, OSError) as exc:
        raise InvalidInput(
            f"Not a readable Parquet file: {exc}", hint="Convert the file first."
        ) from exc
    if columns:
        columns = _pick_columns(names, columns)
    total = pf.metadata.num_rows
    stop = min(offset + limit, total)
    rows = _rows_from(path, pf, offset, stop - offset, columns)
    try:
        schema = pf.read_row_groups([], columns=columns).schema  # the types the file declares
        batches = _collect(rows, stop - offset, max_chars, schema)
    except (pa.ArrowException, OSError) as exc:
        raise InvalidInput(
            f"The Parquet file is corrupt: {str(exc).splitlines()[0]}",
            hint="Re-create the file, or convert the original again.",
        ) from exc
    finally:
        rows.close()  # closes the reader it has open now, not whenever it is collected
    table = pa.Table.from_batches(batches, schema=schema)
    return render(table, fmt=fmt, truncated=stop < total, max_chars=max_chars, total=total)


def _leaf_types(kind: pa.DataType) -> list[pa.DataType]:
    """The Parquet leaf columns of one Arrow type, in file order."""
    if pa.types.is_dictionary(kind):
        return [kind]
    if isinstance(kind, pa.ExtensionType):
        return _leaf_types(kind.storage_type)
    if kind.num_fields == 0:
        return [kind]
    return [t for i in range(kind.num_fields) for t in _leaf_types(kind.field(i).type)]


def _leaves(pf: pq.ParquetFile, columns: list[str] | None) -> tuple[list[int], list[int]]:
    """(leaf columns read, those holding text or binary) for ``columns``, by leaf index."""
    schema = pf.schema_arrow
    leaves = [(field.name, t) for field in schema for t in _leaf_types(field.type)]
    if len(leaves) != pf.metadata.num_columns:  # never seen; then size batches for every column
        read = list(range(pf.metadata.num_columns))
        return read, [j for j in read if pf.schema.column(j).physical_type == "BYTE_ARRAY"]
    wanted = set(columns) if columns else None
    read = [j for j, (name, _) in enumerate(leaves) if wanted is None or name in wanted]
    return read, [j for j in read if _is_text(leaves[j][1]) or _is_bytes(leaves[j][1])]


def _row_cost(group: pq.RowGroupMetaData, read: list[int], dictionary: set[int]) -> float:
    """Bytes one decoded row of ``read`` takes, judged from the footer.

    Every leaf value costs ~16 bytes (value or index, levels, offsets), so a list of a million
    elements per row gets few rows per batch; a plain-encoded column costs what its pages hold.
    """
    rows = max(group.num_rows, 1)
    per_row = 0.0
    for j in read:
        chunk = group.column(j)
        per_row += 16 * chunk.num_values / rows
        if j not in dictionary:  # a dictionary page is decoded once, not once per row
            per_row += chunk.total_uncompressed_size / rows
    return per_row


def _dictionaries(arr: pa.Array) -> Generator[pa.DictionaryArray]:
    """The dictionary-encoded arrays in ``arr``, at any depth, in a fixed order."""
    kind = arr.type
    if pa.types.is_dictionary(kind):
        yield arr
    elif isinstance(arr, pa.ExtensionArray):
        yield from _dictionaries(arr.storage)
    elif pa.types.is_struct(kind):
        for i in range(kind.num_fields):
            yield from _dictionaries(arr.field(i))
    elif kind.num_fields:  # lists and maps (whose values are key/value structs)
        yield from _dictionaries(arr.values)


def _plain_cost(batch: pa.RecordBatch) -> float:
    """Bytes per row the dictionary values of ``batch`` take once decoded as plain values.

    The longest value bounds them: a dictionary holds its whole dictionary page from the first
    batch on, so this covers every dictionary-encoded row of the row group.
    """
    cost = 0.0
    for column in batch.columns:
        for arr in _dictionaries(column):
            if len(arr.dictionary):
                longest = pc.max(pc.binary_length(arr.dictionary)).as_py() or 0
                cost += longest * len(arr) / max(batch.num_rows, 1)
    return cost


def _rows_from(
    path: Path, pf: pq.ParquetFile, offset: int, rows: int, columns: list[str] | None
) -> Generator[pa.RecordBatch]:
    """``rows`` rows starting exactly at row ``offset``; row groups before it are never read.

    Text with a dictionary page is read as a dictionary, so a value that every row points at
    is decoded once. But a writer falls back to plain pages once the dictionary page is full
    (high-cardinality text: most real ids, names and free text), and read as a dictionary those
    plain values are collected into one dictionary of every value read so far: a 6 MB file took
    306 MB to preview at offset 290,000, against 16 MB read as plain text. So when a dictionary
    grows between batches, the row group is read again as plain text, in batches sized by its
    longest dictionary value, and rows are yielded from where the first read stopped.
    """
    meta = pf.metadata
    first = 0
    while first < meta.num_row_groups and offset >= meta.row_group(first).num_rows:
        offset -= meta.row_group(first).num_rows
        first += 1
    read, text = _leaves(pf, columns)
    for index in range(first, meta.num_row_groups):
        if rows <= 0:
            return
        group = meta.row_group(index)
        need = min(group.num_rows, offset + rows)  # rows of this row group to decode
        shared = {j for j in text if group.column(j).has_dictionary_page}
        done = offset  # rows of this row group skipped or yielded so far
        plain = 0.0  # extra bytes per row once shared values are read as plain text
        while done < need:
            per_row = max(_row_cost(group, read, shared) + plain, 1.0)
            size = max(1, min(need, _PREVIEW_MAX_BATCH_ROWS, int(_PREVIEW_BATCH_BYTES / per_row)))
            if text:
                size = min(size, _PREVIEW_TEXT_BATCH_ROWS)
            if plain and need * per_row > _PREVIEW_REREAD_BYTES:
                raise InputTooLarge(
                    f"Previewing {need} rows into row group {index} would decode over "
                    f"{_PREVIEW_REREAD_BYTES // 1024**3} GB of text.",
                    hint="Lower offset, or select fewer text columns; you were not charged.",
                    limit_bytes=_PREVIEW_REREAD_BYTES,
                )
            paths = [group.column(j).path_in_schema for j in sorted(shared)]
            with pq.ParquetFile(path, metadata=meta, read_dictionary=paths or None) as reader:
                start, grown, lengths = 0, False, None
                for batch in reader.iter_batches(size, row_groups=[index], columns=columns):
                    end = start + batch.num_rows
                    if end > done:
                        part = batch.slice(done - start, min(end, need) - done)
                        done += part.num_rows
                        rows -= part.num_rows
                        yield part
                    if done >= need:
                        break
                    if shared:
                        now = [len(d.dictionary) for c in batch.columns for d in _dictionaries(c)]
                        grown = lengths is not None and now != lengths
                        lengths = now
                        if grown:
                            plain, shared = _plain_cost(batch), set()
                            break
                    start = end
            if not grown:
                break
        offset = 0


def validate_select(sql: str) -> str:
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise QueryRejected(
            "The SQL is empty.", hint="Send one SELECT statement, e.g. SELECT * FROM data LIMIT 10."
        )
    try:
        statements = duckdb.extract_statements(text)
    except duckdb.Error as exc:
        raise QueryRejected(
            f"SQL syntax error: {str(exc).splitlines()[0]}", hint="Fix the syntax and retry."
        ) from exc
    if len(statements) != 1:
        raise QueryRejected(
            f"Send exactly one statement; got {len(statements)}.",
            hint="Combine logic with CTEs (WITH ...).",
        )
    if statements[0].type != duckdb.StatementType.SELECT:
        raise QueryRejected(
            f"Only read-only SELECT queries are allowed; got {statements[0].type.name}.",
            hint="Use SELECT (CTEs, DESCRIBE and SUMMARIZE are fine). The data cannot be modified.",
        )
    return text


def _register(con: duckdb.DuckDBPyConnection, name: str, path: Path) -> None:
    """Expose one input as table ``name``, or say exactly why it cannot be (never a bare 500)."""
    try:
        dataset = pads.dataset(path, format="parquet")
    except (pa.ArrowException, OSError) as exc:  # its message names the server path
        raise InvalidInput(
            f"Table {name!r} is not a readable Parquet file.",
            hint="Convert it first (/v1/convert or the convert tool) and pass the "
            "returned artifact_id.",
        ) from exc
    try:
        con.register(name, dataset)
    except (duckdb.Error, pa.ArrowException) as exc:
        # A valid Parquet file can hold types DuckDB cannot scan (float16, decimal256): it raised
        # here, outside every handler, as a bare 500, or as "Retry" from the isolated worker.
        bad = _unreadable_columns(con, dataset.schema)
        raise InvalidInput(
            f"Table {name!r} has columns of a type SQL queries cannot read: "
            + (", ".join(bad) if bad else _PATH_IN_MESSAGE.sub('"<file>"', str(exc)[:200]))
            + ".",
            hint="Cast them to a common type (float16 to float32, decimal256 to decimal128 or "
            "string) and send the file again; you were not charged.",
        ) from exc


def _unreadable_columns(con: duckdb.DuckDBPyConnection, schema: pa.Schema) -> list[str]:
    bad = []
    for field in schema:
        try:
            con.register("aimart_type_probe", pa.schema([field]).empty_table())
        except (duckdb.Error, pa.ArrowException):
            bad.append(f"{field.name} ({type_name(field.type)})")
        finally:
            con.unregister("aimart_type_probe")
    return bad


def query(
    tables: dict[str, Path],
    sql: str,
    *,
    limit: int,
    fmt: OutputFormat,
    query_timeout: float,
    max_chars: int,
    memory_limit: str,
    threads: int,
) -> TabularResult:
    text = validate_select(sql)
    # A private, throwaway spill directory: big sorts and joins can spill instead of failing,
    # and the only path current_setting() can reveal is this random temp dir, not the server's.
    spill = Path(tempfile.mkdtemp(prefix="aimart-q-"))
    con = connect(
        memory_limit=memory_limit, threads=threads, temp_dir=spill
    )  # no other file access
    timer = threading.Timer(query_timeout, con.interrupt)
    try:
        for name, path in tables.items():
            _register(con, name, path)
        timer.start()
        try:
            reader = con.execute(_clipped(con, text, max_chars)).to_arrow_reader(
                min(limit + 1, _QUERY_BATCH_ROWS)
            )
            batches = _collect(reader, limit + 1, max_chars)
            table = pa.Table.from_batches(batches, schema=reader.schema)
        except duckdb.InterruptException as exc:
            raise Timeout(
                f"The query did not finish within {query_timeout:g} seconds.",
                hint="Add filters or aggregations, or reduce joins.",
            ) from exc
        except duckdb.Error as exc:
            # Never echo server paths back to the caller.
            message = _PATH_IN_MESSAGE.sub('"<file>"', str(exc).splitlines()[0])
            raise QueryFailed(
                f"Query failed: {message}",
                hint=f"Available tables: {', '.join(ident(t) for t in tables)}. Use DESCRIBE <table> to list columns.",
            ) from exc
    finally:
        timer.cancel()
        con.close()
        shutil.rmtree(spill, ignore_errors=True)
    truncated = table.num_rows > limit
    return render(table.slice(0, limit), fmt=fmt, truncated=truncated, max_chars=max_chars)


def _clipped(con: duckdb.DuckDBPyConnection, text: str, max_chars: int) -> str:
    """The caller's SELECT with long text and BLOB values cut inside DuckDB, at any depth.

    ``SELECT repeat('x', 1000000) FROM range(1000)`` reached 2.3 GB of RSS: DuckDB built each
    result chunk, then Arrow copied it, all outside memory_limit. Cut by ``left()`` in the same
    plan it stays under 100 MB. The same value in a list, struct, map or union reached 1.2 GB
    until those were cut too. Columns are picked by position (``#n``) and renamed back, so
    repeated names (``SELECT *`` over a join) survive the subquery unchanged. If the cut cannot
    be bound, or changes a column's type, fewer columns are cut (top-level only), then none.
    """
    # Binds the statement without running it. Typed as never None, but DuckDB returns None for
    # statements that are not queries; validate_select makes that unexpected, not impossible.
    rel: duckdb.DuckDBPyRelation | None = con.sql(text)
    if rel is None:
        return text
    for depth in (_MAX_CUT_DEPTH, 0):
        exprs, cut = [], False
        for i, (name, kind) in enumerate(zip(rel.columns, rel.types, strict=True), start=1):
            expr = _cut_sql(f"#{i}", kind, max_chars, 0, depth)
            cut = cut or expr is not None
            exprs.append(f"{expr or f'#{i}'} AS {ident(name)}")
        if not cut:
            return text
        # The newlines keep a trailing -- comment in the caller's SQL from eating the ")".
        wrapped = f"SELECT {', '.join(exprs)} FROM (\n{text}\n) AS q"
        if depth and len(wrapped) > len(text) + _MAX_CUT_SQL:  # unions in unions repeat paths
            continue
        try:
            validate_select(wrapped)
            bound = con.sql(wrapped)
        except (QueryRejected, duckdb.Error):
            continue  # never fail a valid query over our wrapper; the byte cap still holds
        if bound.columns == rel.columns and all(
            str(a) == str(b) or (str(b), str(a)) == ("JSON", "VARCHAR")  # same Arrow string
            for a, b in zip(bound.types, rel.types, strict=True)
        ):
            return wrapped
    return text


def _cut_sql(expr: str, kind: DuckDBPyType, max_chars: int, level: int, depth: int) -> str | None:
    """A DuckDB expression for ``expr`` (of type ``kind``) with its text cut, or None if no cut.

    Nested values are rebuilt with their exact type: lists by ``list_transform``, structs by
    ``struct_pack`` (NULL stays NULL), map values (never keys: cut keys can collide, which
    DuckDB refuses), union members, fixed-size arrays cast back to their size. Each part is cut
    where it lies (``struct_extract(#1, 'a')``), never through a copy: binding a struct to a
    lambda once (``list_transform([#1], lambda p: ...)[1]``) copied every value, and a struct of
    one 1 MB string still took 1 GB for 1000 rows. JSON is cut only at the top level, where
    DuckDB hands Arrow the same string either way.
    """
    kid = kind.id
    if kid == "varchar":
        return f"left({expr}, {max_chars + 1})" if level == 0 or str(kind) == "VARCHAR" else None
    if kid == "blob":
        return f"{expr}[1:{(max_chars + 1) // 2}]"
    if level >= depth or kid not in {"list", "array", "map", "struct", "union"}:
        return None
    item = f"aimart_v{level}"
    children = [(name, t) for name, t in kind.children if isinstance(t, DuckDBPyType)]
    if kid in {"list", "array"}:
        inner = _cut_sql(item, children[0][1], max_chars, level + 1, depth)
        if inner is None:
            return None
        listed = f"list_transform({expr}, lambda {item}: {inner})"
        return listed if kid == "list" else f"CAST({listed} AS {kind})"
    if kid == "map":  # map(NULL, NULL) is NULL
        inner = _cut_sql(item, children[1][1], max_chars, level + 1, depth)
        if inner is None:
            return None
        return f"map(map_keys({expr}), list_transform(map_values({expr}), lambda {item}: {inner}))"
    if kid == "struct":
        if any(not name for name, _ in children):  # unnamed fields (row(...)) cannot be packed
            return None
        parts, any_cut = [], False
        for name, child in children:
            field = f"struct_extract({expr}, {literal(name)})"
            inner = _cut_sql(field, child, max_chars, level + 1, depth)
            any_cut = any_cut or inner is not None
            parts.append(f"{ident(name)} := {inner or field}")
        if not any_cut:
            return None
        return f"CASE WHEN {expr} IS NULL THEN NULL ELSE struct_pack({', '.join(parts)}) END"
    branches = []  # a union; children[0] is its tag
    for tag, member in children[1:]:
        inner = _cut_sql(
            f"union_extract({expr}, {literal(tag)})", member, max_chars, level + 1, depth
        )
        if inner is not None:
            value = f"union_value({ident(tag)} := {inner})"
            branches.append(f"WHEN {literal(tag)} THEN CAST({value} AS {kind})")
    if not branches:
        return None
    return f"CASE union_tag({expr}) {' '.join(branches)} ELSE {expr} END"
