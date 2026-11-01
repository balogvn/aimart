"""Regression from the final data review: a running page title cost a borderless table pages.

Every page of a ledger carries the same title line ('ACME Corp - Confidential sales ledger')
and the column header is printed on page 1 only. On page 1 the title was dropped as a caption;
on each continuation page it was the only row above the first data row, so it stayed, became
that page's header, and the page neither repeated page 1's header nor started with a data
row. The table split into one piece per page and the largest won: 80 of 120 rows, columns
named 'acme_corp_confiden', 'tial_sales_ledger', a 200. No pdf_table or pdf_pages value
returned the whole table.

Round 2 of that review, each a 200 with rows missing:

- RV1: dropping the title made the first data row the header of a table with no header row
  (29 of 30 rows, columns 'c_1', 'c_2025_02_02', ...); without a title that row was lost too.
- RV2: a title naming its page ('Ledger p2') or printed only on continuation pages ('Table 1
  (continued)') still split the table: 41 and 79 of 120 rows.
- RV3: the last one or two rows of a borderless table, alone on the last page, were no table
  to text alignment and were lost: 125 of 126 rows.
"""

from __future__ import annotations

import io
import random
from typing import TYPE_CHECKING

import pdfplumber
import pytest
from fpdf import FPDF
from fpdf.fonts import FontFace

import aimart_testkit as kit
from aimart_parquet.engine.readers import pdf as pdf_reader

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

TITLE = "ACME Corp - Confidential sales ledger"
HEADER = ["ID", "Date", "Region", "Amount", "Qty"]


def _ledger(
    rows: int,
    *,
    title: str | Callable[[int], str] = TITLE,
    footer: bool = False,
    cell: float = 0,
    header: bool = True,
) -> bytes:
    """A borderless ledger, ids 1..rows in column 0, its header (if any) on page 1 only."""

    class Doc(FPDF):
        def header(self) -> None:
            line = title(self.page_no()) if callable(title) else title
            if line:
                self.set_font("Helvetica", "B", 10)
                self.cell(cell, 8, line, new_x="LMARGIN", new_y="NEXT")
                self.set_font("Helvetica", size=9)

        def footer(self) -> None:
            if footer:
                self.set_y(-12)
                self.set_font("Helvetica", size=8)
                self.cell(0, 8, f"Page {self.page_no()} of {{nb}}", align="C")
                self.set_font("Helvetica", size=9)

    rnd = random.Random(5)
    pdf = Doc()
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_page()
    pdf.set_font("Helvetica", size=9)
    headings = (
        {"repeat_headings": 0, "headings_style": FontFace(emphasis="BOLD")}
        if header
        else {"first_row_as_headings": False}
    )
    with pdf.table(borders_layout="NONE", **headings) as table:
        if header:
            row = table.row()
            for name in HEADER:
                row.cell(name)
        for i in range(1, rows + 1):
            row = table.row()
            for value in [
                str(i),
                f"2025-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}",
                rnd.choice(["North", "South", "East"]),
                f"{rnd.uniform(10, 9999):.2f}",
                str(rnd.randrange(1, 900)),
            ]:
                row.cell(value)
    return bytes(pdf.output())


@pytest.mark.parametrize(
    ("rows", "footer", "pages"),
    [(120, False, 3), (70, False, 2), (120, True, 3)],
    ids=["three-pages", "two-pages", "with-footer"],
)
def test_a_running_title_does_not_split_the_table(tmp_path: Path, rows, footer, pages) -> None:
    result = kit.convert_bytes(tmp_path, "ledger.pdf", _ledger(rows, footer=footer))
    report = result.report
    assert [c.name for c in report.columns] == ["id", "date", "region", "amount", "qty"]
    assert [c.type for c in report.columns] == ["int64", "date32[day]", "string", "double", "int64"]
    assert result.rows["id"] == list(range(1, rows + 1))  # was 80 of 120 (pages 2-3)
    assert f"merged a table that continues across {pages} pages" in report.warnings
    assert not any(w.startswith("found ") for w in report.warnings)
    (dropped,) = [w for w in report.warnings if "above the header" in w]
    assert dropped.startswith(f"dropped {pages} line(s) above the header: 'ACME Corp")
    assert dropped.count("ACME") == 1  # one line, dropped once a page


