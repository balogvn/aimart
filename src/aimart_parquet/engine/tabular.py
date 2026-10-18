"""Read rows back out of Parquet for an LLM: previews and sandboxed read-only SQL.

The SQL path runs caller-written queries, so it gets the strictest connection in the codebase:
no filesystem access at all (data arrives only as registered Arrow datasets), exactly one
statement which must be a SELECT, a row cap, and a watchdog that interrupts DuckDB at the
deadline. Results are rendered compactly — JSON rows, a Markdown table, or CSV — with long
cells truncated, because every byte returned is a token the agent pays to read.

Memory: DuckDB's ``memory_limit`` covers only its own buffer manager, never the Arrow batches it
hands back, the Python rows ``render`` builds, or a preview's Parquet decode. So both paths
decode in small batches, cut long text before it is kept (inside DuckDB for queries), and refuse
a result over ``MAX_RESULT_BYTES`` with a typed error instead of growing until the process dies.
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

from aimart_parquet.engine.duck import connect, ident
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
    from collections.abc import Iterable

OutputFormat = Literal["json", "markdown", "csv"]
TABLE_NAME_PATTERN = r"^[a-z_][a-z0-9_]{0,62}$"
_MAX_SAFE_INT = 2**53
_PATH_IN_MESSAGE = re.compile(r'"/[^"]*"')
# A preview or query result larger than this (after long cells are cut) is refused, unpaid.
# 1000 rows of 30 full-length text columns still fit; nothing that big is readable by an agent.
MAX_RESULT_BYTES = 64 * 1024 * 1024
# Rows decoded per Parquet read in preview. pads.dataset().take() decoded whole row groups before
# any row cap: a 258-byte file (one row group of 20,000 rows, its dictionary one 64 KB string)
# took 2.3 GB of RAM to preview one row. Reading this many rows at a time keeps it under 100 MB.
_PREVIEW_BATCH_ROWS = 64
# Rows per Arrow batch pulled from a query result; the byte cap is checked between batches.
_QUERY_BATCH_ROWS = 256


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


def _clip(batch: pa.RecordBatch, max_chars: int) -> pa.RecordBatch:
    """Cut top-level text and binary cells to what ``_cell`` can show, before they are kept."""
    arrays = []
    for arr in batch.columns:
        values = arr.dictionary if isinstance(arr, pa.DictionaryArray) else arr
        kind = values.type
        if pa.types.is_string(kind) or pa.types.is_large_string(kind):
            values = pc.utf8_slice_codeunits(values, 0, max_chars + 1)  # code points, like len()
        elif pa.types.is_binary(kind) or pa.types.is_large_binary(kind):
            values = pc.binary_slice(values, 0, (max_chars + 1) // 2)  # _cell shows it as hex
        if isinstance(arr, pa.DictionaryArray):
            values = pa.DictionaryArray.from_arrays(arr.indices, values, ordered=arr.type.ordered)
        arrays.append(values)
    return pa.RecordBatch.from_arrays(arrays, schema=batch.schema)


def _collect(batches: Iterable[pa.RecordBatch], rows: int, max_chars: int) -> list[pa.RecordBatch]:
    """Keep up to ``rows`` rows, clipped, and refuse a result over MAX_RESULT_BYTES."""
    kept: list[pa.RecordBatch] = []
    seen = size = 0
    if rows <= 0:
        return kept
    for batch in batches:
        clipped = _clip(batch.slice(0, rows - seen), max_chars)
        size += clipped.nbytes
        if size > MAX_RESULT_BYTES:
            raise InputTooLarge(
                f"The result is over {MAX_RESULT_BYTES // (1024 * 1024)} MB after long cells "
                f"were cut to {max_chars} characters.",
                hint="Select fewer or narrower columns (nested values are not cut), or lower "
                "limit; you were not charged.",
                limit_bytes=MAX_RESULT_BYTES,
            )
        kept.append(clipped)
        seen += clipped.num_rows
        if seen >= rows:
            break
    return kept


def render(
    table: pa.Table, *, fmt: OutputFormat, truncated: bool, max_chars: int, total: int | None = None
) -> TabularResult:
    columns = table.column_names
    # By position, never by name: every SELECT * join repeats names (id, name, id, name), and
    # to_pylist()'s dicts kept only the last of each, so values shifted under the wrong headers.
    values = [table.column(i).to_pylist() for i in range(table.num_columns)]
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
        lowered = {n.lower(): n for n in names}
        unknown = [c for c in columns if c.lower() not in lowered]
        if unknown:
            raise InvalidRequest(
                f"Unknown columns: {unknown}.",
                hint="Call inspect to list columns.",
                columns=names[:200],
            )
        columns = [lowered[c.lower()] for c in columns]
    total = pf.metadata.num_rows
    stop = min(offset + limit, total)
    try:
        schema = pf.read_row_groups([], columns=columns).schema
        batches = _collect(_rows_from(pf, offset, columns), stop - offset, max_chars)
    except (pa.ArrowException, OSError) as exc:
        raise InvalidInput(
            f"The Parquet file is corrupt: {str(exc).splitlines()[0]}",
            hint="Re-create the file, or convert the original again.",
        ) from exc
    table = pa.Table.from_batches(batches, schema=schema)
    return render(table, fmt=fmt, truncated=stop < total, max_chars=max_chars, total=total)


def _rows_from(
    pf: pq.ParquetFile, offset: int, columns: list[str] | None
) -> Iterable[pa.RecordBatch]:
    """Batches starting exactly at row ``offset``; row groups before it are never read."""
    meta = pf.metadata
    first = 0
    while first < meta.num_row_groups and offset >= meta.row_group(first).num_rows:
        offset -= meta.row_group(first).num_rows
        first += 1
    groups = range(first, meta.num_row_groups)
    for batch in pf.iter_batches(_PREVIEW_BATCH_ROWS, row_groups=groups, columns=columns):
        if offset >= batch.num_rows:  # rows before the offset in its own row group
            offset -= batch.num_rows
            continue
        yield batch.slice(offset)
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
            try:
                dataset = pads.dataset(path, format="parquet")
            except (pa.ArrowException, OSError) as exc:  # its message names the server path
                raise InvalidInput(
                    f"Table {name!r} is not a readable Parquet file.",
                    hint="Convert it first (/v1/convert or the convert tool) and pass the "
                    "returned artifact_id.",
                ) from exc
            con.register(name, dataset)
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
    """The caller's SELECT with long text and BLOB cells cut inside DuckDB.

    ``SELECT repeat('x', 1000000) FROM range(1000)`` reached 2.3 GB of RSS: DuckDB built each
    result chunk, then Arrow copied it, all outside memory_limit. Cut by ``left()`` in the same
    plan it stays under 100 MB. Columns are picked by position (``#n``) and renamed back, so
    repeated names (``SELECT *`` over a join) survive the subquery unchanged.
    """
    # Binds the statement without running it. Typed as never None, but DuckDB returns None for
    # statements that are not queries; validate_select makes that unexpected, not impossible.
    rel: duckdb.DuckDBPyRelation | None = con.sql(text)
    if rel is None:
        return text
    exprs, cut = [], False
    for i, (name, kind) in enumerate(zip(rel.columns, rel.types, strict=True), start=1):
        expr = f"#{i}"
        if kind.id == "varchar":
            expr, cut = f"left({expr}, {max_chars + 1})", True
        elif kind.id == "blob":
            expr, cut = f"{expr}[1:{(max_chars + 1) // 2}]", True
        exprs.append(f"{expr} AS {ident(name)}")
    if not cut:
        return text
    # The newlines keep a trailing -- comment in the caller's SQL from eating the ")".
    wrapped = f"SELECT {', '.join(exprs)} FROM (\n{text}\n) AS q"
    try:
        validate_select(wrapped)
        con.sql(wrapped)
    except (QueryRejected, duckdb.Error):
        return text  # never fail a valid query over our wrapper; the byte cap still holds
    return wrapped
