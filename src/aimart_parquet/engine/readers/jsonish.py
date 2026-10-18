"""JSON and NDJSON: DuckDB for well-formed input, Python only when it has to think.

Fast path: a top-level array or newline-delimited records go straight to DuckDB's
``read_json``, which unifies heterogeneous records and infers nested STRUCT/LIST types without
building Python objects. Malformed NDJSON lines are skipped and counted.

Slow path, only when needed: the records live inside an envelope (``{"data": {"items": [...]}}``)
or the document is broken (trailing commas, single quotes, comments). We parse in Python —
repairing with ``json_repair`` up to a size cap, because it is pure Python and slow — locate the
record array, and re-emit it as NDJSON for DuckDB.

Both paths load through ``_load``: a strict read first (clean input, one pass), else a lenient one
that skips malformed lines. DuckDB matches nested field names case-insensitively, so a lenient
read also keeps only the first of ``{"k": 1, "K": 2}``, and it cannot name an empty nested key at
all; when the data has such keys, they are renamed (``K_2``, ``unnamed``) in one streamed pass
and the renames are reported.
"""

from __future__ import annotations

import json
import string
from typing import TYPE_CHECKING, Any

import duckdb
from json_repair import repair_json

from aimart_parquet.engine.duck import ident, literal
from aimart_parquet.engine.models import InputFormat
from aimart_parquet.engine.readers import RAW_TABLE, RawColumn, RawTable
from aimart_parquet.engine.text import to_utf8
from aimart_parquet.errors import AimartError, InputTooLarge, InvalidInput, InvalidRequest

if TYPE_CHECKING:
    from pathlib import Path

    from aimart_parquet.engine.limits import Limits
    from aimart_parquet.engine.models import ConvertOptions

_MAX_OBJECT = 64 * 1024 * 1024
_SEARCH_DEPTH = 4
# json.loads needs ~10x the text size in RAM; above this, insist on a streamable layout.
_MAX_PYTHON_PARSE = 100 * 1024 * 1024
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_RENAMES_SHOWN = 5
_TOO_DEEP = "The JSON is nested too deeply to parse."
# DuckDB 1.5's errors for nested keys it cannot keep: 'Duplicate name "K" in struct ...' (strict
# mode only; lenient mode merges them) and 'A table cannot be created from an unnamed struct'
# (``{"a": {"": 1}}``, both modes). Matching them is a shortcut: the key-structure check in
# ``_load`` finds the same keys without it.
_UNKEEPABLE_KEY_ERRORS = ("Duplicate name", "unnamed struct")
_EMPTY_KEY_NAME = "unnamed"

# A path to an object inside a record: keys, with None for "an element of this list". DuckDB
# gives every element of a list, in every record, one STRUCT type, so they share a path.
_KeyPath = tuple[str | None, ...]


def _read_json_sql(path: Path, fmt: str, *, strict: bool = False) -> str:
    return (
        f"read_json({literal(str(path))}, format={literal(fmt)}, sample_size=-1, "
        f"maximum_object_size={_MAX_OBJECT}, ignore_errors={str(not strict).lower()}, "
        "maximum_depth=-1)"
    )


def _objects_sql(path: Path, fmt: str) -> str:
    """Each record as JSON text, no schema; a malformed NDJSON line is NULL."""
    return (
        f"read_json_objects({literal(str(path))}, format={literal(fmt)}, "
        f"maximum_object_size={_MAX_OBJECT}, ignore_errors=true)"
    )


def _fold(key: str) -> str:
    """The name DuckDB compares STRUCT fields by: ASCII letters only (``é`` and ``É`` differ)."""
    return key.translate(_ASCII_LOWER)