def test_a_short_running_title_in_one_column_is_dropped_on_every_page(tmp_path: Path) -> None:
    # 'Ledger' sits inside the ID column: one cell, so positions cannot call it running text.
    # It is the line already dropped above the header on page 1, so it is a title here too.
    result = kit.convert_bytes(tmp_path, "ledger.pdf", _ledger(120, title="Ledger", cell=20))
    assert [c.name for c in result.report.columns] == ["id", "date", "region", "amount", "qty"]
    assert result.rows["id"] == list(range(1, 121))


def test_a_table_without_a_title_is_unchanged(tmp_path: Path) -> None:
    result = kit.convert_bytes(tmp_path, "ledger.pdf", _ledger(120, title="", footer=True))
    assert [c.name for c in result.report.columns] == ["id", "date", "region", "amount", "qty"]
    assert result.rows["id"] == list(range(1, 121))
    assert not any("above the header" in w for w in result.report.warnings)


BODY = [[str(i), f"2025-01-{i:02d}", "North", f"{i}.50", str(i * 3)] for i in range(1, 9)]


NO_HEADER = ["", "", "", "", ""]


def test_one_running_line_above_the_data_is_a_caption() -> None:
    # Round 2 (RV1): this returned BODY, and BODY[0] then became the header in ``_merge``.
    # With no header row left, the rows come back under an empty one.
    cut = ["ACME Corp - Confiden", "tial sales ledger", "", "", ""]
    rows = [cut, *BODY]
    assert pdf_reader._split_captions(rows, running={0}.__contains__) == (
        [NO_HEADER, *BODY],
        ["ACME Corp - Confiden tial sales ledger"],
    )
    assert pdf_reader._split_captions(rows) == (rows, [])  # no positions: unclear, kept


def test_one_header_row_above_the_data_is_never_asked_about() -> None:
    # A header names every typed column: reading positions for it would cost a glyph pass on
    # every page of every borderless table, and could only say no.
    def running(i: int) -> bool:
        raise AssertionError(i)

    rows = [HEADER, *BODY]
    assert pdf_reader._split_captions(rows, running) == (rows, [])


# ---------------------------------------------------------------- RV1: no header row

TYPES = ["int64", "date32[day]", "string", "double", "int64"]
COLUMNS = ["column_1", "column_2", "column_3", "column_4", "column_5"]


@pytest.mark.parametrize(
    ("rows", "title", "pages"),
    [(30, "", 1), (30, TITLE, 1), (120, TITLE, 3), (120, "", 3)],
    ids=["untitled", "titled", "titled-three-pages", "untitled-three-pages"],
)
def test_a_table_with_no_header_row_keeps_its_first_row(
    tmp_path: Path, rows: int, title: str, pages: int
) -> None:
    # Was 29 of 30 (119 of 120) rows, id 1 gone, columns named after its values: 'c_1',
    # 'c_2025_02_02', 'east', 'c_2561_64', 'c_368'. 07fa450 kept every titled row only because
    # the title was the header; the untitled table lost id 1 there too.
    result = kit.convert_bytes(tmp_path, "ledger.pdf", _ledger(rows, title=title, header=False))
    report = result.report
    assert result.rows["column_1"] == list(range(1, rows + 1))
    assert [c.name for c in report.columns] == COLUMNS
    assert [c.type for c in report.columns] == TYPES
    assert (
        "no header row: the first row is data, so the columns are named column_1 to column_5; "
        "pass header=true if it holds the column names"
    ) in report.warnings
    assert not any(w.startswith("found ") for w in report.warnings)
    dropped = [w for w in report.warnings if "above the header" in w]
    assert dropped == (
        [f"dropped {pages} line(s) above the header: 'ACME Corp - Confiden tial sales ledger'"]
        if title
        else []
    )


