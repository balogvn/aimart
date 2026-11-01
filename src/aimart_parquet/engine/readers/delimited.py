"""CSV/TSV: our own dialect and header detection, DuckDB for the heavy lifting.

DuckDB's built-in sniffer is excellent on clean files and brittle on the files people actually
pay to have cleaned: one ragged row or a blank header cell can make it skip lines or drop the
header entirely. So we sniff the dialect ourselves from a sample with the stdlib csv parser
(scoring each candidate delimiter by how consistent row widths are), then hand DuckDB an
explicit dialect, every column as VARCHAR, and let the type ladder decide types afterwards.

The text is staged once as compressed Parquet and ``raw`` is a view over it, never a table:
memory then stays flat as files grow, where a loaded table grew with them.
"""

from __future__ import annotations

import csv
import gzip
import io
import re
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

import duckdb

from aimart_parquet.engine import safety
from aimart_parquet.engine.duck import ident, literal
from aimart_parquet.engine.models import InputFormat
from aimart_parquet.engine.readers import RAW_TABLE, RawColumn, RawTable
from aimart_parquet.engine.text import TextInfo, codec_name, to_utf8, utf8_size
from aimart_parquet.errors import InputTooLarge, InvalidInput

if TYPE_CHECKING:
    from pathlib import Path

    from aimart_parquet.engine.limits import Limits
    from aimart_parquet.engine.models import ConvertOptions

_SAMPLE_BYTES = 2 * 1024 * 1024
_SAMPLE_ROWS = 2000
_CANDIDATES = (",", ";", "\t", "|")
_MAX_LINE = 16 * 1024 * 1024
_STAGE_FILE = "raw_text.parquet"
# Rows per row group of the staged text. The writer buffers one row group of every column:
# 122,880 rows of a 40-column file took 210 MB, 30,720 took 100 MB, for a 4% larger file.
_STAGE_ROWS = 30_720

_NUMERIC = re.compile(
    r"[+-]?[$€£¥₦]?\s*(\d+([.,]\d+)*|\.\d+)([eE][+-]?\d+)?%?|[+-]?\d{1,3}([ .,]\d{3})+([.,]\d+)?"
)
_DATE = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}([ T].*)?")
_BOOL = {"true", "false", "yes", "no", "t", "f", "y", "n"}

csv.field_size_limit(_MAX_LINE)


@dataclass(frozen=True)
class Dialect:
    delimiter: str
    width: int
    consistency: float
    # Blank rows before the first real one. The header comes from the first non-blank row, so
    # DuckDB must skip exactly these: read_csv(header=true) took a leading blank line as the
    # header and read the real one as data (an extra row; a numeric column stayed text).
    skip: int = 0


def _cell_kind(cell: str) -> str:
    s = cell.strip()
    if not s:
        return "empty"
    if _NUMERIC.fullmatch(s):
        return "number"
    if _DATE.fullmatch(s):
        return "date"
    if s.lower() in _BOOL:
        return "bool"
    return "text"


def _parse(sample: str, delimiter: str) -> tuple[list[list[str]], int]:
    """Non-blank rows, and how many blank rows came before the first of them.

    Blank means no cell has a non-space character: an empty line, spaces, or `` , ,``. Like
    DuckDB's ``skip``, a quoted record spanning lines counts once.
    """
    reader = csv.reader(io.StringIO(sample), delimiter=delimiter, quotechar='"', strict=False)
    rows: list[list[str]] = []
    leading = 0
    try:
        for row in reader:
            if row and any(cell.strip() for cell in row):
                rows.append(row)
            elif not rows:
                leading += 1
            if len(rows) >= _SAMPLE_ROWS:
                break
    except csv.Error:
        pass  # a broken quote late in the sample: score what parsed before it
    return rows, leading


def sniff_dialect(sample: str, forced: str | None) -> tuple[Dialect, list[list[str]]]:
    candidates = (forced,) if forced else _CANDIDATES
    best: tuple[tuple[bool, float, int], Dialect, list[list[str]]] | None = None
    for delim in candidates:
        rows, leading = _parse(sample, delim)
        if not rows:
            continue
        widths = Counter(len(r) for r in rows)
        width, count = widths.most_common(1)[0]
        consistency = count / len(rows)
        score = (width > 1, consistency, width)
        if best is None or score > best[0]:
            best = (score, Dialect(delim, width, consistency, leading), rows)
    if best is None:
        return Dialect(forced or ",", 1, 1.0), []
    return best[1], best[2]


