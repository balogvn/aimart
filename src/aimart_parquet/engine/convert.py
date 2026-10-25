"""The conversion pipeline: bytes in, Parquet plus an honest report out.

    unwrap compression -> detect format -> read into DuckDB -> name columns -> infer types
    -> validate against expected_schema -> sort -> stream to Parquet -> report from the footer

The report is built from the file that was actually written (types and null counts come from
the Parquet footer), not from what the pipeline intended to write.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aimart_parquet import __version__
from aimart_parquet.engine import safety
from aimart_parquet.engine.detect import _zip_office_format, detect_format
from aimart_parquet.engine.duck import connect, ident
from aimart_parquet.engine.infer import (
    INSTANT_TYPE,
    NAIVE_TIME_TYPES,
    ColumnPlan,
    TypeInferrer,
    naive_as_utc_note,
    offset_to_utc_note,
)
from aimart_parquet.engine.inspect import inspect_parquet
from aimart_parquet.engine.models import (
    ColumnReport,
    ConversionReport,
    ConvertOptions,
    ExpectedColumn,
    InputFormat,
)
from aimart_parquet.engine.names import make_unique
from aimart_parquet.engine.readers.columnar import read_parquet_input
from aimart_parquet.engine.readers.delimited import read_delimited
from aimart_parquet.engine.readers.excel import read_excel
from aimart_parquet.engine.readers.jsonish import read_json
from aimart_parquet.engine.readers.pdf import read_pdf
from aimart_parquet.engine.readers.statistical import labels_json, read_statistical
from aimart_parquet.engine.writer import copy_to_parquet, write_parquet
from aimart_parquet.errors import InvalidRequest, SchemaViolation

if TYPE_CHECKING:
    from pathlib import Path

    import duckdb

    from aimart_parquet.engine.limits import Limits
    from aimart_parquet.engine.readers import RawColumn, RawTable, Reader

READERS: dict[InputFormat, Reader] = {
    InputFormat.CSV: read_delimited,
    InputFormat.TSV: read_delimited,
    InputFormat.JSON: read_json,
    InputFormat.NDJSON: read_json,
    InputFormat.XLSX: read_excel,
    InputFormat.XLS: read_excel,
    InputFormat.ODS: read_excel,
    InputFormat.SAV: read_statistical,
    InputFormat.ZSAV: read_statistical,
    InputFormat.POR: read_statistical,
    InputFormat.DTA: read_statistical,
    InputFormat.SAS7BDAT: read_statistical,
    InputFormat.XPT: read_statistical,
    InputFormat.PARQUET: read_parquet_input,
    InputFormat.PDF: read_pdf,
}

_MAX_COLUMNS_META = 256 * 1024  # file-level label map; huge survey files keep field metadata only

SQL_TYPES = {
    "string": "VARCHAR",
    "int64": "BIGINT",
    "float64": "DOUBLE",
    "bool": "BOOLEAN",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "timestamptz": "TIMESTAMPTZ",
}


@dataclass
class _Output:
    name: str
    raw: RawColumn | None
    plan: ColumnPlan
    notes: list[str]
    extra_rejects: int = 0


def unwrap(
    src: Path, filename: str | None, workdir: Path, limits: Limits
) -> tuple[Path, str | None]:
    """Remove one layer of gzip/bz2/zip. Office documents are zips too and are left alone.

    The result always lives inside ``workdir``: DuckDB is sandboxed to that directory, so an
    input elsewhere (local mode passes user paths) is copied in first.
    """
    if not src.resolve().is_relative_to(workdir.resolve()):
        local = workdir / ("input" + "".join(src.suffixes[-2:]))
        safety.copy_file(src, local)
        src = local
    kind = safety.sniff_compression(safety.read_magic(src, 8))
    if kind in {"gzip", "bz2"}:
        dst = workdir / "decompressed"
        safety.decompress_stream(src, dst, kind, limits.max_decompressed_bytes)
        name = filename
        if name:
            for suffix in (".gz", ".gzip", ".bz2"):
                if name.lower().endswith(suffix):
                    name = name[: -len(suffix)]
        return dst, name
    if kind == "zip" and _zip_office_format(src) is None:
        return safety.extract_single_member(src, workdir, limits.max_decompressed_bytes)
    return src, filename


def _decimal(col: ExpectedColumn) -> tuple[int, int]:
    """(precision, scale) of an expected_schema decimal; an impossible one is the caller's error."""
    # Explicit None checks: scale 0 is a real request. `col.scale or 2` made DECIMAL(10, 0) a
    # DECIMAL(10, 2), so 1234567890 overflowed to NULL and strict failed on matching data.
    precision = 18 if col.precision is None else col.precision
    scale = 2 if col.scale is None else col.scale
    if scale > precision:
        default = " (the default)" if col.scale is None else ""
        raise InvalidRequest(
            f"expected_schema column {col.name!r}: decimal scale {scale}{default} exceeds "
            f"precision {precision}.",
            hint="Pass a scale no larger than precision, e.g. precision 10 with scale 2 "
            "(scale 0 for whole numbers).",
            column=col.name,
        )
    return precision, scale