def test_header_true_takes_the_first_row_as_names(tmp_path: Path) -> None:
    data = _ledger(30, title="", header=False)
    result = kit.convert_bytes(tmp_path, "ledger.pdf", data, header=True)
    assert [c.name for c in result.report.columns][:3] == ["c_1", "c_2025_02_02", "east"]
    assert result.report.rows == 29
    assert not any("header" in w for w in result.report.warnings)


def test_header_false_reads_the_header_row_as_data(tmp_path: Path) -> None:
    result = kit.convert_bytes(tmp_path, "ledger.pdf", _ledger(20, title=""), header=False)
    assert [c.name for c in result.report.columns] == COLUMNS
    assert result.rows["column_1"] == ["ID", *map(str, range(1, 21))]
    assert not any("header" in w for w in result.report.warnings)


def _bordered(path: Path, rows: list[list[str]]) -> Path:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for row in rows:
        for cell in row:
            pdf.cell(50, 7, cell, border=1)
        pdf.ln()
    pdf.output(str(path))
    return path


@pytest.mark.parametrize(
    ("header", "body"),
    [
        (["Account", "4000", "5000"], [[f"R{i}", f"{i}.25", f"{i}.75"] for i in range(1, 7)]),
        (
            ["Item", "2024-12-31", "2023-12-31"],
            [[f"R{i}", f"{i}.25", f"{i}.75"] for i in range(1, 7)],
        ),
    ],
    ids=["account-codes", "period-dates"],
)
def test_a_header_of_codes_or_dates_stays_the_header(
    tmp_path: Path, header: list[str], body: list[list[str]]
) -> None:
    # Values under number columns, but not written like the numbers under them: names. The
    # warning says how to read the row as data instead, as the guess may be wrong.
    src = _bordered(tmp_path / "codes.pdf", [header, *body])
    result = kit.convert_path(src)
    assert result.report.rows == 6
    assert result.rows[result.report.columns[0].name][0] == "R1"
    assert [c.type for c in result.report.columns] == ["string", "double", "double"]
    shown = ", ".join(map(repr, header))
    assert (
        f"the header row ({shown}) holds numbers or dates as the data does; pass header=false "
        "if it is the first data row"
    ) in result.report.warnings


def test_header_choice_on_rows() -> None:
    assert pdf_reader._split_captions(BODY) == ([NO_HEADER, *BODY], [])
    # Written like the body: '-12.50' has the form of '12.50', and 'n/a' is no value.
    first = ["9", "2025-01-09", "North", "-12.50", "n/a"]
    assert pdf_reader._split_captions([first, *BODY]) == ([NO_HEADER, first, *BODY], [])
    codes = ["9", "2025-01-09", "North", "4000", "27"]  # an integer above decimals
    assert pdf_reader._split_captions([codes, *BODY]) == ([codes, *BODY], [])
    # Two lines of running text and no header under them: both are captions.
    title = [
        ["Sales by region and product l", "ine", "", "", ""],
        ["for the fiscal", "year", "", "", ""],
    ]
    assert pdf_reader._split_captions([*title, *BODY], running={0, 1}.__contains__) == (
        [NO_HEADER, *BODY],
        ["Sales by region and product l ine", "for the fiscal year"],
    )


# ---------------------------------------------------------------- RV2: a line per page


def _page_numbered(page: int) -> str:
    return f"Ledger p{page}"


def _continued(page: int) -> str:
    return "" if page == 1 else "Table 1 (continued)"


