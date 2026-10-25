"""Regression tests for review findings on the PDF reader (engine/readers/pdf.py).

Each test names the finding it pins. Fixtures are generated here (fpdf2, or a tiny raw-PDF
assembler for structures fpdf2 cannot produce), so no binary is committed.
"""

from __future__ import annotations

import gc
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


def _form(content: bytes) -> bytes:
    """A form XObject (object 6 holds the resources it shares with the page)."""
    data = zlib.compress(content, 9)
    head = b"<< /Type /XObject /Subtype /Form /BBox [0 0 10 10] /Resources 6 0 R /Length %d "
    return head % len(data) + b"/Filter /FlateDecode >>\nstream\n" + data + b"\nendstream"


def test_p1_nested_form_xobjects_count_against_the_page_budget(tmp_path: Path) -> None:
    # X1 draws X2 twice, X2 draws X3 twice, ...: 24 levels in 5 KB are 16M figures and not one
    # glyph, so only counting figures stops it (it reached the 700 MB job cap in 13 s).
    depth = 24
    names = b" ".join(b"/X%d %d 0 R" % (i, 6 + i) for i in range(1, depth + 1))
    forms = [_form(b"/X%d Do /X%d Do" % (i + 1, i + 1)) for i in range(1, depth)] + [_form(b"")]
    page = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources 6 0 R "
    page += b"/Contents 5 0 R >>"
    resources = b"<< /Font << /F1 3 0 R >> /XObject << %s >> >>" % names
    src = tmp_path / "forms.pdf"
    src.write_bytes(_raw_pdf(page, resources, *forms, content=b"/X1 Do /X1 Do"))
    assert src.stat().st_size < 8_000
    started = time.monotonic()
    with pytest.raises(InputTooLarge, match="page 1"):
        _convert(src, tmp_path)
    assert time.monotonic() - started < 30


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


def _many_pages(path: Path, count: int) -> Path:
    pdf = FPDF()
    pdf.add_page()
    _bordered_rows(pdf, _ROWS[:4])
    for _ in range(count - 1):
        pdf.add_page()
    pdf.output(str(path))
    return path


def test_p1_only_the_pages_read_are_built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # pdfplumber's doc.pages builds a Page (~2 KB) for every page before one is read: 100k
    # empty pages in a 12 MB file took 200 MB and 12 s, and a few MB of object streams hold
    # millions. Pages are now built one at a time, and only those that are read.
    built: list[int] = []
    page_class = pdf_reader.pdfplumber.page.Page
    init = page_class.__init__

    def spy(self: Any, *args: Any, **kwargs: Any) -> None:
        init(self, *args, **kwargs)
        built.append(self.page_number)

    monkeypatch.setattr(page_class, "__init__", spy)
    src = _many_pages(tmp_path / "long.pdf", 400)
    report, rows = _convert(src, tmp_path, pdf_pages="1")
    assert built == [1]
    assert report.rows == 3
    assert rows[0] == {"region": "Area0", "units": 100, "revenue": 1000.5}
    built.clear()
    shutil.rmtree(tmp_path / "work")
    report, _ = _convert(src, tmp_path)
    assert built == list(range(1, 301))
    assert any("first 300 of 400 pages" in w for w in report.warnings)


def test_p1_page_count_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_reader, "MAX_DOCUMENT_PAGES", 50)
    src = _many_pages(tmp_path / "long.pdf", 51)
    with pytest.raises(InputTooLarge, match="more than 50 pages") as info:
        _convert(src, tmp_path, pdf_pages="1")
    assert "split" in (info.value.hint or "").lower()
    shutil.rmtree(tmp_path / "work")
    report, _ = _convert(_many_pages(tmp_path / "ok.pdf", 50), tmp_path)
    assert report.rows == 3


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


_YEARS = [["Region", "2023", "2024"], *_ROWS[1:]]


