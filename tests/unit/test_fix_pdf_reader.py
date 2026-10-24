"""Regression tests for review findings on the PDF reader (engine/readers/pdf.py).

Each test names the finding it pins. Fixtures are generated here (fpdf2, or a tiny raw-PDF
assembler for structures fpdf2 cannot produce), so no binary is committed.
"""

from __future__ import annotations

import shutil
import time
import zlib
from typing import TYPE_CHECKING, Any

import pyarrow.parquet as pq
import pytest
from fpdf import FPDF

from aimart_parquet.engine.convert import convert_file
from aimart_parquet.engine.isolate import run_isolated
from aimart_parquet.engine.limits import Limits
from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.engine.readers import pdf as pdf_reader
from aimart_parquet.engine.readers.pdf import parse_pages
from aimart_parquet.errors import InputTooLarge, InvalidInput, InvalidRequest

if TYPE_CHECKING:
    from pathlib import Path

    from aimart_parquet.engine.models import ConversionReport

_ROWS = [["Region", "Units", "Revenue"]] + [
    [f"Area{i}", str(100 + i), f"{1000 + i * 7}.50"] for i in range(12)
]


def _convert(
    src: Path, tmp_path: Path, **options: Any
) -> tuple[ConversionReport, list[dict[str, Any]]]:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    out = work / "out.parquet"
    report = convert_file(
        src,
        out,
        filename=src.name,
        options=ConvertOptions(**options),
        workdir=work,
        limits=Limits(),
    )
    return report, pq.read_table(out).to_pylist()


def _borderless_rows(pdf: FPDF, rows: list[list[str]], y0: float = 40) -> None:
    pdf.set_font("Helvetica", size=11)
    for j, row in enumerate(rows):
        for k, cell in enumerate(row):
            pdf.set_xy(20 + k * 50, y0 + j * 7)
            pdf.cell(45, 6, cell, border=0)


def _bordered_rows(pdf: FPDF, rows: list[list[str]]) -> None:
    pdf.set_font("Helvetica", size=11)
    for row in rows:
        for cell in row:
            pdf.cell(50, 7, cell, border=1)
        pdf.ln()


def _raw_pdf(page: bytes, *extra: bytes, content: bytes = b"") -> bytes:
    """A one-page PDF: objects 1-4 are catalog, pages, font, page; 5 is the content stream."""
    stream = zlib.compress(content, 9)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        page,
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream",
        *extra,
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


_PAGE = (
    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
    b"/Resources << /Font << /F1 3 0 R >> >> /Contents %s >>"
)


def _glyph_bomb(path: Path, glyphs: int) -> Path:
    # One text-show operator drawing `glyphs` characters: a few KB on disk after Flate.
    content = b"BT /F1 1 Tf 10 10 Td (" + b"A" * glyphs + b") Tj ET"
    path.write_bytes(_raw_pdf(_PAGE % b"5 0 R", content=content))
    return path


# ---------------------------------------------------------------- P1: glyph explosion


def test_p1_glyph_bomb_fails_fast_as_input_too_large(tmp_path: Path) -> None:
    # Review: 1M glyphs in a 1.6 KB PDF took the worker to 2 GB RSS in 15 s. The page budget
    # stops the interpreter while it draws, before pdfplumber builds an object per glyph.
    src = _glyph_bomb(tmp_path / "bomb.pdf", 1_000_000)
    assert src.stat().st_size < 10_000
    started = time.monotonic()
    with pytest.raises(InputTooLarge, match="page 1") as info:
        _convert(src, tmp_path)
    assert time.monotonic() - started < 10
    assert info.value.http_status == 413
    assert "pdf_pages" in (info.value.hint or "")


def test_p1_document_budget_spans_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_reader, "MAX_DOC_OBJECTS", 500)
    pdf = FPDF()
    for _ in range(4):
        pdf.add_page()
        _borderless_rows(pdf, _ROWS)  # ~200 glyphs and no drawing objects per page
    src = tmp_path / "many.pdf"
    pdf.output(str(src))
    with pytest.raises(InputTooLarge, match="pages read") as info:
        _convert(src, tmp_path)
    assert "pdf_pages" in (info.value.hint or "")
    shutil.rmtree(tmp_path / "work")
    report, _ = _convert(src, tmp_path, pdf_pages="1-2")  # fewer pages fit the budget
    assert report.rows == 24  # the same header on both pages: one table


def test_p1_drawing_objects_count_against_the_page_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pdf_reader, "MAX_PAGE_OBJECTS", 500)
    content = b"".join(b"%d 10 1 1 re f\n" % (i % 600) for i in range(2000))
    src = tmp_path / "rects.pdf"
    src.write_bytes(_raw_pdf(_PAGE % b"5 0 R", content=content))
    with pytest.raises(InputTooLarge, match="page 1"):
        _convert(src, tmp_path)