def _sql_type(col: ExpectedColumn) -> str:
    """The DuckDB type for an expected_schema column."""
    if col.type != "decimal":
        return SQL_TYPES[col.type]
    precision, scale = _decimal(col)
    return f"DECIMAL({precision}, {scale})"


def _unrounded(x: str, target: str) -> str:
    """A number cast to a decimal target, or NULL (a counted reject) where the cast rounds it.

    DuckDB rounds to the scale without a word: DECIMAL(10, 0) of 12.5 is 13. With scale 0
    honoured, strict passed and charged for 13. (Text is judged in TypeInferrer.cast_text.)
    """
    cast = f"TRY_CAST({x} AS {target})"
    return f"(CASE WHEN {cast} = {x} THEN {cast} END)"


def _check_expected_schema(options: ConvertOptions) -> None:
    """Refuse an impossible schema before reading the file, not after (DuckDB raises a 500)."""
    for col in options.expected_schema or ():
        _sql_type(col)


def _expected_expr(inferrer: TypeInferrer, out: _Output, col: ExpectedColumn) -> str:
    target = _sql_type(col)
    if out.plan.type_name == col.type:
        return out.plan.expr
    if out.plan.type_name == "string" and out.raw is not None:
        return inferrer.cast_text(out.raw.source, col.type, target)
    x = f"({out.plan.expr})"
    if col.type == "int64" and out.plan.type_name in {"float64", "decimal", "native"}:
        # DuckDB rounds 12.5 to 12 on cast; a fractional value must be a counted reject instead.
        return f"(CASE WHEN TRY_CAST({x} AS DOUBLE) = floor(TRY_CAST({x} AS DOUBLE)) THEN TRY_CAST({x} AS BIGINT) END)"
    if col.type == "decimal" and out.plan.type_name in {"int64", "float64"} and out.raw is not None:
        # A number inference read from text is read again from that text, where inference read
        # one: through the inferred DOUBLE, '1234567890123456.78' became 1234567890123456.80.
        # Resetting the column to its raw text instead made the 'NA' inference had nulled a
        # counted reject, and strict refused a column that fits.
        decimal = inferrer.cast_text(out.raw.source, col.type, target)
        return f"(CASE WHEN {x} IS NOT NULL THEN {decimal} END)"
    if col.type == "decimal" and inferrer.is_number(out.plan.expr):
        return _unrounded(x, target)
    return f"TRY_CAST({x} AS {target})"


def _zone_note(
    inferrer: TypeInferrer, out: _Output, type_name: str, expr: str, kept: int
) -> str | None:
    """What a cast of a typed column between naive times and instants assumed: UTC, both ways.

    An instant from a naive value assumes its zone, and a naive value from an instant is its
    UTC wall time. The cast of a text column says so (TypeInferrer.cast_note); the same cast of
    a column inference had typed said nothing, so the report depended on whether inference
    left the column as text, and native columns (Parquet, Excel, JSON) never said it.
    """
    source = inferrer.type_id(out.plan.expr)
    if not kept or not (
        (type_name == "timestamptz" and source in NAIVE_TIME_TYPES)
        or (type_name in {"date", "timestamp"} and source == INSTANT_TYPE)
    ):
        return None
    if out.plan.inferred and out.raw is not None:
        # Counted from the text: an inferred instant may hold naive values read as UTC.
        return inferrer.cast_note(out.raw.source, type_name, expr)
    note = naive_as_utc_note if type_name == "timestamptz" else offset_to_utc_note
    return note(kept, kept)


def _cast_rejects(con: duckdb.DuckDBPyConnection, table: str, before: str, after: str) -> list[str]:
    rows = con.sql(
        f"SELECT DISTINCT TRY_CAST(({before}) AS VARCHAR) AS x FROM {ident(table)} "
        f"WHERE ({before}) IS NOT NULL AND ({after}) IS NULL ORDER BY x LIMIT 3"
    ).fetchall()
    return [str(r[0])[:80] for r in rows if r[0] is not None]