@pytest.mark.parametrize("bordered", [True, False])
def test_p2_blank_corner_header_survives_section_rows(tmp_path: Path, bordered: bool) -> None:
    # Review of the first fix: a section row ('North' alone) right under a header with a blank
    # corner became the header, and the real header was dropped as if it were a title.
    rows = [["", "Units", "Revenue"], ["North", "", ""], *_ROWS[1:7], ["South", "", ""], *_ROWS[7:]]
    pdf = FPDF()
    pdf.add_page()
    (_bordered_rows if bordered else _borderless_rows)(pdf, rows)
    src = tmp_path / "sections.pdf"
    pdf.output(str(src))
    report, out = _convert(src, tmp_path)
    assert [c.name for c in report.columns] == ["column_1", "units", "revenue"]
    assert [c.type for c in report.columns] == ["string", "int64", "double"]
    assert report.rows == 14
    assert out[0] == {"column_1": "North", "units": None, "revenue": None}
    assert out[1] == {"column_1": "Area0", "units": 100, "revenue": 1000.5}
    assert not any("above the header" in w for w in report.warnings)


def test_p2_title_above_a_header_of_years_is_dropped(tmp_path: Path) -> None:
    # Years as column names look like a data row to the type check, so the title stayed the
    # header ('revenue_by_region_us', 'd_thousands') and the header became data.
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", size=13)
    pdf.set_xy(20, 30)
    pdf.cell(150, 7, "Revenue by region (USD thousands)")
    _borderless_rows(pdf, _YEARS)
    src = tmp_path / "years_titled.pdf"
    pdf.output(str(src))
    report, rows = _convert(src, tmp_path)
    assert [c.name for c in report.columns] == ["region", "c_2023", "c_2024"]
    assert [c.type for c in report.columns] == ["string", "int64", "double"]
    assert report.rows == 12
    assert rows[0] == {"region": "Area0", "c_2023": 100, "c_2024": 1000.5}
    assert any("above the header" in w and "Revenue by region" in w for w in report.warnings)


def test_p2_years_are_names_only_where_the_body_is_not_years() -> None:
    title = ["Projects by year", ""]
    rows = [title, ["Start", "End"], ["2019", "2020"], ["2020", "2021"], ["2021", "2023"]]
    assert pdf_reader._split_captions(rows) == (rows[1:], ["Projects by year"])
    repeated = [["Totals", "", ""], ["Region", "2024", "2024"], *_ROWS[1:5]]  # not distinct
    assert pdf_reader._split_captions(repeated) == (repeated, [])


def _table_pdf(path: Path, rows: list[list[str]], *, bordered: bool, title: str = "") -> Path:
    pdf = FPDF()
    pdf.add_page()
    if title:
        pdf.set_font("Helvetica", size=11)
        if bordered:
            pdf.cell(150, 7, title, border=1)  # one merged cell across the table
            pdf.ln()
        else:
            pdf.set_xy(20, 30)
            pdf.cell(150, 6, title)
    (_bordered_rows if bordered else _borderless_rows)(pdf, rows)
    pdf.output(str(path))
    return path


_CORNER = ["", "Units", "Revenue"]


@pytest.mark.parametrize("bordered", [True, False], ids=["bordered", "borderless"])
@pytest.mark.parametrize(
    "first", [["Total", "TBD", "9999.00"], ["North", "1204*", "9999.00"]], ids=["tbd", "footnote"]
)
@pytest.mark.parametrize("title", ["", "Quarterly sales"], ids=["untitled", "titled"])
def test_rv1_blank_corner_header_beats_a_wider_first_row(
    tmp_path: Path, bordered: bool, first: list[str], title: str
) -> None:
    # Round-2 review: the header was the widest row above the first data row, so a first body
    # row with text under a numeric column ('TBD', '1204*') outranked a header with a blank
    # corner: columns total/tbd/c_9999_00, the 'Total' row gone, the header reported as a title.
    # A row holding a value under a typed column is body; it never names the columns. Titled:
    # the other direction, the title above that header is still dropped.
    rows = [_CORNER, first, *_ROWS[1:]]
    src = _table_pdf(tmp_path / "corner.pdf", rows, bordered=bordered, title=title)
    report, out = _convert(src, tmp_path)
    assert [c.name for c in report.columns] == ["column_1", "units", "revenue"]
    assert report.rows == 13
    assert out[0]["column_1"] == first[0]
    assert out[0]["revenue"] == 9999.0
    assert out[1]["column_1"] == "Area0"
    dropped = [w for w in report.warnings if "above the header" in w]
    assert dropped == ([f"dropped 1 line(s) above the header: {title!r}"] if title else [])