def test_p1_each_page_is_closed_before_the_next_is_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pdfplumber closes every page when the document closes; that is too late: all pages' glyph
    # objects were alive at once. Each page must be released before the next one is parsed.
    events: list[tuple[str, int]] = []
    page_class = pdf_reader.pdfplumber.page.Page
    close, extract = page_class.close, page_class.extract_tables

    def spy_close(self: Any) -> None:
        events.append(("close", self.page_number))
        close(self)

    def spy_extract(self: Any, *args: Any, **kwargs: Any) -> Any:
        if not events or events[-1] != ("read", self.page_number):
            events.append(("read", self.page_number))
        return extract(self, *args, **kwargs)

    monkeypatch.setattr(page_class, "close", spy_close)
    monkeypatch.setattr(page_class, "extract_tables", spy_extract)
    pdf = FPDF()
    for _ in range(3):
        pdf.add_page()
        _borderless_rows(pdf, _ROWS)
    src = tmp_path / "three.pdf"
    pdf.output(str(src))
    _convert(src, tmp_path)
    assert events[:6] == [
        ("read", 1),
        ("close", 1),
        ("read", 2),
        ("close", 2),
        ("read", 3),
        ("close", 3),
    ]


# ---------------------------------------------------------------- P2: title above a table


def test_p2_title_line_above_a_borderless_table_is_not_the_header(tmp_path: Path) -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=14)
    pdf.set_xy(20, 25)
    pdf.cell(0, 8, "Quarterly sales by region")
    _borderless_rows(pdf, _ROWS)
    src = tmp_path / "titled.pdf"
    pdf.output(str(src))
    report, rows = _convert(src, tmp_path)
    assert [c.name for c in report.columns] == ["region", "units", "revenue"]
    assert [c.type for c in report.columns] == ["string", "int64", "double"]
    assert report.rows == 12
    assert rows[0] == {"region": "Area0", "units": 100, "revenue": 1000.5}
    dropped = [w for w in report.warnings if "above the header" in w]
    assert len(dropped) == 1
    assert "Quarterly sales" in dropped[0]


def test_p2_merged_title_row_in_a_bordered_table_is_dropped(tmp_path: Path) -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.cell(150, 7, "Quarterly sales by region", border=1)  # one cell across all columns
    pdf.ln()
    _bordered_rows(pdf, _ROWS[:6])
    src = tmp_path / "merged.pdf"
    pdf.output(str(src))
    report, rows = _convert(src, tmp_path)
    assert [c.name for c in report.columns] == ["region", "units", "revenue"]
    assert report.rows == 5
    assert rows[0]["units"] == 100
    assert any("Quarterly sales by region" in w for w in report.warnings)


def test_p2_text_in_a_numeric_column_does_not_eat_the_header(tmp_path: Path) -> None:
    # 'pending' is text where numbers belong: the first "data" row is row 3. The rows above it
    # are as wide as the body, so they are not captions and nothing is dropped.
    rows = [
        ["Region", "Units", "Revenue"],
        ["North", "pending", "pending"],
        ["South", "n/a", "pending"],
        *_ROWS[1:8],
    ]
    pdf = FPDF()
    pdf.add_page()
    _bordered_rows(pdf, rows)
    src = tmp_path / "pending.pdf"
    pdf.output(str(src))
    report, out = _convert(src, tmp_path)
    assert [c.name for c in report.columns] == ["region", "units", "revenue"]
    assert report.rows == 9
    assert out[0]["region"] == "North"
    assert not any("above the header" in w for w in report.warnings)


def test_p2_missing_values_do_not_hide_the_first_data_row(tmp_path: Path) -> None:
    rows = [["Region", "Units", "Revenue"], ["North", "-", "n/a"], *_ROWS[1:6]]
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.cell(150, 7, "Sales", border=1)
    pdf.ln()
    _bordered_rows(pdf, rows)
    src = tmp_path / "nulls.pdf"
    pdf.output(str(src))
    report, out = _convert(src, tmp_path)
    assert [c.name for c in report.columns] == ["region", "units", "revenue"]
    assert out[0] == {"region": "North", "units": None, "revenue": None}
    assert report.rows == 6


def test_p2_numeric_header_row_is_kept_as_before(tmp_path: Path) -> None:
    # A header of years looks like data; with nothing above it, row 0 stays the header.
    rows = [["Region", "2024", "2025"]] + [[f"R{i}", str(i), str(i * 2)] for i in range(5)]
    pdf = FPDF()
    pdf.add_page()
    _bordered_rows(pdf, rows)
    src = tmp_path / "years.pdf"
    pdf.output(str(src))
    report, _ = _convert(src, tmp_path)
    assert report.rows == 5
    assert not any("above the header" in w for w in report.warnings)