def _first_wins(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # DuckDB keeps the first of {"k": 1, "k": 9}; json.loads keeps the last. Renaming must not
    # change which one survives, or a value would depend on whether another key collided.
    out = dict(pairs)
    if len(out) < len(pairs):  # rare: rebuild, keeping each key's first value and position
        out = {}
        for key, value in pairs:
            out.setdefault(key, value)
    return out


def _collides(node: Any, *, nested: bool) -> bool:
    """True when a nested object (in ``json_group_structure`` output) has a key DuckDB loses.

    That is two keys that fold alike, or an empty one. Record keys (``nested=False``) are
    columns, which DuckDB keeps apart and names itself (``id``, ``ID_1``).
    """
    if isinstance(node, list):
        return any(_collides(v, nested=True) for v in node)
    if not isinstance(node, dict):
        return False
    if nested and ("" in node or len({_fold(k) for k in node}) < len(node)):
        return True
    return any(_collides(v, nested=True) for v in node.values())


class _KeyRenamer:
    """Gives nested keys DuckDB would merge (or cannot name) new names, the same in every record."""

    def __init__(self) -> None:
        self._names: dict[_KeyPath, dict[str, str]] = {}
        self._taken: dict[_KeyPath, set[str]] = {}
        self.renamed: list[str] = []

    def value(self, node: Any, path: _KeyPath = ()) -> Any:
        if isinstance(node, list):
            item = (*path, None)
            return [self.value(v, item) if isinstance(v, (dict, list)) else v for v in node]
        if not isinstance(node, dict):
            return node
        if not path:  # record keys become columns, which DuckDB keeps apart by itself
            return {
                key: self.value(v, (key,)) if isinstance(v, (dict, list)) else v
                for key, v in node.items()
            }
        names = self._names.setdefault(path, {})
        out: dict[str, Any] = {}
        for key, v in node.items():
            name = names.get(key)
            if name is None:
                name = self._name(path, key, node)
            out[name] = self.value(v, (*path, name)) if isinstance(v, (dict, list)) else v
        return out

    def _name(self, path: _KeyPath, key: str, siblings: dict[str, Any]) -> str:
        taken = self._taken.setdefault(path, set())
        name = key
        if not key or _fold(key) in taken:
            # Never take the name of a real key in this object, even one that comes later.
            others = {_fold(k) for k in siblings}
            base, n = key or _EMPTY_KEY_NAME, 2
            name = base
            while _fold(name) in taken or _fold(name) in others:
                name, n = f"{base}_{n}", n + 1
            where = "".join("[]" if p is None else f".{p}" for p in path).removeprefix(".")
            self.renamed.append(f"{where}.{key or '""'} is now {where}.{name}")
        self._names[path][key] = name
        taken.add(_fold(name))
        return name


def _structure_collides(structure: str) -> bool:
    """Whether a ``json_group_structure`` result has nested keys DuckDB would lose."""
    try:
        return _collides(json.loads(structure), nested=False)
    except RecursionError as exc:
        raise InvalidInput(_TOO_DEEP, hint="Flatten the document before sending.") from exc


def _renamed_record(text: str, renamer: _KeyRenamer) -> str:
    try:
        return json.dumps(renamer.value(json.loads(text, object_pairs_hook=_first_wins)))
    except RecursionError as exc:
        raise InvalidInput(_TOO_DEEP, hint="Flatten the document before sending.") from exc


def _rename_keys(con: duckdb.DuckDBPyConnection, path: Path, fmt: str, out: Path) -> list[str]:
    """Rewrite ``path`` as NDJSON with colliding nested keys renamed; malformed lines dropped."""
    renamer = _KeyRenamer()
    reader = con.execute(f"SELECT CAST(json AS VARCHAR) FROM {_objects_sql(path, fmt)}")
    with out.open("w", encoding="utf-8") as fh:
        for batch in reader.to_arrow_reader(1024):
            for text in batch.column(0).to_pylist():
                if text is not None:  # None is a malformed line, skipped and counted as before
                    fh.write(_renamed_record(text, renamer))
                    fh.write("\n")
    return renamer.renamed


def _load(con: duckdb.DuckDBPyConnection, path: Path, fmt: str, workdir: Path) -> list[str]:
    """Create RAW_TABLE from ``path`` without losing nested keys; returns warnings.

    DuckDB matches STRUCT field names case-insensitively: ``{"a": {"k": 1, "K": 2}}`` (HTTP
    header maps, mixed ``ID``/``id``) failed type detection with 'Duplicate name "K" in
    struct', and with ignore_errors=true the read kept ``k`` and dropped every ``K`` value
    without a warning, on every path; an empty nested key (``{"a": {"": 1}}``) failed the whole
    read with a bare error. So: strict first (clean input, one pass). If that fails, DuckDB's
    error or the data's key structure says whether there are such keys; only then are they
    renamed and the file read again.
    """
    table = ident(RAW_TABLE)
    try:
        con.execute(
            f"CREATE TABLE {table} AS SELECT * FROM {_read_json_sql(path, fmt, strict=True)}"
        )
    except duckdb.Error as exc:
        collides = any(e in str(exc) for e in _UNKEEPABLE_KEY_ERRORS)
        con.execute(f"DROP TABLE IF EXISTS {table}")
    else:
        return []
    if not collides:  # malformed lines (skipped below), which may hide colliding keys too
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM {_read_json_sql(path, fmt)}")
        row = con.execute(
            f"SELECT json_group_structure(json) FROM {_objects_sql(path, fmt)}"
        ).fetchone()
        if not row or row[0] is None or not _structure_collides(row[0]):
            return []
        con.execute(f"DROP TABLE {table}")
    renamed_path = workdir / "records_renamed.ndjson"
    renamed = _rename_keys(con, path, fmt, renamed_path)
    try:
        sql = _read_json_sql(renamed_path, "newline_delimited", strict=True)
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM {sql}")
    except duckdb.Error as exc:  # DuckDB folded a key we did not: refuse rather than drop data
        raise InvalidInput(
            f"Nested keys in this JSON could not be kept apart: {str(exc).splitlines()[0]}",
            hint="Rename nested keys so that none is empty and no two in one object differ "
            "only by letter case.",
        ) from exc
    if not renamed:
        return []
    shown = "; ".join(renamed[:_RENAMES_SHOWN])
    more = f"; and {len(renamed) - _RENAMES_SHOWN} more" if len(renamed) > _RENAMES_SHOWN else ""
    return [
        f"renamed {len(renamed)} nested key{'s' if len(renamed) > 1 else ''} whose values "
        "would otherwise have been lost "
        f"(nested field names are matched ignoring letter case, and cannot be empty): "
        f"{shown}{more}"
    ]


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
                warnings.extend(_load(con, info.path, duck_fmt, workdir))
                loaded = True
            except AimartError:
                raise
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
        warnings.extend(_load(con, ndjson, "newline_delimited", workdir))
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
