"""Round 4 of the final data review: what sits at the top of a page after the first.

A regression-only review of the round 3 commits (against 07fa450, which split each of these
files with a 'found N tables' warning, or read them whole) found three 200s for wrong data:

- PS1: a different table of the same width on the next page, under a two-line header whose
  first line groups columns ('', 'Revenue', '', 'Income', '') and whose second repeats years
  ('Region', '2024', '2023', '2024', '2023'), was merged into the first: only distinct years were
  a header of years, so the years line was a data row and the group line, one line above
  "body", was dropped as a caption. 51 rows for 30 (the years line among them), warned only as
  'dropped 1 line(s) above the header: 'Revenue Income'', and pdf_table=1 no longer existed. A
  sparse one-line header ('Region', '', 'Total', '', '') went the same way: 50 rows.
- PS2: a header of plain numbers over integer columns (a Likert scale 'Question', '1'..'5';
  weeks 'Store', '1'..'4'), printed on every page as fpdf2 and most report writers do, reads as
  data (``_headless``), and an empty header row matched no repeat, so each page added it as a
  data row: 123 rows for 120. header=true, the remedy the warning names, took the first copy
  as the names and kept the others as data, with no warning.
- PS3: a short title printed on continuation pages only ('Page 2', 'Sheet 2 of 4', 'Brought
  forward', 'Sales ledger p2' under a page-1 'Sales ledger') is shaped like a section row
  ('North') when it falls in that column, so it was a data row on every page: 127 rows for 124.

Found while fixing them, each a 200 at 7759739 as well: two tables with no header row under
short titles ('Table 1', 'Table 2') merged; a different table under a label over a line of
numbers ('Region' / '', '1', '2', '3', '4') merged, the numbers a data row; a header printed on
every page that holds numbers was offered as data ('pass header=false'); and a row spilled onto
the last page lost when its last value ended left of the column's edge on page 1.

Round 4b, probing the first round-4 fix (d4d0ad0) the same way: a different table without a
header row, under a title of its own, was merged into the one above, wherever a title cut into
the columns was dropped above a page (any such title merged), the title had the form of page
1's ('Table 2' after 'Table 1'), named a number that was the page's ('Table 2' on page 2), or
was printed atop the next page too (a second table over two pages). Only 'continued', the line
dropped above page 1, or a page label ('Page 2', 'Ledger p2', 'Sheet 2 of 4') now tie a line to
the table above; 'Brought forward' atop continuation pages only splits, as at 07fa450.

A split keeps the 'found N tables' warning and every table reachable with pdf_table; a merge
of a different table, or a title kept as a row, is silent wrong data. Where nothing tells the
cases apart, the page splits, as at 07fa450.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

import pytest
from fpdf import FPDF
from fpdf.fonts import FontFace

import aimart_testkit as kit
from aimart_parquet.engine.readers import pdf as pdf_reader

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

Rows = list[list[str]]
Cell = str | tuple[str, int]  # text, or (text, columns it spans)


def _tables(tables: list[tuple[list[list[Cell]], Rows]], *, ruled: bool) -> bytes:
    """Each table on its own page: (header lines, body). Header cells may span columns."""
    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_font("Helvetica", size=9)
    borders: dict[str, Any] = {} if ruled else {"borders_layout": "NONE"}
    for head, body in tables:
        pdf.add_page()
        style = FontFace(emphasis="BOLD")
        with pdf.table(headings_style=style, num_heading_rows=len(head), **borders) as table:
            for values in [*head, *body]:
                row = table.row()
                for value in values:
                    if isinstance(value, tuple):
                        row.cell(value[0], colspan=value[1])
                    else:
                        row.cell(value)
    return bytes(pdf.output())


def _found(page: int, rows: Rows, captions: list[str] | None = None) -> pdf_reader._Found:
    return pdf_reader._Found(page, rows, "text alignment", list(captions or []))


def _warned_split(warnings: list[str], rows: int) -> bool:
    return any(w.startswith(f"found 2 tables; used table 0: page 1, {rows} rows") for w in warnings)


# ---------------------------------------------------------------- PS1: a header of repeated years


def _amount(rnd: random.Random) -> str:
    return f"{rnd.uniform(100, 9999):,.2f}"


YEARS = ["Region", "2024", "2023", "2024", "2023"]
GROUPS: list[list[Cell]] = [
    ["", ("Revenue", 2), ("Income", 2)],
    ["", ("Year ended 31 December", 4)],
    ["", ("Three months", 2), ("Nine months", 2)],
]


def _segments_then_regions(head: list[list[Cell]], *, ruled: bool) -> bytes:
    rnd = random.Random(11)
    first = [[f"Segment{i}", *(_amount(rnd) for _ in range(4))] for i in range(1, 31)]
    second = [[f"Region{i}", *(_amount(rnd) for _ in range(4))] for i in range(1, 21)]
    return _tables([([["Segment", "Q1", "Q2", "Q3", "Q4"]], first), (head, second)], ruled=ruled)


@pytest.mark.parametrize("ruled", [False, True], ids=["borderless", "ruled"])
@pytest.mark.parametrize(
    "head",
    [[GROUPS[0], YEARS], [GROUPS[1], YEARS], [GROUPS[2], YEARS], [["Region", "", "Total", "", ""]]],
    ids=["revenue-income", "year-ended", "three-nine-months", "sparse-header"],
)
def test_a_new_table_under_a_grouped_or_sparse_header_is_not_merged(
    tmp_path: Path, ruled: bool, head: list[list[Cell]]
) -> None:
    # 9e46d7e: 51 rows (50 for the sparse header) under segment/q1..q4, the years line
    # ('Region', 2024.0, 2023.0, 2024.0, 2023.0) a data row, and no 'found 2 tables'.
    data = _segments_then_regions(head, ruled=ruled)
    result = kit.convert_bytes(tmp_path, "two.pdf", data)
    assert result.rows["segment"] == [f"Segment{i}" for i in range(1, 31)]
    assert _warned_split(result.report.warnings, 30), result.report.warnings
    assert not any("continues across" in w for w in result.report.warnings)
    other = kit.convert_bytes(tmp_path, "two.pdf", data, pdf_table=1)
    first = other.rows[other.report.columns[0].name]
    assert first[-20:] == [f"Region{i}" for i in range(1, 21)]


@pytest.mark.parametrize("ruled", [False, True], ids=["borderless", "ruled"])
def test_a_header_of_repeated_years_names_the_columns(tmp_path: Path, ruled: bool) -> None:
    # 07fa450 and 9e46d7e alike: 21 rows, the years line a data row ('Region', 2024.0, ...)
    # under columns named after the group line (column_1, revenue, column_3, income, ...).
    rnd = random.Random(11)
    body = [[f"Region{i}", *(_amount(rnd) for _ in range(4))] for i in range(1, 21)]
    result = kit.convert_bytes(
        tmp_path, "one.pdf", _tables([([GROUPS[0], YEARS], body)], ruled=ruled)
    )
    report = result.report
    assert result.rows["region"] == [f"Region{i}" for i in range(1, 21)]
    assert [c.type for c in report.columns] == ["string", "double", "double", "double", "double"]
    assert "dropped 1 line(s) above the header: 'Revenue Income'" in report.warnings


AMOUNTS = [["Segment", "Q1", "Q2", "Q3", "Q4"]] + [
    [f"Segment{i}", f"{i},234.50", f"{i},100.25", f"{i}.75", f"{i},000.00"] for i in range(1, 9)
]
REGIONS = [[f"Region{i}", f"{i},111.50", f"{i},222.25", f"{i}.75", f"{i},333.00"] for i in range(5)]


@pytest.mark.parametrize(
    ("line", "names"),
    [
        (["", "Revenue", "", "Income", ""], YEARS),  # a group line over repeated years
        (["Region", "", "", "", ""], ["Store", "1", "2", "3", "4"]),  # a header of plain numbers
        (["Region", "", "Total", "", ""], None),  # a sparse header over typed columns
        (["", "Year ended 31 December", "", "", ""], None),
    ],
    ids=["group-over-years", "line-over-numbers", "sparse-header", "label-over-typed"],
)
def test_merge_a_line_over_a_header_of_numbers_or_typed_columns_splits(
    line: list[str], names: list[str] | None
) -> None:
    # The line was one line above body, so it was dropped as a caption: a different table
    # merged in, its header of numbers a data row. Under it, numbers not written like the body
    # ('2024' over amounts like '1,234.50') are names; text over a typed column is a header.
    second = [line, *([names] if names else []), *REGIONS]
    tables = pdf_reader._merge([_found(1, AMOUNTS), _found(2, second)])
    assert [t.header for t in tables] == [AMOUNTS[0], line]
    assert tables[0].rows == AMOUNTS[1:]


# ---------------------------------------------------------------- PS2: a numeric header per page


def _repeated(path: Path, header: list[str], rows: Rows, *, ruled: bool) -> bytes:
    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_page()
    pdf.set_font("Helvetica", size=9)
    borders: dict[str, Any] = {} if ruled else {"borders_layout": "NONE"}
    style = FontFace(emphasis="BOLD")
    with pdf.table(headings_style=style, repeat_headings=1, **borders) as table:
        for values in [header, *rows]:
            row = table.row()
            for value in values:
                row.cell(value)
    return bytes(pdf.output())


@pytest.mark.parametrize("ruled", [False, True], ids=["borderless", "ruled"])
@pytest.mark.parametrize(
    ("header", "label", "n", "names"),
    [
        (["Question", "1", "2", "3", "4", "5"], "Q", 120, ["question", "c_1", "c_2", "c_3"]),
        (["Store", "1", "2", "3", "4"], "Store", 100, ["store", "c_1", "c_2", "c_3"]),
    ],
    ids=["likert", "weeks"],
)
def test_a_numeric_header_on_every_page_is_the_header(
    tmp_path: Path, ruled: bool, header: list[str], label: str, n: int, names: list[str]
) -> None:
    # 6b5a217 and 9e46d7e: 123 rows for 120, ('Question', 1, 2, 3, 4, 5) a data row at the top
    # of every page under columns column_1..column_6; with header=true, 122 rows, no warning.
    rnd = random.Random(3)
    body = [[f"{label}{i}"] + [str(rnd.randrange(0, 400)) for _ in header[1:]] for i in range(n)]
    data = _repeated(tmp_path, header, body, ruled=ruled)
    for options in ({}, {"header": True}):
        result = kit.convert_bytes(tmp_path, "scale.pdf", data, **options)
        report = result.report
        assert [c.name for c in report.columns][:4] == names
        assert result.rows[names[0]] == [r[0] for r in body]
        assert all(c.type == "int64" for c in report.columns[1:])
        assert "merged a table that continues across 3 pages" in report.warnings
        # Printed atop every page, it is names: neither 'no header row' nor 'header=false'.
        assert not any(w.startswith(("no header row", "the header row")) for w in report.warnings)


LIKERT = ["Question", "1", "2", "3"]
SCORES = [[f"Q{i}", str(i), str(2 * i), str(3 * i)] for i in range(1, 13)]


def test_merge_a_first_row_on_every_page_is_the_header() -> None:
    def page(n: int, rows: Rows) -> pdf_reader._Found:
        return _found(n, [["", "", "", ""], LIKERT, *rows])  # as ``_headless`` reads it

    (table,) = pdf_reader._merge([page(1, SCORES[:4]), page(2, SCORES[4:8]), page(3, SCORES[8:])])
    assert table.header == LIKERT
    assert table.rows == SCORES
    assert table.pages == [1, 2, 3]
    # The counts behind the continuation rules no longer hold the header row.
    assert table.typed() == [1, 2, 3]
    assert table.written[0] == {"Q0"}
    # Names, as any header printed again is: no offer to read them as data.
    assert table.repeated
    assert pdf_reader._settle_header(table, None) == []
    assert table.header == LIKERT
    # A page that judged the same row a header (its body written unlike page 1's) repeats it.
    tables = pdf_reader._merge([page(1, SCORES[:4]), _found(2, [LIKERT, *SCORES[4:]])])
    assert [(t.header, t.rows) for t in tables] == [(LIKERT, SCORES)]
    # One page's first row is no header: a table without one keeps it as data.
    (table,) = pdf_reader._merge([page(1, SCORES[:4]), _found(2, [["", "", "", ""], *SCORES[4:]])])
    assert table.header == ["", "", "", ""]
    assert table.rows == [LIKERT, *SCORES]
    # Numbers alone atop two pages name nothing: as likely data (a sparse matrix), kept.
    zeros = [["0", "0", "0"], ["1", "0", "2"], ["0", "3", "0"]]
    (table,) = pdf_reader._merge(
        [_found(1, [["", "", ""], *zeros]), _found(2, [["", "", ""], *zeros[:1], *zeros[2:]])]
    )
    assert table.header == ["", "", ""]
    assert table.rows == [*zeros, zeros[0], zeros[2]]


def test_a_repeated_header_naming_nothing_keeps_the_warning() -> None:
    # A date alone atop every page (r2 C_date_title.pdf) is a title taken as the header, not
    # names: the warning that it holds a date as the data does stays, as at 7759739.
    body = [[str(i), f"2025-01-{i:02d}", f"{i}.50"] for i in range(1, 9)]
    date = ["2025-03-31", "", ""]
    (table,) = pdf_reader._merge([_found(1, [date, *body[:4]]), _found(2, [date, *body[4:]])])
    assert table.repeated
    (warning,) = pdf_reader._settle_header(table, None)
    assert warning.startswith("the header row ('2025-03-31', '', '') holds numbers or dates")


def test_header_true_drops_every_copy_of_the_names() -> None:
    table = pdf_reader._Table(header=["", "", "", ""], rows=[LIKERT, *SCORES[:3], LIKERT])
    assert pdf_reader._settle_header(table, header=True) == []
    assert table.header == LIKERT
    assert table.rows == SCORES[:3]


# ---------------------------------------------------------------- PS3: a title on later pages


AREA = ["Area", "Units", "Revenue", "Date", "Seq"]
SECTIONS = {1: "North", 30: "South", 70: "East", 100: "West"}


def _area_row(i: int, rnd: random.Random) -> list[str]:
    return [
        f"Area{i}",
        str(rnd.randrange(1, 900)),
        f"{rnd.uniform(10, 9999):.2f}",
        f"2025-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}",
        str(i),
    ]


def _ledger(
    rows: int,
    *,
    title: Callable[[int], str],
    sections: dict[int, str] | None = SECTIONS,
    footer: str = "",
) -> bytes:
    """A borderless ledger, its header on page 1 only, ``title(page)`` in a 30 mm cell above
    each page (in the Area column, where the section rows are), and ``footer`` at the bottom
    left of each page (text alignment reads it into the Area column too)."""

    class Doc(FPDF):
        def header(self) -> None:
            if line := title(self.page_no()):
                self.set_font("Helvetica", "B", 10)
                self.cell(30, 8, line, new_x="LMARGIN", new_y="NEXT")
                self.set_font("Helvetica", size=9)

        def footer(self) -> None:
            if footer:
                self.set_y(-12)
                self.set_font("Helvetica", size=8)
                self.cell(0, 8, footer)
                self.set_font("Helvetica", size=9)

    rnd = random.Random(5)
    pdf = Doc()
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_page()
    pdf.set_font("Helvetica", size=9)
    style = FontFace(emphasis="BOLD")
    with pdf.table(borders_layout="NONE", repeat_headings=0, headings_style=style) as table:
        line = table.row()
        for name in AREA:
            line.cell(name)
        for i in range(1, rows + 1):
            if sections and i in sections:
                line = table.row()
                for value in [sections[i], "", "", "", ""]:
                    line.cell(value)
            line = table.row()
            for value in _area_row(i, rnd):
                line.cell(value)
    return bytes(pdf.output())


@pytest.mark.parametrize(
    ("title", "dropped"),
    [
        (lambda p: "" if p == 1 else f"Page {p}", "'Page 2'; 'Page 3'; 'Page 4'"),
        (lambda p: "" if p == 1 else f"Sheet {p} of 4", "'Sheet 2 of 4'; 'Sheet 3 of 4';"),
        (lambda p: "Sales ledger" if p == 1 else f"Sales ledger p{p}", "'Sales ledger';"),
    ],
    ids=["page-n", "sheet-n-of-m", "page-1-title-differs"],
)
def test_a_title_on_continuation_pages_only_is_not_a_section_row(
    tmp_path: Path, title: Callable[[int], str], dropped: str
) -> None:
    # 6b5a217 and 9e46d7e: 127 rows for 124, 'Page 2'..'Page 4' rows with every other value
    # null, and the only warning 'merged a table that continues across 4 pages'.
    result = kit.convert_bytes(tmp_path, "ledger.pdf", _ledger(120, title=title))
    report = result.report
    assert [a for a in result.rows["area"] if not a.startswith("Area")] == [*SECTIONS.values()]
    assert [s for s in result.rows["seq"] if s is not None] == list(range(1, 121))
    assert "merged a table that continues across 4 pages" in report.warnings
    (lines,) = [w for w in report.warnings if "above the header" in w]
    assert dropped in lines
    assert not any(w.startswith("found ") for w in report.warnings)


def test_a_title_under_a_footer_read_as_a_row_is_not_a_data_row(tmp_path: Path) -> None:
    # No section rows, but a footer at the left margin is read into the Area column, a
    # text-only row like a section row: 'Sheet 2' and 'Sheet 3' became data rows (125 rows).
    data = _ledger(
        120, title=lambda p: "" if p == 1 else f"Sheet {p}", sections=None, footer="Confidential"
    )
    result = kit.convert_bytes(tmp_path, "ledger.pdf", data)
    assert not any(a.startswith("Sheet") for a in result.rows["area"])
    assert [s for s in result.rows["seq"] if s is not None] == list(range(1, 121))
    assert not any(w.startswith("found ") for w in result.report.warnings)


@pytest.mark.parametrize("rows", [55, 120], ids=["2-pages", "4-pages"])
def test_a_title_on_continuation_pages_that_names_nothing_splits(tmp_path: Path, rows: int) -> None:
    # 'Brought forward' atop pages 2.. only names no page and was never dropped above page 1:
    # nothing ties it to the table on page 1 rather than to a new one (a title printed atop
    # the pages of a second table looks the same), and in the column of the section rows it
    # is shaped like one. The page splits, warned (as at 07fa450), rather than adding it as a
    # data row. d4d0ad0 took a line atop the next page as well as a running title.
    data = _ledger(rows, title=lambda p: "" if p == 1 else "Brought forward")
    result = kit.convert_bytes(tmp_path, "ledger.pdf", data)
    assert any(w.startswith("found 2 tables") for w in result.report.warnings)
    assert "Brought forward" not in result.rows[result.report.columns[0].name]


SECTIONED = [["Area", "Units", "Revenue"], ["North", "", ""]] + [
    [f"Area{i}", str(100 + i), f"{1000 + i}.50"] for i in range(6)
]


def _more(start: int) -> Rows:
    return [[f"Area{i}", str(100 + i), f"{1000 + i}.50"] for i in range(start, start + 3)]


def test_merge_a_section_row_at_a_page_top_is_body_when_another_page_has_no_title() -> None:
    south = ["South", "", ""]
    # Page 3 starts with body: the pages carry no running title, so 'South' is a section row.
    (table,) = pdf_reader._merge(
        [_found(1, SECTIONED), _found(2, [south, *_more(6)]), _found(3, _more(9))]
    )
    assert table.rows == [*SECTIONED[1:], south, *_more(6), *_more(9)]
    # As when a page merged before it did.
    (table,) = pdf_reader._merge(
        [_found(1, SECTIONED), _found(2, _more(6)), _found(3, [south, *_more(9)])]
    )
    assert table.rows == [*SECTIONED[1:], *_more(6), south, *_more(9)]
    # On the last page of two nothing says so: a split, warned, not a title kept as a row.
    tables = pdf_reader._merge([_found(1, SECTIONED), _found(2, [south, *_more(6)])])
    assert [t.header for t in tables] == [SECTIONED[0], south]
    # Nor when the next page starts with a title of its own.
    tables = pdf_reader._merge(
        [
            _found(1, SECTIONED),
            _found(2, [south, *_more(6)]),
            _found(3, _more(9), ["Sales p3"]),
        ]
    )
    assert [t.header for t in tables][:2] == [SECTIONED[0], south]


def test_merge_a_line_that_names_its_page_is_a_title() -> None:
    lines = [("Page 2", 2), ("Sheet 2 of 4", 2), ("p12", 12), ("Ledger, pa ge 3", 3), ("2", 2)]
    for line, page in lines:
        pages = [_found(n, _more(3 * n)) for n in range(2, page)]
        (table,) = pdf_reader._merge(
            [_found(1, SECTIONED), *pages, _found(page, [[line, "", ""], *_more(50)])]
        )
        assert table.captions == [line]
        assert [line, "", ""] not in table.rows
    # 'Page 12' on page 2 names another page, and a number that is the page's names no page
    # ('Table 2', 'Zone 2' on page 2): like a section row, and nothing else says, so a split.
    for line in ["Page 12", "Table 2", "Zone 2"]:
        tables = pdf_reader._merge([_found(1, SECTIONED), _found(2, [[line, "", ""], *_more(6)])])
        assert [t.header for t in tables] == [SECTIONED[0], [line, "", ""]]
    # The same line atop the next page only shows it is that page's title too, whichever
    # table that is: d4d0ad0 took it as a running title of the table above. Now a split, as
    # at 07fa450, the line the header of the second table (pages 2-3), never a data row.
    top = ["Brought forward", "", ""]
    tables = pdf_reader._merge(
        [_found(1, SECTIONED), _found(2, [top, *_more(6)]), _found(3, [top, *_more(9)])]
    )
    assert [(t.header, t.rows) for t in tables] == [
        (SECTIONED[0], SECTIONED[1:]),
        (top, [*_more(6), *_more(9)]),
    ]


# ---------------------------------------------------------------- found while fixing round 4

SEQ = ["Area", "Units", "Revenue", "Date", "Seq"]


def _seq_row(i: int) -> list[str]:
    return [f"Area{i}", str(100 + i), f"{1000 + 7 * i}.25", f"2025-{(i % 12) + 1:02d}-01", str(i)]


def _pages(*pages: tuple[str, Rows, bool], ruled: bool = False) -> bytes:
    """One table per page: (title above it, rows, whether rows[0] is its header)."""
    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_font("Helvetica", size=9)
    borders: dict[str, Any] = {} if ruled else {"borders_layout": "NONE"}
    for title, rows, heading in pages:
        pdf.add_page()
        if title:
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(30, 8, title, new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", size=9)
        head: dict[str, Any] = (
            {"headings_style": FontFace(emphasis="BOLD")}
            if heading
            else {"first_row_as_headings": False}
        )
        with pdf.table(**borders, **head) as table:
            for values in rows:
                row = table.row()
                for value in values:
                    row.cell(value)
    return bytes(pdf.output())


def test_two_tables_without_a_header_row_under_short_titles_are_two(tmp_path: Path) -> None:
    # 7759739: 60 rows, 'Table 2' dropped as a title and the second table merged into the
    # first. Page 1 had no header either, so page 2 lacking one says nothing (07fa450 split).
    first = [_seq_row(i) for i in range(1, 31)]
    second = [_seq_row(i) for i in range(31, 61)]
    data = _pages(("Table 1", first, False), ("Table 2", second, False))
    result = kit.convert_bytes(tmp_path, "two.pdf", data)
    assert len(result.rows[result.report.columns[0].name]) == 30
    assert _warned_split(result.report.warnings, 30), result.report.warnings


@pytest.mark.parametrize("ruled", [False, True], ids=["borderless", "ruled"])
def test_a_new_table_under_a_label_over_numbers_is_not_merged(tmp_path: Path, ruled: bool) -> None:
    # 7759739: 51 rows, the line ('', 1, 2, 3, 4) a data row and 'Region' dropped as a title.
    # Under the line, a row filled unlike any body row ('' under Shop) is a header's second line.
    rnd = random.Random(2)
    first = [["Shop", "Q1", "Q2", "Q3", "Q4"]] + [
        [f"Shop{i}", *(str(rnd.randrange(1, 900)) for _ in range(4))] for i in range(1, 31)
    ]
    second = [["Region", "", "", "", ""], ["", "1", "2", "3", "4"]] + [
        [f"Region{i}", *(str(rnd.randrange(1, 900)) for _ in range(4))] for i in range(1, 21)
    ]
    data = _pages(("", first, True), ("", second, True), ruled=ruled)
    result = kit.convert_bytes(tmp_path, "two.pdf", data)
    assert result.rows["shop"] == [f"Shop{i}" for i in range(1, 31)]
    assert _warned_split(result.report.warnings, 30), result.report.warnings


def test_merge_a_line_above_body_continues_only_a_table_with_a_header() -> None:
    body = [[f"Area{i}", str(100 + i), f"{1000 + i}.50"] for i in range(6)]
    more = [[f"Area{i}", str(100 + i), f"{1000 + i}.50"] for i in range(6, 9)]
    for header in (["", "", ""], ["Table 1", "", ""]):  # ``_headless``, or a title kept
        tables = pdf_reader._merge(
            [_found(1, [header, *body]), _found(2, [["Page 2", "", ""], *more])]
        )
        assert [t.rows for t in tables] == [body, more]
    # A line that says so still continues it.
    (table,) = pdf_reader._merge(
        [_found(1, [["", "", ""], *body]), _found(2, [["(continued)", "", ""], *more])]
    )
    assert table.rows == [*body, *more]


def test_a_header_of_codes_on_every_page_is_not_offered_as_data(tmp_path: Path) -> None:
    # 7759739 warned "pass header=false if it is the first data row": a header printed on
    # every page is names (07fa450 had no such warning).
    rnd = random.Random(4)
    rows = [[f"Item{i}", *(f"{rnd.uniform(1, 999):.2f}" for _ in range(3))] for i in range(120)]
    data = _repeated(tmp_path, ["Account", "4000", "5000", "6000"], rows, ruled=False)
    result = kit.convert_bytes(tmp_path, "codes.pdf", data)
    assert [c.name for c in result.report.columns] == ["account", "c_4000", "c_5000", "c_6000"]
    assert result.rows["account"] == [r[0] for r in rows]
    assert not any("header=false" in w for w in result.report.warnings)


def test_rows_spilled_onto_the_last_page_keep_a_narrow_last_column(tmp_path: Path) -> None:
    # Every version: the spilled row's last value ended left of the edge the bold 'Seq' header
    # set on page 1, the row came back four columns wide, and was lost: 35 rows for 36.
    data = _pages(
        ("", [SEQ, *(_seq_row(i) for i in range(1, 36))], True), ("", [_seq_row(36)], False)
    )
    result = kit.convert_bytes(tmp_path, "spill.pdf", data)
    assert result.rows["seq"] == list(range(1, 37))
    assert "merged a table that continues across 2 pages" in result.report.warnings


def test_spilled_rows_under_a_section_row_on_the_last_page_of_two_split(tmp_path: Path) -> None:
    # Nothing tells 'South' from a title printed on that page only: a warned split, the row
    # reachable as table 1, never added under the title or dropped without a word.
    first = [SEQ, ["North", "", "", "", ""], *(_seq_row(i) for i in range(1, 36))]
    data = _pages(("", first, True), ("", [["South", "", "", "", ""], _seq_row(36)], False))
    result = kit.convert_bytes(tmp_path, "spill.pdf", data)
    assert result.rows["seq"] == [None, *range(1, 36)]
    assert _warned_split(result.report.warnings, 36), result.report.warnings
    other = kit.convert_bytes(tmp_path, "spill.pdf", data, pdf_table=1)
    assert other.report.rows == 1
    assert other.rows[other.report.columns[0].name] == ["Area36"]


# ------------------------------------------- round 4b: a title nothing ties to the table above

WIDE_SALES = "Table 1. Sales by region for the fiscal year, all business units, in US dollars"
WIDE_COSTS = "Table 2. Operating costs by region for the fiscal year, every cost centre, in euros"
STATIC = "Regional sales ledger, all business units, fiscal year, amounts in US dollars"


def _titled(*pages: tuple[str, Rows, bool], wide: bool) -> bytes:
    """``_pages``, the title in a cell as wide as the page (cut into the columns) or 30 mm."""
    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=18)
    for title, rows, heading in pages:
        pdf.add_page()
        if title:
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(0 if wide else 30, 8, title, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=9)
        head: dict[str, Any] = (
            {"headings_style": FontFace(emphasis="BOLD")}
            if heading
            else {"first_row_as_headings": False}
        )
        with pdf.table(borders_layout="NONE", **head) as table:
            for values in rows:
                row = table.row()
                for value in values:
                    row.cell(value)
    return bytes(pdf.output())


def _labelled(label: str, start: int, n: int = 30) -> Rows:
    return [[f"{label}{i}", *_seq_row(i)[1:]] for i in range(start, start + n)]


SALES, COSTS, MORE_COSTS = _labelled("Area", 1), _labelled("Cost", 1), _labelled("Cost", 31)


@pytest.mark.parametrize(
    ("pages", "wide"),
    [
        (((WIDE_SALES, SALES, False), (WIDE_COSTS, COSTS, False)), True),
        (
            (
                (WIDE_SALES.replace("year", "year 2023"), SALES, False),
                (
                    WIDE_SALES.replace("Table 1", "Table 2").replace("year", "year 2024"),
                    COSTS,
                    False,
                ),
            ),
            True,
        ),
        (((WIDE_SALES, [SEQ, *SALES], True), (WIDE_COSTS, COSTS, False)), True),
        ((("", [SEQ, *SALES], True), ("Costs", COSTS, False)), False),
        ((("Table 1", [SEQ, *SALES], True), ("Table 2", COSTS, False)), False),
        (
            (
                (WIDE_SALES, [SEQ, *SALES], True),
                (WIDE_COSTS, COSTS, False),
                (WIDE_COSTS, MORE_COSTS, False),
            ),
            True,
        ),
        ((("", SALES, False), (STATIC, COSTS, False)), True),
    ],
    ids=[
        "no-header-rows-two-titles",
        "no-header-rows-titles-differing-in-digits",
        "headed-then-no-header-row",
        "headed-then-short-title",
        "table-1-then-table-2",
        "second-table-over-two-pages",
        "title-on-page-2-only",
    ],
)
def test_a_table_without_a_header_row_under_its_own_title_is_not_merged(
    tmp_path: Path, pages: tuple[tuple[str, Rows, bool], ...], wide: bool
) -> None:
    # d4d0ad0, each a 200 for one table of 60 (90) rows, the second table's rows under the
    # first one's names, warned only as 'merged' and 'dropped N line(s)': a title cut into the
    # columns was dropped above any page, and 'Table 2' on page 2 named its page. Nothing ties
    # such a title to the table above, so the page splits, as at 07fa450.
    data = _titled(*pages, wide=wide)
    result = kit.convert_bytes(tmp_path, "two.pdf", data)
    assert any(w.startswith("found 2 tables") for w in result.report.warnings)
    first = kit.convert_bytes(tmp_path, "two.pdf", data, pdf_table=0)
    assert first.rows[first.report.columns[0].name] == [r[0] for r in SALES]
    second = kit.convert_bytes(tmp_path, "two.pdf", data, pdf_table=1)
    labels = second.rows[second.report.columns[0].name]
    assert labels[-1] == pages[-1][1][-1][0]
    assert not any(str(v).startswith("Area") for v in labels)


def test_a_title_atop_continuation_pages_only_splits(tmp_path: Path) -> None:
    # The same table, its title printed atop pages 2 and 3 only: the layout of
    # 'second-table-over-two-pages' above, so nothing tells them apart and the page splits,
    # warned, with every row reachable (as at 07fa450; d4d0ad0 merged both).
    data = _titled(
        ("", [SEQ, *SALES], True),
        (STATIC, _labelled("Area", 31), False),
        (STATIC, _labelled("Area", 61), False),
        wide=True,
    )
    result = kit.convert_bytes(tmp_path, "ledger.pdf", data)
    assert any(w.startswith("found 2 tables") for w in result.report.warnings)
    first = kit.convert_bytes(tmp_path, "ledger.pdf", data, pdf_table=0)
    assert first.rows["area"] == [r[0] for r in SALES]


def test_a_report_header_atop_every_page_does_not_split_the_table(tmp_path: Path) -> None:
    # 07fa450: 80 of 120 rows, named after the header line. The same line was dropped above
    # page 1, so it is a running title of the table.
    line = "ACME Corporation - annual report 2024 - section 4, regional sales ledger"
    rows = _labelled("Area", 1, 120)

    class Doc(FPDF):
        def header(self) -> None:
            self.set_font("Helvetica", "B", 10)
            self.cell(0, 8, line, new_x="LMARGIN", new_y="NEXT")
            self.set_font("Helvetica", size=9)

    pdf = Doc()
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_page()
    with pdf.table(borders_layout="NONE", headings_style=FontFace(emphasis="BOLD")) as table:
        for values in [SEQ, *rows]:
            row = table.row()
            for value in values:
                row.cell(value)
    result = kit.convert_bytes(tmp_path, "report.pdf", bytes(pdf.output()))
    assert result.rows["area"] == [r[0] for r in rows]
    assert "merged a table that continues across 3 pages" in result.report.warnings
    assert not any(w.startswith("found ") for w in result.report.warnings)


def test_a_section_row_with_the_page_number_in_it_stays_a_section_row(tmp_path: Path) -> None:
    # d4d0ad0 dropped 'Zone 2' atop page 2 as a title naming its page; 'Zone' names no page,
    # and page 3 starts with body, so the pages carry no title and it is a section row.
    blank = ["", "", "", ""]
    data = _pages(
        ("", [SEQ, ["Zone 1", *blank], *_labelled("Area", 1, 35)], True),
        ("", [["Zone 2", *blank], *_labelled("Area", 36)], False),
        ("", _labelled("Area", 66), False),
    )
    result = kit.convert_bytes(tmp_path, "zones.pdf", data)
    labels = [a for a in result.rows["area"] if not a.startswith("Area")]
    assert labels == ["Zone 1", "Zone 2"]
    assert [s for s in result.rows["seq"] if s is not None] == list(range(1, 96))
    assert not any("above the header" in w for w in result.report.warnings)


def test_merge_a_title_over_a_header_of_numbers_splits() -> None:
    # A page label ties a line to the table above only over rows filled like its body: under
    # it, a row with no label ('', '1', '2', '3') is a new table's header of numbers, which
    # would be a data row once the title was dropped. Whether the alignment pass cut the title
    # into the columns (a caption above an empty header row) or kept it in one cell.
    shops = [["Shop", "Q1", "Q2", "Q3"]] + [
        [f"Shop{i}", str(i), str(2 * i), str(3 * i)] for i in range(8)
    ]
    weeks = ["", "1", "2", "3"]
    more = [[f"Region{i}", str(i), str(4 * i), str(5 * i)] for i in range(5)]
    for page in (
        _found(2, [["Page 2", "", "", ""], weeks, *more]),
        _found(2, [["", "", "", ""], weeks, *more], ["Page 2"]),
    ):
        tables = pdf_reader._merge([_found(1, shops), page])
        assert tables[0].rows == shops[1:]
        assert len(tables) == 2
    # Over body, the same title is dropped.
    (table,) = pdf_reader._merge([_found(1, shops), _found(2, [["Page 2", "", "", ""], *more])])
    assert table.rows == [*shops[1:], *more]
    assert table.captions == ["Page 2"]