@pytest.mark.parametrize("bordered", [True, False], ids=["bordered", "borderless"])
def test_rv1_text_only_first_row_does_not_outrank_a_blank_corner_header(
    tmp_path: Path, bordered: bool
) -> None:
    # The same with no value at all in the first body row: it is as wide as the body and wider
    # than the header, and still not the header.
    rows = [_CORNER, ["Total", "TBD", "TBD"], *_ROWS[1:]]
    report, out = _convert(_table_pdf(tmp_path / "t.pdf", rows, bordered=bordered), tmp_path)
    assert [c.name for c in report.columns] == ["column_1", "units", "revenue"]
    assert report.rows == 13
    assert out[0]["column_1"] == "Total"
    assert not any("above the header" in w for w in report.warnings)


@pytest.mark.parametrize(
    "title",
    [
        # Cut into three cells, the last one '024': a number under Revenue.
        "Quarterly sales by region and product line for fiscal year 2024",
        # Cut into three text cells: 'Sales by region and product l', 'ine for the ...', ...
        "Sales by region and product line for the fiscal year, in US dollars",
    ],
    ids=["number-cut", "text-cut"],
)
def test_rv2_title_as_wide_as_a_borderless_table_is_not_the_header(
    tmp_path: Path, title: str
) -> None:
    # Round-2 review: text alignment cuts a long title at the column edges, so it was as wide
    # as the body and the caption check gave up: columns quarterly_sales_by_region_and,
    # product_line_for_fiscal_year_2, c_024, every column string, the header a data row.
    src = _table_pdf(tmp_path / "wide_title.pdf", _ROWS, bordered=False, title=title)
    report, rows = _convert(src, tmp_path)
    assert [c.name for c in report.columns] == ["region", "units", "revenue"]
    assert [c.type for c in report.columns] == ["string", "int64", "double"]
    assert report.rows == 12
    assert rows[0] == {"region": "Area0", "units": 100, "revenue": 1000.5}
    dropped = [w for w in report.warnings if "above the header" in w]
    assert len(dropped) == 1
    assert title[:20] in dropped[0]


def test_rv2_title_over_a_numeric_column_is_dropped(tmp_path: Path) -> None:
    # Aligned with the Units column, so the alignment pass reads ['', 'Sales', ''] as a row:
    # text under one numeric column, where the header has text under both.
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.set_xy(70, 30)
    pdf.cell(40, 6, "Sales")
    _borderless_rows(pdf, _ROWS)
    src = tmp_path / "over_units.pdf"
    pdf.output(str(src))
    report, rows = _convert(src, tmp_path)
    assert [c.name for c in report.columns] == ["region", "units", "revenue"]
    assert report.rows == 12
    assert rows[0] == {"region": "Area0", "units": 100, "revenue": 1000.5}
    assert any("above the header: 'Sales'" in w for w in report.warnings)


def test_rv2_header_choice_on_rows() -> None:
    body = _ROWS[1:]
    header = _ROWS[0]
    # A row of text under every typed column, above another such row: the upper one names the
    # columns ('pending' rows are body), unless it is running text cut by the alignment pass.
    pending = [header, ["North", "pending", "pending"], *body]
    assert pdf_reader._split_captions(pending) == (pending, [])
    title = ["Sales by region and product l", "ine for the fiscal year, in US d", "ollars"]
    titled = [title, header, *body]
    assert pdf_reader._split_captions(titled) == (titled, [])  # no positions: unclear, kept
    assert pdf_reader._split_captions(titled, running={0}.__contains__) == (
        [header, *body],
        [" ".join(title)],
    )


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


# ---------------------------------------------------------------- RV5: table-search cost


def _ruled_grid(path: Path, lines: int) -> Path:
    # `lines` long ruled lines each way, 4 pt apart (more than pdfplumber's 3 pt snap).
    ops = [b"BT /F1 10 Tf 20 770 Td (x) Tj ET", b"0.1 w"]
    for i in range(lines):
        at = 20 + 4 * i
        ops.append(b"20 %d m %d %d l S" % (at, 20 + 4 * lines, at))
        ops.append(b"%d 20 m %d %d l S" % (at, at, 20 + 4 * lines))
    path.write_bytes(_raw_pdf(_PAGE % b"5 0 R", content=b"\n".join(ops)))
    return path