def _apply_expected_schema(
    con: duckdb.DuckDBPyConnection,
    table: str,
    inferrer: TypeInferrer,
    outputs: list[_Output],
    options: ConvertOptions,
) -> list[_Output]:
    assert options.expected_schema is not None
    by_name = {o.name.lower(): o for o in outputs}
    expected_names = {c.name.lower() for c in options.expected_schema}
    violations: list[dict[str, object]] = []
    result: list[_Output] = []
    for col in options.expected_schema:
        out = by_name.get(col.name.lower())
        target = _sql_type(col)
        if out is None:
            if options.strict or not col.nullable:
                violations.append({"column": col.name, "problem": "missing"})
                continue
            plan = ColumnPlan(source="", expr=f"CAST(NULL AS {target})", type_name=col.type)
            result.append(_Output(col.name, None, plan, ["added as all-null: missing from input"]))
            continue
        typed = out.plan.type_name != "string"
        if col.type == "string" and typed and out.raw is not None and out.raw.text:
            # Declaring a text column string asks for its text. Casting the inferred value back
            # rewrote it ('1.50' -> '1.5', '15/01/2025' -> '2025-01-15', '1.234' -> '1234') and
            # lost what the 98% tolerance had nulled ('oops' -> null, charged under strict).
            out = _Output(out.name, out.raw, inferrer.text_plan(out.raw.source), [])
        expr = _expected_expr(inferrer, out, col)
        row = con.sql(
            f"SELECT count_if(({out.plan.expr}) IS NOT NULL AND ({expr}) IS NULL), "
            f"count_if(({expr}) IS NULL), count(({expr})) FROM {ident(table)}"
        ).fetchone()
        rejects, nulls, kept = (int(row[0]), int(row[1]), int(row[2])) if row else (0, 0, 0)
        # Values inference already nulled (the 98% tolerance) do not fit either. Counting only
        # this cast's rejects let strict=true pass, and charge, with 'oops' in an int64 column.
        misfits = out.plan.rejected_count + rejects
        if misfits and options.strict:
            violations.append(
                {"column": col.name, "problem": f"{misfits} values do not fit {col.type}"}
            )
        if nulls and not col.nullable:
            violations.append(
                {"column": col.name, "problem": f"{nulls} nulls in a non-nullable column"}
            )
        examples = out.plan.rejected_examples
        if rejects:
            # A count without the values ('3 rejected', examples []) leaves the caller unable to
            # see that '12,5' was the problem.
            examples = [*examples, *_cast_rejects(con, table, out.plan.expr, expr)][:3]
        plan = ColumnPlan(
            out.plan.source,
            expr,
            col.type,
            inferred=out.plan.inferred,
            rejected_count=out.plan.rejected_count,
            rejected_examples=examples,
            notes=out.plan.notes,
        )
        notes = (
            [*out.notes, f"cast to {col.type} per expected_schema"]
            if out.plan.type_name != col.type
            else out.notes
        )
        if out.plan.type_name == "string" and out.raw is not None:
            note = inferrer.cast_note(out.raw.source, col.type, expr)
        else:
            note = _zone_note(inferrer, out, col.type, expr, kept)
        notes = [*notes, note] if note else notes
        result.append(_Output(col.name, out.raw, plan, notes, extra_rejects=rejects))
    extras = [o for o in outputs if o.name.lower() not in expected_names]
    if extras and options.strict:
        violations.append(
            {"column": ", ".join(o.name for o in extras[:20]), "problem": "not in expected_schema"}
        )
    if violations:
        raise SchemaViolation(
            f"The data does not match expected_schema ({len(violations)} problems).",
            hint="Fix the listed columns, relax strict/nullable, or drop expected_schema.",
            violations=violations[:50],
        )
    return result + extras


