"""The conversion pipeline: bytes in, Parquet plus an honest report out.

    unwrap compression -> detect format -> read into DuckDB -> name columns -> infer types
    -> validate against expected_schema -> sort -> stream to Parquet -> report from the footer

(A gzip of CSV or TSV is not unwrapped: the delimited reader lets DuckDB read one of a single
member as it is, and unpacks any other. See keep_gzip.)

The report is built from the file that was actually written (types and null counts come from
the Parquet footer), not from what the pipeline intended to write.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from aimart_parquet import __version__
from aimart_parquet.engine import safety
from aimart_parquet.engine.detect import (
    START_BYTES,
    _zip_office_format,
    detect_format,
    format_from_start,
)
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
from aimart_parquet.errors import InputTooLarge, InvalidRequest, SchemaViolation

if TYPE_CHECKING:
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

# Delimited text is staged and then written by COPYs that keep input order, and DuckDB keeps
# that order by holding every batch a faster thread finished early. With 2 threads that held
# 400-650 MB for a 40-column CSV (spilled to the RAM disk, or out of memory, under a lower
# limit); with one it held about 100 MB. Production runs on 1 vCPU, where a second thread buys
# no CPU anyway. Other formats are read into memory before the write and keep the setting.
_ONE_THREAD = frozenset({InputFormat.CSV, InputFormat.TSV})

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

    The result is always a new file inside ``workdir``, never ``src``: DuckDB is sandboxed to
    that directory, and the result is the pipeline's own (a reader may replace it, and
    convert_file deletes it once read) while ``src`` is the caller's or the service's. An
    archive is unpacked from where it lies (Python reads it; only DuckDB is held to
    ``workdir``): every input outside the work directory used to be copied in first, and that
    copy (up to 50 MB) sat on Cloud Run's RAM disk for the whole job next to the text unpacked
    from it.
    """
    kind = safety.sniff_compression(safety.read_magic(src, 8))
    if kind in {"gzip", "bz2"}:
        dst = workdir / safety.DECOMPRESSED
        safety.decompress_stream(src, dst, kind, limits.max_decompressed_bytes)
        return dst, _without_compression(filename)
    if kind == "zip" and _zip_office_format(src) is None:
        return safety.extract_single_member(src, workdir, limits.max_decompressed_bytes)
    return _link(src, workdir, "input" + "".join(src.suffixes[-2:])), filename


def keep_gzip(
    src: Path, filename: str | None, workdir: Path, forced: InputFormat | None
) -> tuple[Path, str | None, InputFormat] | None:
    """A gzip of CSV or TSV, linked into ``workdir`` still compressed, with its name and format.

    DuckDB reads gzip, and the text unwrap unpacked sat on Cloud Run's RAM disk while it was
    read: up to the 256 MB stream cap of a job's files. The delimited reader checks the stream
    first, and unpacks it where DuckDB could read it differently. Anything else is None (unwrap
    it), and so is a gzip whose first KB does not settle its format: ``format_from_start``
    decides it, the one answer the checks before the quote use too.
    """
    if safety.sniff_compression(safety.read_magic(src, 8)) != "gzip":
        return None
    name = _without_compression(filename)
    head = safety.gzip_head(src, START_BYTES)
    if head is None:
        return None  # corrupt where it starts: unwrap says so, as before
    fmt = format_from_start(head, name, forced, whole=len(head) < START_BYTES)
    if fmt not in {InputFormat.CSV, InputFormat.TSV}:
        return None
    # No '.gz': the reader may unpack or transcode text over it (and it tells read_csv whether
    # it reads a gzip or text, never leaving that to the name).
    return _link(src, workdir, "input"), name, fmt


def _without_compression(filename: str | None) -> str | None:
    name = filename
    if name:
        for suffix in (".gz", ".gzip", ".bz2"):
            if name.lower().endswith(suffix):
                name = name[: -len(suffix)]
    return name