def test_rv5_ruled_grid_is_refused_before_the_cell_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Round-2 review: 140 ruled lines each way in a 2 KB page took 5.6 s in pdfplumber's cell
    # search (for each crossing it scans every later one), growing as lines**4 toward the
    # convert deadline with memory flat, far inside the object budget. 120 x 120 lines are
    # 14,400 crossings: refused, unpaid, before that search runs.
    from pdfplumber import table as plumber_table

    def cell_search(_: object) -> None:
        raise AssertionError("the cell search ran")

    src = _ruled_grid(tmp_path / "grid.pdf", 120)
    assert src.stat().st_size < 3_000
    monkeypatch.setattr(plumber_table, "intersections_to_cells", cell_search)
    with pytest.raises(InputTooLarge, match="page 1 rules a grid of 14,400 line crossings") as info:
        _convert(src, tmp_path)
    assert info.value.http_status == 413
    assert "pdf_pages to skip page 1" in (info.value.hint or "")


def test_rv5_edge_pairs_are_refused_before_the_pair_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pair loop before it is h x v steps whether or not the edges cross.
    monkeypatch.setattr(pdf_reader, "MAX_GRID_PAIRS", 30 * 30 - 1)
    with pytest.raises(InputTooLarge, match=r"page 1 has more ruling edges .*\(900 edge pairs"):
        _convert(_ruled_grid(tmp_path / "grid.pdf", 30), tmp_path)


def test_rv5_document_budget_sums_the_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pages each under the page limit cannot add up to the deadline either.
    pdf = FPDF()
    for _ in range(3):
        pdf.add_page()
        _bordered_rows(pdf, _ROWS)
    src = tmp_path / "three.pdf"
    pdf.output(str(src))
    found: list[int] = []
    charge = pdf_reader._Budget.crossings

    def spy(budget: pdf_reader._Budget, crossings: int) -> None:
        found.append(crossings)
        charge(budget, crossings)

    monkeypatch.setattr(pdf_reader._Budget, "crossings", spy)
    report, _ = _convert(src, tmp_path)
    assert report.rows == 36  # one table, its header repeated on each page
    assert len(found) == 3  # one ruled search a page; no text search once a ruled table is found
    monkeypatch.setattr(pdf_reader, "MAX_DOC_GRID_WORK", found[0] ** 2 + found[1] ** 2)
    with pytest.raises(InputTooLarge, match=r"more table grid .*stopped on page 3"):
        _convert(src, tmp_path)


def test_rv5_dense_real_tables_stay_under_the_limits(tmp_path: Path) -> None:
    # The other direction: an 80 x 20 table, ruled (1,701 crossings) and borderless (3,360 in
    # its text-alignment search), converts as before.
    rows = [[f"c{c}" for c in range(20)]]
    rows += [[f"r{r}"] + [str(r * c) for c in range(1, 20)] for r in range(79)]
    for bordered in (True, False):
        pdf = FPDF(format=(20 * 14 + 20, 80 * 4 + 20))
        pdf.set_auto_page_break(auto=False)
        pdf.add_page()
        pdf.set_font("Helvetica", size=6)
        for j, row in enumerate(rows):
            for k, cell in enumerate(row):
                pdf.set_xy(10 + k * 14, 10 + j * 4)
                pdf.cell(14 if bordered else 13, 4, cell, border=int(bordered))
        src = tmp_path / f"dense_{bordered}.pdf"
        pdf.output(str(src))
        report, out = _convert(src, tmp_path)
        assert (report.rows, len(report.columns)) == (79, 20)
        assert out[-1]["c19"] == 78 * 19


def test_rv5_search_hook_is_idempotent_and_inert_outside_read_pdf() -> None:
    from pdfplumber import table as plumber_table

    before = plumber_table.edges_to_intersections
    pdf_reader._bound_table_search()
    assert plumber_table.edges_to_intersections is before
    edges = [
        {"orientation": "h", "x0": 0, "x1": 10, "top": 5, "bottom": 5},
        {"orientation": "v", "x0": 5, "x1": 5, "top": 0, "bottom": 10},
    ]
    assert pdf_reader._SEARCHING.get() is None
    assert list(plumber_table.edges_to_intersections(edges)) == [(5, 5)]


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


