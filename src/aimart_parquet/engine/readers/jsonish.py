"""JSON and NDJSON: DuckDB for well-formed input, Python only when it has to think.

Fast path: a top-level array or newline-delimited records go straight to DuckDB's
``read_json``, which unifies heterogeneous records and infers nested STRUCT/LIST types without
building Python objects. Malformed NDJSON lines are skipped and counted.

Slow path, only when needed: the records live inside an envelope (``{"data": {"items": [...]}}``)
or the document is broken (trailing commas, single quotes, comments). We parse in Python —
repairing with ``json_repair`` up to a size cap, because it is pure Python and slow — locate the
record array, and re-emit it as NDJSON for DuckDB.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from json_repair import repair_json

from aimart_parquet.engine.duck import ident, literal
from aimart_parquet.engine.models import InputFormat
from aimart_parquet.engine.readers import RAW_TABLE, RawColumn, RawTable
from aimart_parquet.engine.text import to_utf8
from aimart_parquet.errors import InputTooLarge, InvalidInput, InvalidRequest

if TYPE_CHECKING:
    from pathlib import Path

    import duckdb

    from aimart_parquet.engine.limits import Limits
    from aimart_parquet.engine.models import ConvertOptions

_MAX_OBJECT = 64 * 1024 * 1024
_SEARCH_DEPTH = 4
# json.loads needs ~10x the text size in RAM; above this, insist on a streamable layout.
_MAX_PYTHON_PARSE = 100 * 1024 * 1024


def _read_json_sql(path: Path, fmt: str) -> str:
    return (
        f"read_json({literal(str(path))}, format={literal(fmt)}, sample_size=-1, "
        f"maximum_object_size={_MAX_OBJECT}, ignore_errors=true, maximum_depth=-1)"
    )


def _first_char(path: Path) -> str:
    with path.open("rb") as fh:
        head = fh.read(4096).lstrip(b"\xef\xbb\xbf \t\r\n")
    return head[:1].decode("ascii", errors="replace")


def _find_records(doc: Any, path: str | None) -> tuple[list[Any], str | None]:
    """Return the record list and the dot path it was found at."""
    if path:
        node: Any = doc
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                raise InvalidRequest(
                    f"json_records_path {path!r} does not exist in the document (failed at {part!r}).",
                    hint="Omit json_records_path to auto-detect the record array.",
                )
        if not isinstance(node, list):
            raise InvalidRequest(
                f"json_records_path {path!r} points at a {type(node).__name__}, not an array.",
                hint="Point it at the array of records.",
            )
        return node, path
    if isinstance(doc, list):
        return doc, None
    best: tuple[int, list[Any], str] | None = None

    def visit(node: Any, trail: list[str], depth: int) -> None:
        nonlocal best
        if depth > _SEARCH_DEPTH or not isinstance(node, dict):
            return
        for key, value in node.items():
            here = [*trail, str(key)]
            if (
                isinstance(value, list)
                and value
                and sum(isinstance(v, dict) for v in value) > len(value) / 2
            ):
                if best is None or len(value) > best[0]:
                    best = (len(value), value, ".".join(here))
            elif isinstance(value, dict):
                visit(value, here, depth + 1)

    visit(doc, [], 0)
    if best is not None:
        return best[1], best[2]
    return [doc], None  # a single object is a one-row table


def _load_python(path: Path, limits: Limits, warnings: list[str]) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except RecursionError as exc:
        raise InvalidInput(
            "The JSON is nested too deeply to parse.", hint="Flatten the document before sending."
        ) from exc
    except json.JSONDecodeError as first_error:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if len(lines) > 1:
            parsed, bad = [], 0
            for line in lines:
                try:
                    parsed.append(json.loads(line))
                except json.JSONDecodeError:
                    bad += 1
            if parsed and bad <= max(1, len(lines) // 10):
                if bad:
                    warnings.append(f"skipped {bad} malformed JSON lines")
                return parsed
        if len(text.encode()) > limits.max_json_repair_bytes:
            raise InvalidInput(
                f"Invalid JSON at line {first_error.lineno}, column {first_error.colno}: "
                f"{first_error.msg}.",
                hint=(
                    f"Documents over {limits.max_json_repair_bytes // (1024 * 1024)} MB are not "
                    "auto-repaired; fix the JSON or send NDJSON."
                ),
            ) from first_error
        repaired = repair_json(text, return_objects=True)
        if repaired in ("", None, [], {}):
            raise InvalidInput(
                f"Invalid JSON at line {first_error.lineno}, column {first_error.colno}: "
                f"{first_error.msg}, and it could not be repaired.",
                hint="Check the file is JSON, or pass options.format.",
            ) from first_error
        warnings.append(f"repaired malformed JSON ({first_error.msg} at line {first_error.lineno})")
        return repaired


def _looks_like_ndjson(path: Path) -> bool:
    """The first two non-empty lines are each a complete JSON object."""
    seen = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            if len(stripped) > _MAX_OBJECT:
                return False
            try:
                if not isinstance(json.loads(stripped), dict):
                    return False
            except RecursionError as exc:
                raise InvalidInput(
                    "The JSON is nested too deeply to parse.",
                    hint="Flatten the document before sending.",
                ) from exc
            except json.JSONDecodeError:
                return seen >= 1  # one good line then garbage: still NDJSON, with bad lines
            seen += 1
            if seen == 2:
                return True
    return False


def _drop_null_rows(con: duckdb.DuckDBPyConnection, table: str) -> int:
    """ignore_errors turns a malformed line into an all-NULL row; remove those rows."""
    cols = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description or []]
    if not cols:
        return 0
    cond = " AND ".join(f"{ident(c)} IS NULL" for c in cols)
    before = con.execute(f"SELECT count(*) FROM {table}").fetchone()
    con.execute(f"DELETE FROM {table} WHERE {cond}")
    after = con.execute(f"SELECT count(*) FROM {table}").fetchone()
    return (int(before[0]) if before else 0) - (int(after[0]) if after else 0)


def _has_structure(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    """False when DuckDB found no records or no keys (it then emits one opaque ``json`` column).

    Those inputs — ``[]``, ``[{}, {}]``, ``[1, 2, 3]`` — go to the Python path, which either
    rejects them clearly or wraps scalars as ``{"value": ...}``. Accepting them here wrote a
    junk ``json`` column and charged for it.
    """
    row = con.execute(f"SELECT count(*) FROM {table}").fetchone()
    if not row or int(row[0]) == 0:
        con.execute(f"DROP TABLE IF EXISTS {table}")
        return False
    rel = con.sql(f"SELECT * FROM {table}")
    opaque = str(rel.types[0]).startswith(("JSON", "MAP(")) if rel.types else False
    if (
        len(rel.columns) == 1 and rel.columns[0] == "json" and opaque
    ):  # [{}, {}] is MAP(VARCHAR, JSON)
        con.execute(f"DROP TABLE IF EXISTS {table}")
        return False
    return True


def _count_lines(path: Path) -> int:
    with path.open("rb") as fh:
        return sum(1 for line in fh if line.strip())


def read_json(
    con: duckdb.DuckDBPyConnection,
    path: Path,
    workdir: Path,
    options: ConvertOptions,
    fmt: InputFormat,
    limits: Limits,
) -> RawTable:
    info = to_utf8(path, workdir, options.encoding)
    warnings: list[str] = []
    first = _first_char(info.path)
    table = ident(RAW_TABLE)
    loaded = False
    if not options.json_records_path:
        duck_fmt = None
        if first == "[":
            duck_fmt = "array"
        elif first == "{" and _looks_like_ndjson(info.path):
            duck_fmt = "newline_delimited"
        if duck_fmt:
            try:
                con.execute(
                    f"CREATE TABLE {table} AS SELECT * FROM {_read_json_sql(info.path, duck_fmt)}"
                )
                loaded = True
            except Exception:  # noqa: BLE001 - any DuckDB failure falls back to the Python path
                con.execute(f"DROP TABLE IF EXISTS {table}")
            if loaded:
                loaded = _has_structure(con, table)
            if loaded and duck_fmt == "newline_delimited":
                skipped = _drop_null_rows(con, table)
                row = con.execute(f"SELECT count(*) FROM {table}").fetchone()
                skipped = max(skipped, _count_lines(info.path) - (int(row[0]) if row else 0))
                if skipped > 0:
                    warnings.append(f"skipped {skipped} malformed or empty JSON lines")
    if not loaded:
        text_bytes = info.path.stat().st_size
        if text_bytes > _MAX_PYTHON_PARSE:
            raise InputTooLarge(
                f"This JSON is a single {text_bytes // (1024 * 1024)} MB document; documents over "
                f"{_MAX_PYTHON_PARSE // (1024 * 1024)} MB must be NDJSON or a top-level array.",
                hint="Re-export as newline-delimited JSON (one record per line).",
                limit_bytes=_MAX_PYTHON_PARSE,
            )
        doc = _load_python(info.path, limits, warnings)
        records, found_at = _find_records(doc, options.json_records_path)
        del doc
        if not records:
            raise InvalidInput("The JSON contains no records.", hint="Send at least one record.")
        if all(isinstance(r, dict) and not r for r in records):
            raise InvalidInput("The JSON records have no fields.", hint="Send objects with keys.")
        ndjson = workdir / "records.ndjson"
        with ndjson.open("w", encoding="utf-8") as out:
            for rec in records:
                out.write(json.dumps(rec if isinstance(rec, dict) else {"value": rec}))
                out.write("\n")
        del records
        con.execute(
            f"CREATE TABLE {table} AS SELECT * FROM {_read_json_sql(ndjson, 'newline_delimited')}"
        )
        if found_at:
            warnings.append(f"read records from JSON path '{found_at}'")
    columns = _describe(con, options.flatten_nested)
    if not columns:
        raise InvalidInput("The JSON records have no fields.", hint="Send objects with keys.")
    return RawTable(
        table=RAW_TABLE,
        columns=columns,
        source_format=InputFormat.NDJSON if fmt is InputFormat.NDJSON else InputFormat.JSON,
        encoding=info.encoding,
        text_bytes=info.path.stat().st_size,
        warnings=warnings,
    )


def _describe(con: duckdb.DuckDBPyConnection, flatten: bool) -> list[RawColumn]:
    rel = con.sql(f"SELECT * FROM {ident(RAW_TABLE)}")
    columns: list[RawColumn] = []
    exprs: list[str] = []
    for name, dtype in zip(rel.columns, rel.types, strict=True):
        for col_name, expr, kind in _expand(ident(name), name, dtype, flatten):
            alias = f"c{len(exprs)}"
            exprs.append(f"{expr} AS {ident(alias)}")
            columns.append(
                RawColumn(
                    source=alias, original=col_name, infer=kind == "text", text=kind == "text"
                )
            )
    con.execute(f"CREATE TABLE raw_flat AS SELECT {', '.join(exprs)} FROM {ident(RAW_TABLE)}")
    con.execute(f"DROP TABLE {ident(RAW_TABLE)}")
    con.execute(f"ALTER TABLE raw_flat RENAME TO {ident(RAW_TABLE)}")
    return columns


def _expand(expr: str, name: str, dtype: Any, flatten: bool) -> list[tuple[str, str, str]]:
    """(output name, SQL expression, kind) for a column, recursing into structs if flattening."""
    type_id = dtype.id
    if str(dtype) == "JSON":  # JSON is a VARCHAR alias: its id says "varchar"
        # Mixed scalars ([1, "two", 3]) arrive as JSON; unwrap so strings lose their quotes,
        # then let the type ladder decide. Objects and arrays keep their JSON text.
        return [(name, f"json_extract_string({expr}, '$')", "text")]
    if type_id == "varchar":
        return [(name, expr, "text")]
    if flatten and type_id == "struct":
        out: list[tuple[str, str, str]] = []
        for child_name, child_type in dtype.children:
            child_expr = f"struct_extract({expr}, {literal(child_name)})"
            out.extend(_expand(child_expr, f"{name}__{child_name}", child_type, flatten))
        return out
    if flatten and type_id in {"list", "map", "array", "struct", "union"}:
        return [(name, f"CAST(to_json({expr}) AS VARCHAR)", "json")]
    return [(name, expr, "typed")]