def _link(src: Path, workdir: Path, name: str) -> Path:
    """``src`` as ``workdir / name``, the pipeline's own file: a reader may replace it, and
    convert_file deletes it once read, leaving ``src`` as it was."""
    local = workdir / name
    if local.exists():  # src itself, or another file in the work directory
        local = Path(tempfile.mkdtemp(dir=workdir)) / name
    if src.resolve().is_relative_to(workdir.resolve()):
        # The service's own file (an upload, a download, a stored artifact's link): a hard link
        # costs no space, and deleting it, or renaming a UTF-8 version over it, leaves that
        # file as it was. Nothing may write into it.
        safety.link_or_copy(src, local)
    else:
        # Elsewhere is a local caller's own file (or a stored artifact the service could not
        # link): copied in, as DuckDB reads only the work directory, and never linked, which
        # would put the converter one bug away from writing to it.
        safety.copy_file(src, local)
    return local


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
    if col.type == "bool" and inferrer.is_number(out.plan.expr):
        # DuckDB casts every nonzero number to true: 2, 0.5 and -1 were true, uncounted, while
        # the cast of a text column reads only 0 and 1. A number is a bool only if it is one.
        return f"(CASE WHEN {x} = 0 THEN false WHEN {x} = 1 THEN true END)"
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
        # count_if over no rows is NULL, not 0: a CSV that is only its header, with any
        # expected_schema, raised TypeError here (the 500 after the payment that says "Retry"),
        # where without the schema it converted to an empty table.
        rejects, nulls, kept = (int(x or 0) for x in row) if row else (0, 0, 0)
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


def _out_of_memory_hint(options: ConvertOptions) -> str:
    """What to change when DuckDB ran out of memory converting: only what the call can change."""
    sort = ", drop sort_by (it sorts the whole file)" if options.sort_by else ""
    return (
        f"Split the file into parts with fewer columns or rows{sort}, or pass "
        "options.infer_types=false to keep text columns as text; you were not charged."
    )


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
    kept = keep_gzip(src, filename, workdir, options.format)
    if kept is None:
        path, name = unwrap(src, filename, workdir, limits)
        fmt = detect_format(path, safety.read_magic(path), name, options.format)
    else:
        path, name, fmt = kept
    read: Reader = partial(read_delimited, gzipped=True) if kept else READERS[fmt]
    tmp = workdir / "duck_tmp"
    tmp.mkdir(exist_ok=True)
    con = connect(
        memory_limit=limits.duckdb_memory_limit,
        threads=1 if fmt in _ONE_THREAD else limits.duckdb_threads,
        allowed_dirs=[workdir],
        temp_dir=tmp,
        max_temp_size=limits.duckdb_max_temp_size,
    )
    try:
        raw = read(con, path, workdir, options, fmt, limits)
        text_bytes = raw.text_bytes if raw.text_bytes is not None else path.stat().st_size
        # Nothing reads the input again (raw is a table, or a view over the staged text), and
        # /tmp is RAM on Cloud Run: a 446 MiB decompressed CSV stayed there through inference,
        # the write and a sort's spill, 1.25 GiB of files at the peak.
        path.unlink(missing_ok=True)
        report = _finish(con, raw, out_path, name, options, fmt)
    except duckdb.OutOfMemoryException as exc:
        # Reading, typing every column and the final COPY (a sort, with sort_by) all run in
        # DuckDB; past its memory limit and spill cap that is the file's size, not the moment.
        # It was a 500 whose hint said to retry, which cannot succeed: 30 ISO-date columns of
        # 100 rows once passed the 1 GB limit while being typed. read_csv says 'to read'.
        raise InputTooLarge(
            "The file needs more memory to convert than this server allows.",
            hint=_out_of_memory_hint(options),
        ) from exc
    finally:
        con.close()
    report.source_bytes = source_bytes
    report.duration_ms = int((time.monotonic() - started) * 1000)
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
    columns: list[ColumnReport] = []
    # By position: the file's columns are the SELECT's, in order. Looked up by name, two outputs
    # with one name (DuckDB writes the second as 'a_1') both reported the first column's type
    # and nulls, charged as the description of a file it did not match, and two names equal
    # but for case ('a', 'A' -> 'A_1') crashed with KeyError: 'A'. The name is the file's.
    for o, st in zip(outputs, summary.columns, strict=True):
        columns.append(
            ColumnReport(
                name=st.name,
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