@pytest.mark.parametrize(
    ("page", "extra"),
    [
        (_PAGE % b"6 0 R", [b"6 0 R"]),  # /Contents refers to itself
        (_PAGE.replace(b"<< /Font << /F1 3 0 R >> >>", b"6 0 R") % b"5 0 R", [b"7 0 R", b"6 0 R"]),
        (_PAGE.replace(b"/F1 3 0 R", b"/F1 6 0 R") % b"5 0 R", [b"7 0 R", b"6 0 R"]),
    ],
    ids=["self-reference", "resources", "font"],
)
def test_p5_other_reference_cycles_fail_in_process(
    tmp_path: Path, page: bytes, extra: list[bytes]
) -> None:
    # In-process (isolate_jobs=False) nothing kills a hang, so this test would never return.
    src = tmp_path / "cycle.pdf"
    src.write_bytes(_raw_pdf(page, *extra, content=b"BT /F1 12 Tf 10 10 Td (Hi) Tj ET"))
    with pytest.raises(InvalidInput, match="reference cycle"):
        _convert(src, tmp_path)
    # pdfplumber's close() rebuilds the page list, raises again, and never closes its file: one
    # leaked handle per failed PDF in an API process that runs jobs in-process.
    gc.collect()


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


# ---------------------------------------------------------------- RV4: shared-object DAGs


def _assemble(objs: list[bytes], trailer: bytes = b"") -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R %s>>\n" % (len(objs) + 1, trailer)
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def _dag(first: int, depth: int, node: bytes, leaf: bytes) -> list[bytes]:
    """Objects ``first``.. each naming the next one twice: 2**depth paths through ``depth``."""
    return [node % (n + 1, n + 1) for n in range(first, first + depth - 1)] + [leaf]


_DICT = b"<< /A %d 0 R /B %d 0 R >>"
_LIST = b"[%d 0 R %d 0 R]"
_GRID = [["Region", "Units"], ["North", "7"], ["South", "9"]]


def _shared_dag_pdf(path: Path, case: str, depth: int = 40) -> Path:
    """A bordered 2x3 table (rectangles and text, as fpdf2 draws them) plus one hostile DAG."""
    ops = [b"BT /F2 10 Tf 50 760 Td (x) Tj ET"]
    for j, row in enumerate(_GRID):
        for k, cell in enumerate(row):
            x, y = 50 + 100 * k, 700 - 20 * j
            ops.append(b"%d %d 100 20 re S" % (x, y))
            ops.append(b"BT /F1 10 Tf %d %d Td (%s) Tj ET" % (x + 5, y + 6, cell.encode()))
    data = zlib.compress(b"\n".join(ops), 9)
    catalog = b"<< /Type /Catalog /Pages 2 0 R >>"
    page = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] %s "
    page += b"/Resources << /Font << /F1 3 0 R /F2 %s >> >> /Contents 5 0 R >>"
    font2, extra, trailer, objs = b"3 0 R", b"", b"", list[bytes]()
    if case.startswith("info"):  # pdfplumber.open resolves every /Info value, recursively
        trailer = b"/Info 6 0 R "
        objs = [b"<< /Title (t) /Keywords 7 0 R >>"]
        objs += _dag(7, depth, _DICT if case == "info-dict" else _LIST, b"(leaf)")
    elif case in {"CropBox", "TrimBox", "Rotate"}:  # pdfplumber's Page resolves these
        extra = b"/%s 6 0 R" % case.encode()
        objs = _dag(6, depth, _LIST, b"0")
    elif case == "page-labels":  # pdfminer walks the labels' number tree to count pages
        catalog = b"<< /Type /Catalog /Pages 2 0 R /PageLabels 6 0 R >>"
        objs = _dag(6, depth, b"<< /Kids [%d 0 R %d 0 R] >>", b"<< /Nums [0 << /S /D >>] >>")
    elif case == "font-widths":  # pdfminer resolves a CID font's /W whole
        font2 = b"6 0 R"
        objs = [
            b"<< /Type /Font /Subtype /Type0 /BaseFont /X /Encoding /Identity-H "
            b"/DescendantFonts [7 0 R] >>",
            b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /X /CIDSystemInfo "
            b"<< /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /W [1 [8 0 R]] >>",
            *_dag(8, depth, _DICT, b"0"),
        ]
    elif case == "font-bbox":  # ... and a font descriptor's /FontBBox
        font2 = b"6 0 R"
        objs = [
            b"<< /Type /Font /Subtype /TrueType /BaseFont /Xyz /FontDescriptor 7 0 R "
            b"/FirstChar 32 /LastChar 32 /Widths [500] >>",
            b"<< /Type /FontDescriptor /FontName /Xyz /Flags 32 /FontBBox 8 0 R >>",
            *_dag(8, depth, _DICT, b"0"),
        ]
    head = [
        catalog,
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        page % (extra, font2),
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream",
    ]
    path.write_bytes(_assemble(head + objs, trailer))
    return path