@pytest.mark.parametrize("cell", [0, 30], ids=["full-width", "one-column"])
@pytest.mark.parametrize(
    ("title", "rows", "pages", "dropped"),
    [
        (_page_numbered, 120, 3, "dropped 3 line(s) above the header: 'Ledger p1'; 'Ledger p2';"),
        (_page_numbered, 70, 2, "dropped 2 line(s) above the header: 'Ledger p1'; 'Ledger p2'"),
        (_page_numbered, 30, 1, "dropped 1 line(s) above the header: 'Ledger p1'"),
        (_continued, 120, 3, "dropped 2 line(s) above the header: 'Table 1 (continued)'"),
        (_continued, 70, 2, "dropped 1 line(s) above the header: 'Table 1 (continued)'"),
    ],
    ids=["page-numbered", "page-numbered-2", "page-numbered-1", "continued", "continued-2"],
)
def test_a_title_that_differs_per_page_does_not_split_the_table(
    tmp_path: Path, title: Callable[[int], str], rows: int, pages: int, dropped: str, cell: float
) -> None:
    # Was 41 of 120 rows (page 2 only, columns 'ledger_p2', 'column_2', ...) and 79 of 120
    # (pages 2-3, 'table_1_continued'): no pdf_table value returned the whole table.
    result = kit.convert_bytes(tmp_path, "ledger.pdf", _ledger(rows, title=title, cell=cell))
    report = result.report
    assert result.rows["id"] == list(range(1, rows + 1))
    assert [c.name for c in report.columns] == ["id", "date", "region", "amount", "qty"]
    assert [c.type for c in report.columns] == TYPES
    if pages > 1:
        assert f"merged a table that continues across {pages} pages" in report.warnings
    assert any(w.startswith(dropped) for w in report.warnings), report.warnings
    assert not any(w.startswith("found ") for w in report.warnings)


def _two_tables(second: list[str]) -> bytes:
    """A 40-row ledger on page 1 and a 30-row table typed like it, headed ``second``, on page 2."""
    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_font("Helvetica", size=9)
    for header, rows, year in ((HEADER, 40, 2025), (second, 30, 2024)):
        pdf.add_page()
        with pdf.table(borders_layout="NONE", headings_style=FontFace(emphasis="BOLD")) as table:
            row = table.row()
            for name in header:
                row.cell(name)
            for i in range(1, rows + 1):
                row = table.row()
                for value in [str(i), f"{year}-01-{i % 28 + 1:02d}", "East", f"{i}.25", str(i)]:
                    row.cell(value)
    return bytes(pdf.output())


@pytest.mark.parametrize(
    "second",
    [
        ["", "Shipped", "Carrier", "Weight", "Boxes"],
        ["Order", "Shipped", "Carrier", "Weight", "Boxes"],
    ],
    ids=["blank-corner", "named"],
)
def test_a_new_table_on_the_next_page_is_not_merged(tmp_path: Path, second: list[str]) -> None:
    # Found checking RV2: a header with a blank corner names fewer typed columns than the
    # first table has, so it was dropped as a title and the 30 shipments became ledger rows.
    data = _two_tables(second)
    result = kit.convert_bytes(tmp_path, "two.pdf", data)
    assert result.rows["id"] == list(range(1, 41))
    found = "found 2 tables; used table 0: page 1, 40 rows x 5 columns (pass pdf_table to choose)"
    assert found in result.report.warnings
    assert not any("above the header" in w for w in result.report.warnings)
    other = kit.convert_bytes(tmp_path, "two.pdf", data, pdf_table=1)
    names = [c.name for c in other.report.columns]
    assert names[1:] == ["shipped", "carrier", "weight", "boxes"]
    assert other.rows[names[0]] == list(range(1, 31))


def _found(page: int, rows: list[list[str]]) -> pdf_reader._Found:
    return pdf_reader._Found(page, rows, "text alignment")


SECTIONS = [["Region", "Units", "Revenue"], ["North", "", ""]] + [
    [f"Area{i}", str(100 + i), f"{1000 + i}.50"] for i in range(6)
]