# ---------------------------------------------------------------- P4: page ranges


def test_p4_huge_page_range_is_clamped_not_iterated() -> None:
    started = time.monotonic()
    assert parse_pages("1-99999999999", 3) == [1, 2, 3]
    assert parse_pages("2-99999999999999999999,1", 3) == [1, 2, 3]
    assert time.monotonic() - started < 1
    with pytest.raises(InvalidRequest, match="selects no page"):
        parse_pages("5-99999999999", 3)


def test_p4_page_limit_still_applies_after_clamping() -> None:
    with pytest.raises(InvalidRequest, match="limit is 300"):
        parse_pages("1-99999999999", 400)


# ---------------------------------------------------------------- P5: reference cycles


def _refcycle(path: Path) -> Path:
    # /Contents -> 6 0 R, object 6 is "7 0 R", object 7 is "6 0 R": resolve1 looped forever.
    path.write_bytes(_raw_pdf(_PAGE % b"6 0 R", b"7 0 R", b"6 0 R", content=b"BT ET"))
    return path


def test_p5_reference_cycle_is_invalid_input_not_a_hang(tmp_path: Path) -> None:
    src = _refcycle(tmp_path / "refcycle.pdf")
    work = tmp_path / "work"
    work.mkdir()
    # In a child with a deadline, so the unfixed hang fails this test (as a Timeout, not an
    # InvalidInput) instead of hanging the suite. No wall-clock assertion: under a loaded
    # machine the forkserver's first start alone took longer than the fixed parse.
    with pytest.raises(InvalidInput, match="reference") as info:
        run_isolated(
            convert_file,
            src,
            work / "out.parquet",
            filename=src.name,
            options=ConvertOptions(),
            workdir=work,
            limits=Limits(),
            timeout_s=20,
        )
    assert info.value.http_status == 422


def test_p5_resolve_never_returns_a_reference_and_bounds_chains() -> None:
    from pdfminer.pdfparser import PDFSyntaxError
    from pdfminer.pdftypes import PDFObjRef, resolve1, resolve_all

    class Doc:
        def __init__(self, objs: dict[int, object]) -> None:
            self.objs = objs

        def getobj(self, objid: int) -> object:
            return self.objs[objid]

    chain = Doc({})
    chain.objs = {i: PDFObjRef(chain, i + 1) for i in range(1, 10)} | {10: 42}
    assert PDFObjRef(chain, 1).resolve() == 42  # short chains are followed in one call
    cyclic = Doc({})
    cyclic.objs = {6: PDFObjRef(cyclic, 7), 7: PDFObjRef(cyclic, 6)}
    for resolve in (PDFObjRef(cyclic, 6).resolve, lambda: resolve1(PDFObjRef(cyclic, 6))):
        with pytest.raises(PDFSyntaxError, match="reference"):
            resolve()
    with pytest.raises(PDFSyntaxError):
        resolve_all([PDFObjRef(cyclic, 7)])


def test_p5_patch_is_idempotent() -> None:
    from pdfminer.pdftypes import PDFObjRef

    before = PDFObjRef.resolve
    pdf_reader._bound_reference_chains()
    assert PDFObjRef.resolve is before


# ---------------------------------------------------------------- P6: degenerate bordered boxes


def test_p6_bordered_note_box_does_not_disable_the_borderless_fallback(tmp_path: Path) -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.cell(80, 7, "Note", border=1)
    pdf.cell(80, 7, "Draft", border=1)
    pdf.ln()
    pdf.add_page()
    _borderless_rows(pdf, _ROWS)
    src = tmp_path / "box_then_borderless.pdf"
    pdf.output(str(src))
    report, rows = _convert(src, tmp_path)
    assert report.rows == 12
    assert rows[0] == {"region": "Area0", "units": 100, "revenue": 1000.5}
    assert any("text alignment" in w for w in report.warnings)


def test_p6_empty_bordered_grid_on_the_same_page(tmp_path: Path) -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for _ in range(2):  # a 2 x 2 grid of empty boxes (a signature block)
        pdf.cell(60, 10, "", border=1)
        pdf.cell(60, 10, "", border=1)
        pdf.ln()
    _borderless_rows(pdf, _ROWS, y0=60)
    src = tmp_path / "signature_then_table.pdf"
    pdf.output(str(src))
    report, _ = _convert(src, tmp_path)
    assert report.rows == 12
    assert [c.name for c in report.columns] == ["region", "units", "revenue"]