# Unread (metadata, page labels) or read one level deep (page boxes): the table converts.
_DAG_SKIPPED = ["info-dict", "info-list", "CropBox", "TrimBox", "Rotate", "page-labels"]
# Read whole by pdfminer's font code: refused once the walk passes MAX_RESOLVED_PARTS.
_DAG_REFUSED = ["font-widths", "font-bbox"]


def _isolated(src: Path, work: Path) -> ConversionReport:
    work.mkdir()
    return run_isolated(
        convert_file,
        src,
        work / "out.parquet",
        filename=src.name,
        options=ConvertOptions(),
        workdir=work,
        limits=Limits(),
        timeout_s=20,
    )


@pytest.mark.parametrize("case", _DAG_SKIPPED + _DAG_REFUSED)
def test_rv4_shared_object_dags_do_not_run_to_the_deadline(tmp_path: Path, case: str) -> None:
    # Round-2 review: a 3 KB /Info whose dictionaries name the next one twice per level, 40
    # levels deep, ran until the deadline: pdfplumber resolves metadata recursively with no
    # visited set, so it walks 2**40 paths (memory stays low; no budget or hop limit helps).
    # Page boxes, the page-label tree and font widths and boxes are walked the same way (or
    # filled the 700 MB job cap). In a child with a deadline, so the unfixed walk fails this
    # test (Timeout, or InputTooLarge from the cap) instead of hanging the suite.
    src = _shared_dag_pdf(tmp_path / f"{case}.pdf", case)
    assert src.stat().st_size < 5_000
    if case in _DAG_REFUSED:
        with pytest.raises(InvalidInput, match="nested object unfolds") as info:
            _isolated(src, tmp_path / "work")
        assert info.value.http_status == 422
        return
    report = _isolated(src, tmp_path / "work")
    assert [c.name for c in report.columns] == ["region", "units"]
    assert pq.read_table(tmp_path / "work" / "out.parquet").to_pylist() == [
        {"region": "North", "units": 7},
        {"region": "South", "units": 9},
    ]


@pytest.mark.parametrize("case", _DAG_SKIPPED + _DAG_REFUSED)
def test_rv4_shared_object_dags_in_process(tmp_path: Path, case: str) -> None:
    # In-process (isolate_jobs=False) nothing would stop the walk; fixed, it is quick.
    src = _shared_dag_pdf(tmp_path / f"{case}.pdf", case)
    started = time.monotonic()
    if case in _DAG_REFUSED:
        with pytest.raises(InvalidInput, match="nested object unfolds"):
            _convert(src, tmp_path)
    else:
        report, rows = _convert(src, tmp_path)
        assert report.rows == 2
        assert rows[0] == {"region": "North", "units": 7}
    assert time.monotonic() - started < 10