def test_the_line_above_a_continuation_is_body_when_the_body_holds_its_like() -> None:
    more = [[f"Area{i}", str(100 + i), f"{1000 + i}.50"] for i in range(6, 9)]
    # A section row at the top of page 2, shaped like 'North': body, as it is on page 1.
    (table,) = pdf_reader._merge([_found(1, SECTIONS), _found(2, [["South", "", ""], *more])])
    assert table.rows == [*SECTIONS[1:], ["South", "", ""], *more]
    assert table.captions == []
    # A row holding a value is body too, when its text is text the body holds there. Round 3:
    # a first 'pending' was body as well, but so was a new table's header holding a year
    # ('Region', '2023', 'Change %'), so it now splits (test_final_data_pdf_next_page.py).
    seen = [*SECTIONS, ["Area6", "pending", "1006.50"]]
    pending = ["Area9", "pending", "1009.50"]
    (table,) = pdf_reader._merge([_found(1, seen), _found(2, [pending, *more])])
    assert table.rows == [*seen[1:], pending, *more]
    # A title: dropped and reported. A header naming every typed column: a new table.
    (table,) = pdf_reader._merge([_found(1, SECTIONS), _found(2, [["", "Sales p2", ""], *more])])
    assert table.rows == [*SECTIONS[1:], *more]
    assert table.captions == ["Sales p2"]
    tables = pdf_reader._merge(
        [_found(1, SECTIONS), _found(2, [["City", "Stores", "Sales"], *more])]
    )
    assert [t.header for t in tables] == [SECTIONS[0], ["City", "Stores", "Sales"]]
    # A header with a blank corner over a typed column names fewer typed columns than the
    # first table has, but fills most cells: a new table, not a title.
    ledger = [["ID", "Region", "Units", "Revenue"]] + [
        [str(i), "North", str(100 + i), f"{1000 + i}.50"] for i in range(1, 7)
    ]
    more = [[str(i), "South", str(100 + i), f"{1000 + i}.50"] for i in range(7, 10)]
    corner = ["", "City", "Stores", "Sales"]
    tables = pdf_reader._merge([_found(1, ledger), _found(2, [corner, *more])])
    assert [t.header for t in tables] == [ledger[0], corner]
    (table,) = pdf_reader._merge([_found(1, ledger), _found(2, [["Ledger p2", "", "", ""], *more])])
    assert table.rows == [*ledger[1:], *more]
    assert table.captions == ["Ledger p2"]


def test_tables_without_a_header_row_merge_only_page_to_page() -> None:
    # An empty header row is no header: two such tables pages apart are not one repeated.
    body = [[f"Area{i}", str(100 + i), f"{1000 + i}.50"] for i in range(6)]
    piece = [["", "", ""], *body]
    tables = pdf_reader._merge([_found(1, piece), _found(2, piece), _found(4, piece)])
    assert [t.pages for t in tables] == [[1, 2], [4]]


# ---------------------------------------------------------------- RV3: the last rows alone


def _last_page_lines(data: bytes) -> list[str]:
    with pdfplumber.open(io.BytesIO(data)) as doc:
        return (doc.pages[-1].extract_text() or "").splitlines()