def test_p6_borderless_page_after_a_bordered_page_is_listed_and_selectable(
    tmp_path: Path,
) -> None:
    pdf = FPDF()
    pdf.add_page()
    _bordered_rows(pdf, [["A", "B"], ["1", "2"], ["3", "4"]])
    pdf.add_page()
    _borderless_rows(pdf, _ROWS)
    src = tmp_path / "mixed.pdf"
    pdf.output(str(src))
    report, _ = _convert(src, tmp_path)
    # The bordered table stays the default, as before the per-page text pass existed (text
    # alignment also turns contents pages into "tables"); the borderless one is now listed.
    assert report.rows == 2
    assert len(report.available_sheets or []) == 2
    assert any("ruling lines" in w for w in report.warnings)
    shutil.rmtree(tmp_path / "work")
    report, rows = _convert(src, tmp_path, pdf_table=1)
    assert report.rows == 12
    assert rows[0] == {"region": "Area0", "units": 100, "revenue": 1000.5}
    assert any("text alignment" in w for w in report.warnings)


def test_p6_bordered_table_beats_a_larger_contents_page(tmp_path: Path) -> None:
    pdf = FPDF()
    pdf.add_page()
    _borderless_rows(pdf, [[f"Chapter {i}", "", str(3 + i * 4)] for i in range(20)])
    pdf.add_page()
    _bordered_rows(pdf, _ROWS[:4])
    src = tmp_path / "contents_then_table.pdf"
    pdf.output(str(src))
    report, rows = _convert(src, tmp_path)
    assert report.rows == 3
    assert rows[0] == {"region": "Area0", "units": 100, "revenue": 1000.5}


# ---------------------------------------------------------------- P7: continuation without header


def test_p7_bordered_table_continuing_without_a_repeated_header_is_merged(tmp_path: Path) -> None:
    pdf = FPDF()
    pdf.add_page()
    _bordered_rows(pdf, _ROWS[:8])
    pdf.add_page()
    _bordered_rows(pdf, _ROWS[8:])
    src = tmp_path / "cont_noheader.pdf"
    pdf.output(str(src))
    report, rows = _convert(src, tmp_path)
    assert report.rows == 12
    assert [c.name for c in report.columns] == ["region", "units", "revenue"]
    assert {"region": "Area7", "units": 107, "revenue": 1049.5} in rows
    assert len(report.available_sheets or []) == 1
    assert any("continues across 2 pages" in w for w in report.warnings)


def test_p7_a_new_table_with_its_own_header_is_not_a_continuation(tmp_path: Path) -> None:
    pdf = FPDF()
    pdf.add_page()
    _bordered_rows(pdf, _ROWS[:5])
    pdf.add_page()
    _bordered_rows(pdf, [["City", "Stores", "Sales"], ["Oslo", "3", "9.50"], ["Rome", "4", "7.25"]])
    src = tmp_path / "two_tables.pdf"
    pdf.output(str(src))
    report, _ = _convert(src, tmp_path)
    assert len(report.available_sheets or []) == 2


def test_p7_text_rows_under_a_numeric_column_are_not_a_continuation(tmp_path: Path) -> None:
    pdf = FPDF()
    pdf.add_page()
    _bordered_rows(pdf, _ROWS[:5])
    pdf.add_page()
    _bordered_rows(pdf, [["North", "many", "n/a"], ["South", "few", "n/a"]])
    src = tmp_path / "notes.pdf"
    pdf.output(str(src))
    report, _ = _convert(src, tmp_path)
    assert len(report.available_sheets or []) == 2


# ---------------------------------------------------------------- P8: encrypted PDFs


def _encrypted(path: Path, user_password: str) -> Path:
    pdf = FPDF()
    pdf.set_encryption(owner_password="owner-secret", user_password=user_password)
    pdf.add_page()
    _bordered_rows(pdf, _ROWS[:4])
    pdf.output(str(path))
    return path


def test_p8_password_protected_pdf_says_so(tmp_path: Path) -> None:
    with pytest.raises(InvalidInput) as info:
        _convert(_encrypted(tmp_path / "enc.pdf", "user-secret"), tmp_path)
    assert info.value.message == "The PDF is password-protected."
    assert info.value.hint == "Remove the password and resend."


def test_p8_encrypted_pdf_with_an_empty_user_password_opens(tmp_path: Path) -> None:
    report, rows = _convert(_encrypted(tmp_path / "enc_empty.pdf", ""), tmp_path)
    assert report.rows == 3
    assert rows[0]["region"] == "Area0"


def test_p8_unexplained_parser_errors_still_name_something() -> None:
    from pdfminer.pdfdocument import PDFEncryptionError
    from pdfplumber.utils.exceptions import PdfminerException

    assert pdf_reader._reason(PdfminerException(KeyError())) == "KeyError"
    assert pdf_reader._reason(ValueError("bad xref")) == "bad xref"
    with pytest.raises(InvalidInput, match="cannot open") as info:
        pdf_reader._raise_unreadable(PdfminerException(PDFEncryptionError("Unknown algorithm")))
    assert "encryption" in (info.value.hint or "")