def test_rv4_resolve_all_is_pdfminers_with_a_part_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from pdfminer import pdffont, pdftypes
    from pdfminer.pdfparser import PDFSyntaxError
    from pdfminer.pdftypes import PDFObjRef

    class Doc:
        def __init__(self) -> None:
            self.objs: dict[int, object] = {}

        def getobj(self, objid: int) -> object:
            return self.objs[objid]

    assert pdffont.resolve_all is pdftypes.resolve_all  # pdffont imported it by name
    doc = Doc()
    doc.objs = {1: [PDFObjRef(doc, 2), 3], 2: {"w": PDFObjRef(doc, 3)}, 3: 500}
    # Lists copied, dictionaries resolved in place, as pdfminer's own: font code relies on it.
    assert pdftypes.resolve_all(PDFObjRef(doc, 1)) == [{"w": 500}, 3]
    assert doc.objs[2] == {"w": 500}
    monkeypatch.setattr(pdf_reader, "MAX_RESOLVED_PARTS", 100)
    flat = list(range(99))  # the list and its 99 numbers: 100 parts
    assert pdftypes.resolve_all(flat) == flat
    with pytest.raises(PDFSyntaxError, match="more than 100 parts"):
        pdftypes.resolve_all([*flat, 99])
    shared = Doc()  # 20 levels naming the next twice: 2**20 paths, refused after 100 parts
    shared.objs = {n: [PDFObjRef(shared, n + 1)] * 2 for n in range(1, 20)} | {20: 0}
    with pytest.raises(PDFSyntaxError):
        pdftypes.resolve_all(PDFObjRef(shared, 1))


def test_rv4_patches_are_idempotent() -> None:
    from pdfminer import pdftypes
    from pdfplumber import pdf as plumber_pdf

    before = (pdftypes.resolve_all, plumber_pdf.resolve_and_decode)
    pdf_reader._walk_shared_objects_once()
    assert (pdftypes.resolve_all, plumber_pdf.resolve_and_decode) == before


def test_rv4_long_parser_messages_are_cut() -> None:
    message = pdf_reader._reason(ValueError("Bounding box: " + "[" * 100_000))
    assert len(message) < 400
    assert message.startswith("Bounding box: [[")


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