def detect_header(rows: list[list[str]], width: int) -> bool:
    """True when the first row looks like names rather than data.

    For each column whose body is mostly typed (numbers, dates, booleans), a text cell in row 0
    is a vote for a header. A first row that is typed exactly like the body is data. An
    all-text table is assumed to have a header, because that is overwhelmingly the norm.
    """
    if len(rows) < 2:
        return True
    first, body = rows[0], rows[1:200]
    typed_columns = header_votes = 0
    for c in range(width):
        kinds = Counter(_cell_kind(r[c]) for r in body if c < len(r) and r[c].strip())
        if not kinds:
            continue
        top, count = kinds.most_common(1)[0]
        if top != "text" and count / sum(kinds.values()) >= 0.8:
            typed_columns += 1
            if c < len(first) and _cell_kind(first[c]) in {"text", "empty"}:
                header_votes += 1
    if typed_columns:
        return header_votes > 0
    return True


def _read_sample(path: Path, *, gzipped: bool = False) -> tuple[str, bool]:
    with gzip.open(path, "rb") if gzipped else path.open("rb") as fh:
        data = fh.read(_SAMPLE_BYTES + 1)
    complete = len(data) <= _SAMPLE_BYTES
    text = data[:_SAMPLE_BYTES].decode("utf-8", errors="replace").removeprefix("\ufeff")
    if not complete and "\n" in text:
        text = text[: text.rfind("\n")]  # never score a half-read final line
    return text, complete


def _gzip_text(
    path: Path, workdir: Path, options: ConvertOptions, limits: Limits
) -> tuple[TextInfo, int | None]:
    """The text of the gzip ``path``, and its size where DuckDB is to read it from the gzip.

    UTF-8 stays in the gzip: one pass counts it against the bomb cap (InputTooLarge, as unwrap's
    copy does) and checks it, unless options.encoding says UTF-8 (to_utf8 would not check
    either). Other text is transcoded from the stream: only its UTF-8 version is written. A gzip
    DuckDB could read differently from Python, or an encoding to_utf8 refuses, is unpacked over
    ``path`` first, as unwrap did, so its errors come in the same order.
    """
    encoding, cap = options.encoding, limits.max_decompressed_bytes
    codec = codec_name(encoding) if encoding else "utf-8"
    unpack = codec is None
    if codec == "utf-8":
        chunks = safety.gzip_chunks(path, cap)
        try:
            with closing(chunks):
                size = sum(map(len, chunks)) if encoding else utf8_size(chunks)
        except safety.GzipMismatch:
            size, unpack = None, True
        if size is not None:
            return TextInfo(path, "utf-8", 0), size
    if unpack:
        unpacked = workdir / "decompressed"
        safety.decompress_stream(path, unpacked, "gzip", cap)
        unpacked.replace(path)
        return to_utf8(path, workdir, encoding, replace=True), None
    opener = partial(safety.open_gzip, path, cap)
    return to_utf8(path, workdir, encoding, replace=True, opener=opener), None