def convert_file(
    src: Path,
    out_path: Path,
    *,
    filename: str | None,
    options: ConvertOptions,
    workdir: Path,
    limits: Limits,
) -> ConversionReport:
    started = time.monotonic()
    _check_expected_schema(options)
    source_bytes = safety.ensure_size(src, limits.max_input_bytes)
    path, name = unwrap(src, filename, workdir, limits)
    fmt = detect_format(path, safety.read_magic(path), name, options.format)
    tmp = workdir / "duck_tmp"
    tmp.mkdir(exist_ok=True)
    con = connect(
        memory_limit=limits.duckdb_memory_limit,
        threads=limits.duckdb_threads,
        allowed_dirs=[workdir],
        temp_dir=tmp,
    )
    try:
        raw = READERS[fmt](con, path, workdir, options, fmt, limits)
        report = _finish(con, raw, out_path, name, options, fmt)
    finally:
        con.close()
    report.source_bytes = source_bytes
    report.duration_ms = int((time.monotonic() - started) * 1000)
    text_bytes = raw.text_bytes if raw.text_bytes is not None else path.stat().st_size
    report.estimated_tokens_raw = max(1, text_bytes // 4)
    return report


def _finish(
    con: duckdb.DuckDBPyConnection,
    raw: RawTable,
    out_path: Path,
    filename: str | None,
    options: ConvertOptions,
    fmt: InputFormat,
) -> ConversionReport:
    inferrer = TypeInferrer(
        con,
        raw.table,
        extra_null_tokens=options.null_values,
        date_order=options.date_order,
        prefer_decimal_comma=raw.prefer_decimal_comma,
    )
    to_infer = [c.source for c in raw.columns if c.infer and options.infer_types]
    plans = inferrer.plan(to_infer) if to_infer else {}
    names = make_unique([c.original for c in raw.columns], normalize=options.normalize_column_names)
    outputs: list[_Output] = []
    for col, name in zip(raw.columns, names, strict=True):
        plan = plans.get(col.source) or (
            inferrer.text_plan(col.source)
            if col.text
            else ColumnPlan(col.source, ident(col.source), "native")
        )
        outputs.append(_Output(name, col, plan, list(plan.notes)))
    if options.expected_schema:
        outputs = _apply_expected_schema(con, raw.table, inferrer, outputs, options)

    order = ""
    if options.sort_by:
        known = {o.name.lower(): o.name for o in outputs}
        missing = [c for c in options.sort_by if c.lower() not in known]
        if missing:
            raise InvalidRequest(
                f"sort_by names unknown columns: {missing}.",
                hint="Use output column names (after normalization).",
                columns=[o.name for o in outputs][:100],
            )
        order = " ORDER BY " + ", ".join(
            f"{ident(known[c.lower()])} NULLS LAST" for c in options.sort_by
        )

    select_sql = (
        "SELECT "
        + ", ".join(f"{o.plan.expr} AS {ident(o.name)}" for o in outputs)
        + f" FROM {ident(raw.table)}{order}"
    )
    field_meta: dict[str, dict[str, str]] = {}
    columns_meta: dict[str, dict[str, object]] = {}
    for o in outputs:
        meta: dict[str, str] = {}
        if o.raw is not None and o.raw.original != o.name:
            meta["original_name"] = o.raw.original
        if o.raw is not None and o.raw.label:
            meta["label"] = o.raw.label
        if o.raw is not None and o.raw.value_labels:
            meta["value_labels"] = labels_json(o.raw.value_labels)
        if meta:
            field_meta[o.name] = meta
            columns_meta[o.name] = {
                k: (json.loads(v) if k == "value_labels" else v) for k, v in meta.items()
            }
    file_meta = {
        "aimart.provenance": json.dumps(
            {
                "tool": "aimart-parquet",
                "version": __version__,
                "source_format": fmt.value,
                "source_file": (filename or "")[:200],
                "converted_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
            },
            separators=(",", ":"),
        )
    }
    if columns_meta:
        encoded = json.dumps(columns_meta, separators=(",", ":"), ensure_ascii=False)
        if len(encoded) <= _MAX_COLUMNS_META:
            file_meta["aimart.columns"] = encoded
    if any(o.raw is not None and (o.raw.label or o.raw.value_labels) for o in outputs):
        write_parquet(
            con.sql(select_sql).to_arrow_reader(options.row_group_size),
            out_path,
            field_metadata=field_meta,
            file_metadata=file_meta,
            compression=options.compression,
            row_group_size=options.row_group_size,
        )
    else:
        copy_to_parquet(
            con,
            select_sql,
            out_path,
            file_metadata=file_meta,
            compression=options.compression,
            row_group_size=options.row_group_size,
        )

    summary = inspect_parquet(out_path)
    stats = {c.name: c for c in summary.columns}
    columns: list[ColumnReport] = []
    for o in outputs:
        st = stats[o.name]
        columns.append(
            ColumnReport(
                name=o.name,
                original_name=o.raw.original if o.raw is not None else o.name,
                type=st.type,
                nullable=st.nullable,
                null_count=st.null_count if st.null_count is not None else 0,
                inferred_from_text=o.plan.inferred,
                rejected_count=o.plan.rejected_count + o.extra_rejects,
                rejected_examples=o.plan.rejected_examples[:3],
                label=st.label,
                value_labels=st.value_labels,
                notes=o.notes,
            )
        )
    schema_digest = json.dumps(
        [{"n": c.name, "t": c.type, "nulls": c.null_count} for c in columns], separators=(",", ":")
    )
    return ConversionReport(
        source_format=raw.source_format,
        source_bytes=0,
        output_bytes=out_path.stat().st_size,
        rows=summary.rows,
        columns=columns,
        encoding=raw.encoding,
        delimiter=raw.delimiter,
        sheet=raw.sheet,
        available_sheets=raw.available_sheets,
        warnings=raw.warnings,
        duration_ms=0,
        estimated_tokens_raw=0,
        estimated_tokens_schema=max(1, len(schema_digest) // 4),
    )