def test_p6_borderless_page_after_a_bordered_page_is_named_and_reachable(
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
    # The bordered table wins, as before (text alignment also turns contents pages into
    # "tables"); the page that has none is named, with the pdf_pages value that searches it.
    assert report.rows == 2
    assert report.available_sheets == ["table 0: page 1, 2 rows x 2 columns"]
    assert any(
        "page(s) 2 have no ruled table" in w and "pdf_pages='2'" in w for w in report.warnings
    )
    shutil.rmtree(tmp_path / "work")
    report, rows = _convert(src, tmp_path, pdf_pages="2")
    assert report.rows == 12
    assert rows[0] == {"region": "Area0", "units": 100, "revenue": 1000.5}
    assert any("text alignment" in w for w in report.warnings)


def test_p6_text_pass_stops_once_a_bordered_table_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Running text alignment on every page without a ruled table took a 300-page report with
    # one bordered table from 30 s to 76 s (review of the first fix): it costs ~3x the ruled pass.
    searched: list[int] = []
    aligned = pdf_reader._aligned_tables

    def spy(page: Any, number: int) -> Any:
        searched.append(number)
        return aligned(page, number)

    monkeypatch.setattr(pdf_reader, "_aligned_tables", spy)
    pdf = FPDF()
    pdf.add_page()
    _borderless_rows(pdf, _ROWS)  # page 1: searched, before any bordered table is known
    pdf.add_page()
    _bordered_rows(pdf, _ROWS[:4])
    for _ in range(3):
        pdf.add_page()
        _borderless_rows(pdf, _ROWS)
    src = tmp_path / "report.pdf"
    pdf.output(str(src))
    report, rows = _convert(src, tmp_path)
    assert searched == [1]
    assert report.rows == 3
    assert rows[0] == {"region": "Area0", "units": 100, "revenue": 1000.5}
    assert report.available_sheets == ["table 0: page 2, 3 rows x 3 columns"]
    assert any("page(s) 1,3-5 have no ruled table" in w for w in report.warnings)


def test_p6_span_is_a_valid_pdf_pages_value() -> None:
    assert pdf_reader._span([2]) == "2"
    assert pdf_reader._span([1, 3, 4, 5, 9, 10]) == "1,3-5,9-10"
    assert parse_pages(pdf_reader._span([1, 3, 4, 5, 9, 10]), 10) == [1, 3, 4, 5, 9, 10]


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


def test_p7_year_tables_on_consecutive_pages_stay_apart(tmp_path: Path) -> None:
    # Review of the first fix: 'Department, 2023, 2024' on page 2 reads as a data row under the
    # page-1 table's numeric columns, so a second table was merged into the first as body.
    pdf = FPDF()
    pdf.add_page()
    _bordered_rows(pdf, _YEARS[:6])
    pdf.add_page()
    _bordered_rows(
        pdf, [["Department", "2023", "2024"], ["Sales", "7", "1.50"], ["Ops", "9", "2.50"]]
    )
    src = tmp_path / "two_year_tables.pdf"
    pdf.output(str(src))
    report, rows = _convert(src, tmp_path)
    assert report.available_sheets == [
        "table 0: page 1, 5 rows x 3 columns",
        "table 1: page 2, 2 rows x 3 columns",
    ]
    assert all(r["region"].startswith("Area") for r in rows)
    shutil.rmtree(tmp_path / "work")
    report, rows = _convert(src, tmp_path, pdf_table=1)
    assert [c.name for c in report.columns] == ["department", "c_2023", "c_2024"]
    assert rows == [
        {"department": "Sales", "c_2023": 7, "c_2024": 1.5},
        {"department": "Ops", "c_2023": 9, "c_2024": 2.5},
    ]


def test_p7_year_table_continuing_without_its_header_is_merged(tmp_path: Path) -> None:
    pdf = FPDF()
    pdf.add_page()
    _bordered_rows(pdf, _YEARS[:8])
    pdf.add_page()
    _bordered_rows(pdf, _YEARS[8:])
    src = tmp_path / "years_cont.pdf"
    pdf.output(str(src))
    report, rows = _convert(src, tmp_path)
    assert report.rows == 12
    assert {"region": "Area7", "c_2023": 107, "c_2024": 1049.5} in rows
    assert len(report.available_sheets or []) == 1


def _spill(path: Path, first: list[list[str]], rest: list[list[str]]) -> Path:
    pdf = FPDF()
    pdf.add_page()
    _bordered_rows(pdf, first)
    pdf.add_page()
    _bordered_rows(pdf, rest)
    pdf.output(str(path))
    return path


def test_rv3_last_row_spilling_onto_the_next_page_is_merged(tmp_path: Path) -> None:
    # Round-2 review: a continuation of one row (the most common spill) was dropped as "not a
    # table" before merging, so Area11 was lost, and the warning sent the caller to page 2,
    # which alone yields nothing.
    report, rows = _convert(_spill(tmp_path / "spill.pdf", _ROWS[:12], _ROWS[12:]), tmp_path)
    assert report.rows == 12
    assert rows[-1] == {"region": "Area11", "units": 111, "revenue": 1077.5}
    assert report.available_sheets == ["table 0: pages 1-2, 12 rows x 3 columns"]
    assert any("continues across 2 pages" in w for w in report.warnings)
    assert not any("no ruled table" in w for w in report.warnings)


@pytest.mark.parametrize(
    "box",
    [["Note", "see", "appendix"], ["Area11", "111"]],
    ids=["text-under-numbers", "other-width"],
)
def test_rv3_one_row_box_that_is_not_more_body_stays_out(tmp_path: Path, box: list[str]) -> None:
    report, rows = _convert(_spill(tmp_path / "box.pdf", _ROWS[:12], [box]), tmp_path)
    assert report.rows == 11
    assert report.available_sheets == ["table 0: page 1, 11 rows x 3 columns"]
    assert all(r["region"] != box[0] for r in rows)
    # The box is no table: page 2 is named as one without a usable ruled table, as before.
    assert any("page(s) 2 have no ruled table" in w for w in report.warnings)


def test_rv3_one_row_pieces_never_start_a_table(tmp_path: Path) -> None:
    pdf = FPDF()
    pdf.add_page()
    _bordered_rows(pdf, [["Invoice", "1042"]])  # a 1-row box, then a borderless table
    _borderless_rows(pdf, _ROWS, y0=60)
    src = tmp_path / "box_then_table.pdf"
    pdf.output(str(src))
    report, rows = _convert(src, tmp_path)
    assert report.rows == 12
    assert rows[0] == {"region": "Area0", "units": 100, "revenue": 1000.5}
    assert any("text alignment" in w for w in report.warnings)


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