def read_delimited(
    con: duckdb.DuckDBPyConnection,
    path: Path,
    workdir: Path,
    options: ConvertOptions,
    fmt: InputFormat,
    limits: Limits,
    *,
    gzipped: bool = False,
) -> RawTable:
    """``gzipped``: ``path`` is still a gzip (convert.keep_gzip), whose text never lands on the
    RAM disk unpacked: DuckDB reads UTF-8 from the gzip, and other text is transcoded from it.
    """
    if gzipped:
        info, size = _gzip_text(path, workdir, options, limits)
    else:
        info, size = to_utf8(path, workdir, options.encoding, replace=True), None
    warnings: list[str] = []
    if info.replaced_chars:
        warnings.append(
            f"{info.replaced_chars} characters could not be decoded as {info.encoding} and were "
            "replaced with U+FFFD; pass options.encoding if the text looks wrong"
        )
    sample, _ = _read_sample(info.path, gzipped=size is not None)
    forced = options.delimiter or ("\t" if fmt is InputFormat.TSV else None)
    dialect, rows = sniff_dialect(sample, forced)
    if not rows:
        raise InvalidInput(
            "The file contains no rows.", hint="Send a file with at least one row of data."
        )
    if dialect.consistency < 0.5 and not forced:
        warnings.append(
            f"row widths are irregular (only {dialect.consistency:.0%} of sampled rows have "
            f"{dialect.width} fields); pass options.delimiter if columns look wrong"
        )
    header = options.header if options.header is not None else detect_header(rows, dialect.width)
    width = dialect.width
    if header:
        names = [rows[0][i] if i < len(rows[0]) else "" for i in range(width)]
    else:
        names = [f"column_{i + 1}" for i in range(width)]
    sources = [f"c{i}" for i in range(width)]
    columns_sql = "{" + ", ".join(f"{literal(s)}: 'VARCHAR'" for s in sources) + "}"
    # buffer_size defaults to 16 x max_line_size: at our line cap DuckDB read a 200 MB file
    # through 256 MB of buffers. One line cap's worth still parses a 16 MB line.
    csv_args = (
        f"{literal(str(info.path))}, delim={literal(dialect.delimiter)}, quote='\"', "
        f"escape='\"', header={str(header).lower()}, skip={dialect.skip}, auto_detect=false, "
        f"columns={columns_sql}, null_padding=true, max_line_size={_MAX_LINE}, "
        f"buffer_size={_MAX_LINE}"
    )
    if size is not None:
        csv_args += ", compression='gzip'"
    stage = workdir / _STAGE_FILE
    try:
        # Staged, not loaded. CREATE TABLE raw held every cell as an uncompressed VARCHAR (1.6x
        # the file, more for short cells), and a 200 MB CSV peaked at 700 MB of the job's 700 MB
        # cap. A COPY streams in bounded memory; its zstd file was 9-11x smaller than that table
        # (and on Cloud Run's RAM disk, smaller is what counts); inference scans it faster.
        con.execute(
            f"COPY (SELECT * FROM read_csv({csv_args}, strict_mode=false)) TO {literal(str(stage))} "
            f"(FORMAT parquet, COMPRESSION zstd, COMPRESSION_LEVEL 1, ROW_GROUP_SIZE {_STAGE_ROWS})"
        )
    except duckdb.OutOfMemoryException as exc:
        # Past DuckDB's memory limit this is a size problem, not a parsing one, and the old
        # message sent callers to the delimiter.
        raise InputTooLarge(
            "The file needs more memory to read than this server allows.",
            hint="Send a smaller file, or split it into parts; you were not charged.",
        ) from exc
    except Exception as exc:  # duckdb raises several unrelated exception types here
        raise InvalidInput(
            f"Could not parse the file as delimited text: {str(exc).splitlines()[0]}",
            hint="Check options.delimiter, options.encoding and options.header.",
        ) from exc
    con.execute(
        f"CREATE VIEW {ident(RAW_TABLE)} AS SELECT * FROM read_parquet({literal(str(stage))})"
    )
    warnings.extend(_malformed_row_warnings(con, csv_args))
    return RawTable(
        table=RAW_TABLE,
        columns=[
            RawColumn(source=s, original=n, infer=True, text=True)
            for s, n in zip(sources, names, strict=True)
        ],
        source_format=InputFormat.TSV if dialect.delimiter == "\t" else InputFormat.CSV,
        encoding=info.encoding,
        delimiter=dialect.delimiter,
        prefer_decimal_comma=dialect.delimiter == ";",
        text_bytes=info.path.stat().st_size if size is None else size,
        warnings=warnings,
    )


def _malformed_row_warnings(con: duckdb.DuckDBPyConnection, csv_args: str) -> list[str]:
    """Second, strict pass that only counts rows the lenient read had to bend."""
    try:
        con.execute(
            f"SELECT count(*) FROM read_csv({csv_args}, strict_mode=true, ignore_errors=true, "
            "store_rejects=true, rejects_table='aimart_rejects', rejects_scan='aimart_scans')"
        ).fetchall()  # results stream lazily: without fetching, the rejects table stays partial
        stats = con.execute(
            "SELECT error_type, count(*), min(line) FROM aimart_rejects GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
    except Exception:  # noqa: BLE001 - diagnostics must never fail the conversion
        return []
    messages = {
        "TOO MANY COLUMNS": "had more fields than the header; extra fields were dropped",
        "MISSING COLUMNS": "had fewer fields than the header; missing fields became null",
        "UNQUOTED VALUE": "had stray quote characters",
        "LINE SIZE OVER MAXIMUM": "exceeded the maximum line size",
        "INVALID UNICODE": "contained invalid characters",
    }
    return [
        f"{count} rows {messages.get(str(kind), str(kind).lower())} (first at line {line})"
        for kind, count, line in stats
    ]
