"""Decide what a file is from its bytes first and its name second.

Agents mislabel files constantly (a TSV called .csv, JSON served as text/plain, an xlsx saved
as .xls), so magic bytes win over extensions wherever a format has a reliable signature.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

from aimart_parquet.engine.models import InputFormat
from aimart_parquet.engine.safety import MAGIC_BYTES, SNIFF_BYTES, sniff_compression
from aimart_parquet.errors import UnsupportedFormat

_SAS7BDAT_MAGIC = bytes.fromhex("000000000000000000000000c2ea8160b31411cfbd92080009c7318c181f1011")
_BOM = b"\xef\xbb\xbf"
# PDF readers accept the '%PDF-' header anywhere in the first 1024 bytes and look for the
# trailer ('startxref', '%%EOF') in the last 1024.
_PDF_WINDOW = 1024
# The most of a file's start ``detect_format`` reads; what ``format_from_start`` needs of one.
START_BYTES = _PDF_WINDOW

_EXTENSIONS: dict[str, InputFormat] = {
    ".csv": InputFormat.CSV,
    ".txt": InputFormat.CSV,
    ".tsv": InputFormat.TSV,
    ".tab": InputFormat.TSV,
    ".json": InputFormat.JSON,
    ".geojson": InputFormat.JSON,
    ".ndjson": InputFormat.NDJSON,
    ".jsonl": InputFormat.NDJSON,
    ".xlsx": InputFormat.XLSX,
    ".xlsm": InputFormat.XLSX,
    ".xlsb": InputFormat.XLSX,
    ".xls": InputFormat.XLS,
    ".ods": InputFormat.ODS,
    ".sav": InputFormat.SAV,
    ".zsav": InputFormat.ZSAV,
    ".por": InputFormat.POR,
    ".dta": InputFormat.DTA,
    ".sas7bdat": InputFormat.SAS7BDAT,
    ".xpt": InputFormat.XPT,
    ".parquet": InputFormat.PARQUET,
    ".pq": InputFormat.PARQUET,
    ".pdf": InputFormat.PDF,
}

SUPPORTED_EXTENSIONS = tuple(sorted(_EXTENSIONS))


def _strip_compression_suffix(name: str) -> str:
    lowered = name.lower()
    for suffix in (".gz", ".gzip", ".bz2", ".zip"):
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


def format_from_name(name: str | None) -> InputFormat | None:
    if not name:
        return None
    suffix = Path(_strip_compression_suffix(name)).suffix.lower()
    return _EXTENSIONS.get(suffix)


def _zip_office_format(path: Path | bytes) -> InputFormat | None:
    try:
        with zipfile.ZipFile(io.BytesIO(path) if isinstance(path, bytes) else path) as zf:
            names = set(zf.namelist())
            if "[Content_Types].xml" in names and any(n.startswith("xl/") for n in names):
                return InputFormat.XLSX
            if "mimetype" in names:
                mime = zf.read("mimetype")[:100]
                if b"opendocument.spreadsheet" in mime:
                    return InputFormat.ODS
    except zipfile.BadZipFile:
        return None
    return None


def _read_window(path: Path | bytes, *, tail: bool) -> bytes:
    if isinstance(path, bytes):
        return path[-_PDF_WINDOW:] if tail else path[:_PDF_WINDOW]
    try:
        with path.open("rb") as fh:
            if tail:
                fh.seek(max(0, fh.seek(0, os.SEEK_END) - _PDF_WINDOW))
            return fh.read(_PDF_WINDOW)
    except OSError:
        return b""


def _pdf_header(magic: bytes) -> bool:
    """The file starts with a PDF header, after at most a BOM, NULs and whitespace."""
    return magic.removeprefix(_BOM).lstrip(b"\x00\t\r\n ")[:5] == b"%PDF-"


def _pdf_after_junk(path: Path | bytes, magic: bytes) -> bool:
    """A PDF whose header follows junk: a UTF-8 BOM, an HTTP or PJL header, binary bytes.

    Detection once looked at offset 0 only, so such a file (which pdfminer reads fine) was
    parsed as CSV and charged for rows of PDF syntax. The header alone is not enough here: a
    CSV may hold the text '%PDF-1.4' in a cell within its first KB. A PDF also ends with its
    trailer, which no CSV does, so both are required. (A file named .pdf is trusted by name.)
    """
    head = magic if len(magic) >= _PDF_WINDOW else _read_window(path, tail=False) or magic
    if b"%PDF-" not in head[:_PDF_WINDOW]:
        return False
    end = _read_window(path, tail=True)
    return b"startxref" in end or b"%%EOF" in end


def format_from_magic(path: Path | bytes, magic: bytes) -> InputFormat | None:
    if magic.startswith(b"PAR1"):
        return InputFormat.PARQUET
    if _pdf_header(magic):
        return InputFormat.PDF
    if magic.startswith(b"$FL3"):
        return InputFormat.ZSAV
    if magic.startswith(b"$FL2"):
        return InputFormat.SAV
    if magic.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return InputFormat.XLS
    if magic.startswith(b"PK\x03\x04"):
        return _zip_office_format(path)
    if magic[: len(_SAS7BDAT_MAGIC)] == _SAS7BDAT_MAGIC:
        return InputFormat.SAS7BDAT
    if magic.startswith((b"HEADER RECORD*******LIB", b"HEADER RECORD*******LIBV8")):
        return InputFormat.XPT
    if magic.startswith(b"<stata_dta>"):
        return InputFormat.DTA
    if b"SPSSPORT" in magic:
        return InputFormat.POR
    if _pdf_after_junk(path, magic):
        return InputFormat.PDF
    return None


def _looks_binary(magic: bytes) -> bool:
    if magic.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")):
        return False  # text with a byte-order mark
    return b"\x00" in magic[:512]


def format_from_text(magic: bytes) -> InputFormat:
    head = magic.lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith((b"{", b"[")):
        first_line, _, rest = head.partition(b"\n")
        if (
            head.startswith(b"{")
            and first_line.rstrip().endswith(b"}")
            and rest.lstrip().startswith(b"{")
        ):
            return InputFormat.NDJSON
        return InputFormat.JSON
    lines = head.splitlines()[:5]
    tabs = sum(line.count(b"\t") for line in lines)
    commas = sum(line.count(b",") + line.count(b";") for line in lines)
    return InputFormat.TSV if tabs > commas else InputFormat.CSV


def detect_format(
    path: Path | bytes, magic: bytes, filename: str | None, forced: InputFormat | None
) -> InputFormat:
    """The format of the file at ``path`` (or of these bytes), which starts with ``magic``."""
    if forced is not None:
        return forced
    by_magic = format_from_magic(path, magic)
    by_name = format_from_name(filename)
    if by_magic is not None:
        # .dta files from Stata 13 and older have no textual signature; trust the name there.
        return by_magic
    if by_name in {InputFormat.DTA, InputFormat.POR, InputFormat.XPT, InputFormat.SAS7BDAT}:
        return by_name  # binary formats whose older variants lack a reliable signature
    if by_name is InputFormat.PDF:
        # Readers skip junk before the header, so a .pdf without a signature we recognise is
        # still most likely a PDF. If it is not one, the PDF reader fails with a 422 that names
        # options.format; sniffed as text it became CSV rows of PDF syntax, and a 200.
        return by_name
    if _looks_binary(magic):
        if by_name is not None:
            return by_name
        raise UnsupportedFormat(
            "Could not recognise this binary file.",
            hint=(
                "Supported: CSV/TSV, JSON/NDJSON, Excel (xlsx/xls/ods), tables in digital PDFs, SPSS (sav/zsav/por), "
                "Stata (dta), SAS (sas7bdat/xpt), Parquet — optionally gzip/bz2/zip compressed. "
                "Pass options.format if the file is one of these."
            ),
            filename=filename,
        )
    if by_name in {InputFormat.CSV, InputFormat.TSV, InputFormat.JSON, InputFormat.NDJSON}:
        sniffed = format_from_text(magic)
        # A .json file that is really NDJSON is common; a .csv that sniffs as JSON is a mislabel.
        if by_name is InputFormat.JSON and sniffed is InputFormat.NDJSON:
            return sniffed
        if by_name in {InputFormat.CSV, InputFormat.TSV} and sniffed in {
            InputFormat.JSON,
            InputFormat.NDJSON,
        }:
            return sniffed
        return by_name
    return format_from_text(magic)


def format_from_start(
    start: bytes, filename: str | None, forced: InputFormat | None, *, whole: bool
) -> InputFormat | None:
    """The format ``convert_file`` reads a file beginning with ``start`` as, or None when its
    start does not decide that. ``whole``: ``start`` is the entire file, not only its start.

    For the checks before the quote (``Service.check_options``), which refuse an option only
    for a format whose reader refuses it. Judged by the name alone, as 5ecb6ac did, an xlsx
    named data.csv with encoding 'auto' (which agents send) and a JSON array named data.csv
    with delimiter '"' were refused before the quote, while 07fa450 converted and settled
    them: the bytes win over the name here, and the reader that runs never reads the option.
    And for ``convert.keep_gzip``, over the first ``START_BYTES`` a gzip holds: a CSV or TSV
    there is read from the gzip, never unpacked, so this must never name a format the
    unpacked file would not be read as. None unpacks it, as before.

    None when the bytes say no more before the file is opened: gzip, bz2 and zip (``unwrap``
    inflates the first two and extracts a zip's member; only a zip's central directory, at its
    end, says it is a workbook, and a workbook reads none of these options anyway); a start
    shorter than ``detect_format`` reads, or one holding a PDF header after junk, whose trailer
    (at the end) decides; bytes nothing recognises, which the engine refuses itself.
    """
    if forced is not None:
        return forced
    if sniff_compression(start[:SNIFF_BYTES]) is not None:
        return None
    magic = start[:MAGIC_BYTES]
    if not whole and (
        len(start) < START_BYTES or (b"%PDF-" in start[:_PDF_WINDOW] and not _pdf_header(magic))
    ):
        return None
    try:
        return detect_format(start, magic, filename, None)
    except UnsupportedFormat:
        return None