@pytest.mark.parametrize(
    ("rows", "title", "footer", "header", "spilled"),
    [
        (126, "", False, True, 1),
        (127, "", False, True, 2),
        (123, TITLE, False, True, 1),
        (124, TITLE, False, True, 2),
        (126, "", True, True, 1),
        (124, TITLE, True, True, 2),
        (127, "", False, False, 1),
        (125, TITLE, False, False, 2),
    ],
    ids=[
        "one-row",
        "two-rows",
        "titled-one-row",
        "titled-two-rows",
        "footer",
        "titled-footer",
        "no-header-one-row",
        "no-header-titled-two-rows",
    ],
)
def test_the_last_rows_alone_on_the_last_page_are_kept(
    tmp_path: Path, rows: int, title: str, footer: bool, header: bool, spilled: int
) -> None:
    # Was 125 of 126 rows (122 of 124 under the title), a 200 whose only warning was 'merged a
    # table that continues across 3 pages': text alignment needs three words stacked to see a
    # column, so one or two rows on a page are no table to it.
    data = _ledger(rows, title=title, footer=footer, header=header)
    lines = _last_page_lines(data)
    assert len(lines) == spilled + bool(title) + footer  # the layout this test is about
    result = kit.convert_bytes(tmp_path, "ledger.pdf", data)
    report = result.report
    assert result.rows[report.columns[0].name] == list(range(1, rows + 1))
    assert [c.name for c in report.columns] == (
        ["id", "date", "region", "amount", "qty"] if header else COLUMNS
    )
    assert [c.type for c in report.columns] == TYPES
    assert "merged a table that continues across 4 pages" in report.warnings
    assert not any(w.startswith("found ") for w in report.warnings)


def test_a_page_of_prose_after_the_table_adds_no_rows(tmp_path: Path) -> None:
    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_page()
    pdf.set_font("Helvetica", size=9)
    with pdf.table(borders_layout="NONE", headings_style=FontFace(emphasis="BOLD")) as table:
        row = table.row()
        for name in HEADER:
            row.cell(name)
        for i in range(1, 31):
            row = table.row()
            for value in [str(i), f"2025-01-{i % 28 + 1:02d}", "North", f"{i}.50", str(i)]:
                row.cell(value)
    pdf.add_page()
    pdf.multi_cell(0, 5, "Notes. Amounts are in US dollars, 2025 prices.\nTotals: 30 rows.")
    result = kit.convert_bytes(tmp_path, "ledger.pdf", bytes(pdf.output()))
    assert result.rows["id"] == list(range(1, 31))
    assert [c.type for c in result.report.columns] == TYPES
    assert not any("continues across" in w for w in result.report.warnings)


def _spill(page: int, rows: list[list[str]]) -> pdf_reader._Found:
    return pdf_reader._Found(page, rows, "text alignment", spill=True)


LEDGER = [HEADER, ["North", "", "", "", ""], *BODY]


@pytest.mark.parametrize(
    ("rows", "kept"),
    [
        ([["9", "2025-01-09", "North", "9.50", "27"]], 1),
        ([["South", "", "", "", ""], ["9", "2025-01-09", "South", "9.50", "27"]], 2),
        ([["9", "2025-01-09", "North", "9.50", "27"], ["South", "", "", "", ""]], 1),
        ([["9", "2025-01-09", "North", "9.50", "27"], ["", "", "", "", "4"]], 1),
        ([["", "", "", "", "4"]], 0),  # a page number under the Qty column
        ([["", "", "", "", "2025-01-31"]], 0),  # a date alone, under a number column
        ([["9", "", "", "", "27"]], 0),  # no body row is filled like this
        ([["Notes. Amounts are", "in US dollars,", "2025 prices.", "", ""]], 0),
        ([["South", "", "", "", ""]], 0),  # a section row heads data rows
    ],
    ids=[
        "row",
        "section-then-row",
        "row-then-section",
        "row-then-page-number",
        "page-number",
        "lone-date",
        "unlike-shape",
        "prose",
        "section-alone",
    ],
)
def test_rows_read_at_the_column_edges_are_kept_only_when_they_are_body(
    rows: list[list[str]], kept: int
) -> None:
    (table,) = pdf_reader._merge([_found(1, LEDGER), _spill(2, rows)])
    assert table.rows == [*LEDGER[1:], *rows[:kept]]
    assert table.pages == ([1, 2] if kept else [1])


def test_rows_read_at_the_column_edges_never_start_a_table() -> None:
    other = [["Code", "Name", "Total"], ["A1", "Alpha", "12.50"], ["B2", "Beta", "8.25"]]
    (table,) = pdf_reader._merge([_found(1, LEDGER), _spill(2, other)])
    assert table.header == HEADER
    assert table.pages == [1]
