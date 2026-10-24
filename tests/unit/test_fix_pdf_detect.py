"""Review P3: a PDF with a BOM or junk before '%PDF-' was parsed as CSV and charged.

PDF readers accept the header anywhere in the first 1024 bytes and pdfminer reads such files
fine, but detection only looked at offset 0 and did not trust the '.pdf' name, so the PDF's
syntax came back as CSV rows with a 200. The other direction matters as much: a CSV that merely
contains the text '%PDF-' in a cell must stay a CSV.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow.parquet as pq
import pytest
from fpdf import FPDF

from aimart_parquet.engine.convert import convert_file
from aimart_parquet.engine.detect import detect_format, format_from_magic
from aimart_parquet.engine.limits import Limits
from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.engine.models import InputFormat as F
from aimart_parquet.engine.safety import read_magic
from aimart_parquet.errors import InvalidInput

if TYPE_CHECKING:
    from pathlib import Path

    from aimart_parquet.engine.models import ConversionReport

_ROWS = [["Region", "Units", "Revenue"], ["North", "10", "2.50"], ["South", "30", "4.50"]]


def _pdf_bytes() -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for row in _ROWS:
        for cell in row:
            pdf.cell(50, 7, cell, border=1)
        pdf.ln()
    return bytes(pdf.output())


def _convert(
    src: Path, tmp_path: Path, name: str | None, **options: Any
) -> tuple[ConversionReport, list[dict[str, Any]]]:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    out = work / "out.parquet"
    report = convert_file(
        src, out, filename=name, options=ConvertOptions(**options), workdir=work, limits=Limits()
    )
    return report, pq.read_table(out).to_pylist()


_PREFIXES = {
    "bom": b"\xef\xbb\xbf",
    "text": b"garbage before the header\r\n",
    "http": b"HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\n\r\n",
    "binary": bytes(range(128, 250)),
    "pjl": b"\x1b%-12345X@PJL JOB NAME=report\r\n@PJL ENTER LANGUAGE=PDF\r\n",
}


@pytest.mark.parametrize("prefix", sorted(_PREFIXES))
@pytest.mark.parametrize("name", ["report.pdf", None, "download"])
def test_pdf_with_a_prefix_before_the_header_is_a_pdf(
    tmp_path: Path, prefix: str, name: str | None
) -> None:
    src = tmp_path / "doc"
    src.write_bytes(_PREFIXES[prefix] + _pdf_bytes())
    assert detect_format(src, read_magic(src), name, None) is F.PDF
    report, rows = _convert(src, tmp_path, name)
    assert report.source_format is F.PDF
    assert [c.name for c in report.columns] == ["region", "units", "revenue"]
    assert rows == [
        {"region": "North", "units": 10, "revenue": 2.5},
        {"region": "South", "units": 30, "revenue": 4.5},
    ]


def test_bom_at_offset_zero_needs_no_trailer(tmp_path: Path) -> None:
    # A truncated PDF has no trailer; with only a BOM before the header it is still a PDF, as a
    # header at offset 0 (or after whitespace) always was.
    magic = b"\xef\xbb\xbf%PDF-1.7\n1 0 obj\n"
    path = tmp_path / "truncated"
    path.write_bytes(magic)
    assert format_from_magic(path, magic) is F.PDF
    assert format_from_magic(path, b"\x00\n %PDF-1.4\n") is F.PDF


@pytest.mark.parametrize("name", ["files.csv", None, "download"])
def test_csv_mentioning_the_pdf_header_in_a_cell_stays_csv(tmp_path: Path, name: str) -> None:
    # '%PDF-' inside the first KB but no PDF trailer at the end: text, not a PDF.
    text = "file,magic,pages\n" + "".join(f"report{i}.pdf,%PDF-1.{i % 8},{i}\n" for i in range(40))
    src = tmp_path / "files"
    src.write_text(text)
    assert b"%PDF-" in read_magic(src)
    assert detect_format(src, read_magic(src), name, None) is F.CSV
    report, rows = _convert(src, tmp_path, name)
    assert report.rows == 40
    assert rows[0] == {"file": "report0.pdf", "magic": "%PDF-1.0", "pages": 0}


def test_header_beyond_the_first_kilobyte_is_trusted_only_by_name(tmp_path: Path) -> None:
    src = tmp_path / "doc"
    src.write_bytes(b"junk line\n" * 200 + _pdf_bytes())  # header at offset 2000
    assert detect_format(src, read_magic(src), "report.pdf", None) is F.PDF
    report, _ = _convert(src, tmp_path, "report.pdf")
    assert report.rows == 2
    assert detect_format(src, read_magic(src), None, None) is F.CSV  # outside the 1 KB window


def test_a_dot_pdf_that_is_not_a_pdf_fails_cleanly_instead_of_becoming_csv(
    tmp_path: Path,
) -> None:
    src = tmp_path / "doc"
    src.write_text("a,b\n1,2\n3,4\n")
    assert detect_format(src, read_magic(src), "data.pdf", None) is F.PDF
    with pytest.raises(InvalidInput, match="Could not read the PDF") as info:
        _convert(src, tmp_path, "data.pdf")
    assert info.value.http_status == 422  # unpaid
    assert "options.format" in (info.value.hint or "")
    report, _ = _convert(src, tmp_path, "data.pdf", format="csv")  # the hint works
    assert report.rows == 2


def test_magic_without_a_readable_file_falls_back_to_the_bytes_given(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert format_from_magic(missing, b"a,b\n%PDF-1.4,2\n") is None
    assert format_from_magic(missing, b"%PDF-1.4\n") is F.PDF
