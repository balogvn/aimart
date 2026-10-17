"""Read rows back out of Parquet for an LLM: previews and sandboxed read-only SQL.

The SQL path runs caller-written queries, so it gets the strictest connection in the codebase:
no filesystem access at all (data arrives only as registered Arrow datasets), exactly one
statement which must be a SELECT, a row cap, and a watchdog that interrupts DuckDB at the
deadline. Results are rendered compactly — JSON rows, a Markdown table, or CSV — with long
cells truncated, because every byte returned is a token the agent pays to read.
"""

from __future__ import annotations

import csv
import datetime as dt
import decimal
import io
import json
import re
import threading
from typing import TYPE_CHECKING, Any, Literal

import duckdb
import pyarrow as pa
import pyarrow.dataset as pads
import pyarrow.parquet as pq

from aimart_parquet.engine.duck import connect, ident
from aimart_parquet.engine.inspect import type_name
from aimart_parquet.engine.models import TabularResult
from aimart_parquet.errors import InvalidInput, InvalidRequest, QueryFailed, QueryRejected, Timeout

if TYPE_CHECKING:
    from pathlib import Path

OutputFormat = Literal["json", "markdown", "csv"]
TABLE_NAME_PATTERN = r"^[a-z_][a-z0-9_]{0,62}$"
_PATH_IN_MESSAGE = re.compile(r'"/[^"]*"')


def _cell(value: Any, max_chars: int) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
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


def render(
    table: pa.Table, *, fmt: OutputFormat, truncated: bool, max_chars: int, total: int | None = None
) -> TabularResult:
    columns = table.column_names
    rows = [[_cell(v, max_chars) for v in row.values()] for row in table.to_pylist()]
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
    except (pa.ArrowInvalid, OSError) as exc:
        raise InvalidInput(
            f"Not a readable Parquet file: {exc}", hint="Convert the file first."
        ) from exc
    names = pf.schema_arrow.names
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
    indices = (
        pa.array(range(offset, stop), type=pa.int64())
        if stop > offset
        else pa.array([], pa.int64())
    )
    # take() decodes only the row groups that hold these indices, so a deep offset stays cheap.
    table = pads.dataset(path).take(indices, columns=columns)
    truncated = stop < total
    return render(table, fmt=fmt, truncated=truncated, max_chars=max_chars, total=total)


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
    con = connect(memory_limit=memory_limit, threads=threads)  # no allowed dirs: zero file access
    timer = threading.Timer(query_timeout, con.interrupt)
    try:
        for name, path in tables.items():
            con.register(name, pads.dataset(path))
        timer.start()
        try:
            reader = con.execute(text).to_arrow_reader(min(limit + 1, 10_000))
            batches: list[pa.RecordBatch] = []
            seen = 0
            for batch in reader:
                batches.append(batch)
                seen += batch.num_rows
                if seen > limit:
                    break
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
    truncated = table.num_rows > limit
    return render(table.slice(0, limit), fmt=fmt, truncated=truncated, max_chars=max_chars)
