"""Decide what a file is from its bytes first and its name second.

Agents mislabel files constantly (a TSV called .csv, JSON served as text/plain, an xlsx saved
as .xls), so magic bytes win over extensions wherever a format has a reliable signature.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from aimart_parquet.engine.models import InputFormat
from aimart_parquet.errors import UnsupportedFormat

_SAS7BDAT_MAGIC = bytes.fromhex("000000000000000000000000c2ea8160b31411cfbd92080009c7318c181f1011")

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


def _zip_office_format(path: Path) -> InputFormat | None:
    try:
        with zipfile.ZipFile(path) as zf:
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


def format_from_magic(path: Path, magic: bytes) -> InputFormat | None:
    if magic.startswith(b"PAR1"):
        return InputFormat.PARQUET
    if magic.lstrip(b"\x00\t\r\n ")[:5] == b"%PDF-":
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
    path: Path, magic: bytes, filename: str | None, forced: InputFormat | None
) -> InputFormat:
    if forced is not None:
        return forced
    by_magic = format_from_magic(path, magic)
    by_name = format_from_name(filename)
    if by_magic is not None:
        # .dta files from Stata 13 and older have no textual signature; trust the name there.
        return by_magic
    if by_name in {InputFormat.DTA, InputFormat.POR, InputFormat.XPT, InputFormat.SAS7BDAT}:
        return by_name  # binary formats whose older variants lack a reliable signature
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
