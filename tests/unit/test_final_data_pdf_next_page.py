"""Round 3 of the final data review: what the page after a table is, when it repeats no header.

Round 2 (6b5a217) let one line above body on the next page be body, a running title, or a new
table's header. A reviewer of that commit built PDFs on which the line was misread, each a 200:

- PR1: a line holding a number under a typed column was body, so a different table of the same
  width whose header holds a year or a number ('Region', '2023', '2024', 'Change %') was merged
  into the first, its header a data row: 51 rows for 30, 'units' int64 -> double, and no
  'found 2 tables' warning (07fa450 and 57c8f90 split). Ruled and borderless alike.
- PR2: a line shaped like a text-only body row was body. After a suppressed row in the body
  ('Area5', 'x', 'x', text in every cell) any new table's header on the next page was one.
- PR3: a running title in the column of the section rows ('Ledger p2', or '(continued)' on
  continuation pages only) is shaped like a section row ('North'), so each became a data row,
  with no warning: 127 rows for 124.
- PR4 (07fa450 too): a '(continued)' title printed on continuation pages only and wide enough to
  cross every typed column stayed those pages' header and split the table: 79 of 120 rows.
- PR5 (07fa450 too, deferred): a last page holding the table's final row and then a different
  table is one table to text alignment, so the second table is appended to the first.

A split keeps the 'found N tables' warning and every table reachable with pdf_table; a merge
of a different table is silent wrong data. When the line cannot be told apart, the page splits.
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


def _two_tables(first: Rows, second: Rows, *, ruled: bool) -> bytes:
    """Two tables, each on its own page under its own header (``rows[0]``)."""
    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_font("Helvetica", size=9)
    borders: dict[str, Any] = {} if ruled else {"borders_layout": "NONE"}
    for rows in (first, second):
        pdf.add_page()
        with pdf.table(headings_style=FontFace(emphasis="BOLD"), **borders) as table:
            for values in rows:
                row = table.row()
                for value in values:
                    row.cell(value)
    return bytes(pdf.output())


def _ledger(
    rows: int,
    *,
    header: list[str],
    row: Callable[[int, random.Random], list[str]],
    title: Callable[[int], str],
    cell: float = 0,
    sections: dict[int, str] | None = None,
    after: Callable[[FPDF], None] | None = None,
    repeat: bool = False,
) -> bytes:
    """A borderless table of ``rows`` rows, its header on page 1 only (every page: ``repeat``),
    ``title(page)`` above every page in a ``cell`` mm wide cell (0: the page width), and
    section rows before some rows."""

    class Doc(FPDF):
        def header(self) -> None:
            if line := title(self.page_no()):
                self.set_font("Helvetica", "B", 10)
                self.cell(cell, 8, line, new_x="LMARGIN", new_y="NEXT")
                self.set_font("Helvetica", size=9)

    rnd = random.Random(5)
    pdf = Doc()
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_page()
    pdf.set_font("Helvetica", size=9)
    style = FontFace(emphasis="BOLD")
    with pdf.table(
        borders_layout="NONE", repeat_headings=int(repeat), headings_style=style
    ) as table:
        line = table.row()
        for name in header:
            line.cell(name)
        for i in range(1, rows + 1):
            if sections and i in sections:
                line = table.row()
                for value in [sections[i]] + [""] * (len(header) - 1):
                    line.cell(value)
            line = table.row()
            for value in row(i, rnd):
                line.cell(value)
    if after:
        after(pdf)
    return bytes(pdf.output())


def _found(page: int, rows: Rows, captions: list[str] | None = None) -> pdf_reader._Found:
    return pdf_reader._Found(page, rows, "text alignment", list(captions or []))


def _warned_split(warnings: list[str], rows: int) -> bool:
    return any(w.startswith(f"found 2 tables; used table 0: page 1, {rows} rows") for w in warnings)


# ---------------------------------------------------------------- PR1: a number in a header

PRODUCTS = [["Product", "Units", "Price", "Revenue"]] + [
    [f"Product{i}", str(10 + i), f"{i}.25", f"{100 + i}.50"] for i in range(1, 9)
]
REGIONS = [[f"Region{i}", f"{1000 + i}.25", f"{2000 + i}.75", f"{i}.5"] for i in range(1, 6)]
STORES = [["Store", "Units", "Orders", "Revenue"]] + [
    [f"Store{i}", str(10 + i), str(20 + i), f"{100 + i}.50"] for i in range(1, 9)
]
SCORES = [[f"Q{i}", str(i), str(2 * i), f"{i}.5"] for i in range(1, 6)]


@pytest.mark.parametrize(
    ("first", "header", "body"),
    [
        (PRODUCTS, ["Region", "2023", "2024", "Change %"], REGIONS),
        (PRODUCTS, ["Department", "Headcount", "Budget", "2024"], REGIONS),
        # Written like the body ('1' over integers) and no year: a scale of answers.
        (STORES, ["Question", "1", "2", "Mean"], SCORES),
    ],
    ids=["years-and-change", "a-year-beside-names", "numbers-beside-a-mean"],
)
def test_merge_a_header_holding_a_number_starts_a_new_table(
    first: Rows, header: list[str], body: Rows
) -> None:
    tables = pdf_reader._merge([_found(1, first), _found(2, [header, *body])])
    assert [t.header for t in tables] == [first[0], header]
    assert tables[0].rows == first[1:]


def _products(rnd: random.Random) -> Rows:
    return [["Product", "Units", "Price", "Revenue"]] + [
        [
            f"Product{i}",
            str(rnd.randrange(1, 500)),
            f"{rnd.uniform(1, 99):.2f}",
            f"{rnd.uniform(100, 9999):.2f}",
        ]
        for i in range(1, 31)
    ]


@pytest.mark.parametrize("ruled", [False, True], ids=["borderless", "ruled"])
@pytest.mark.parametrize(
    "header",
    [["Region", "2023", "2024", "Change %"], ["Department", "Headcount", "Budget", "2024"]],
    ids=["years-and-change", "a-year-beside-names"],
)
def test_a_new_table_whose_header_holds_a_year_is_not_merged(
    tmp_path: Path, header: list[str], ruled: bool
) -> None:
    # 6b5a217: 51 rows (30 products, the header ('Region', 2023.0, 2024.0, null), 20 regions),
    # 'units' int64 -> double, and only 'merged a table that continues across 2 pages'.
    rnd = random.Random(7)
    first = _products(rnd)
    second = [header] + [
        [
            f"Region{i}",
            f"{rnd.uniform(100, 9999):.2f}",
            f"{rnd.uniform(100, 9999):.2f}",
            f"{rnd.uniform(-20, 20):.1f}",
        ]
        for i in range(1, 21)
    ]
    data = _two_tables(first, second, ruled=ruled)
    result = kit.convert_bytes(tmp_path, "two.pdf", data)
    assert result.rows["product"] == [f"Product{i}" for i in range(1, 31)]
    assert [c.type for c in result.report.columns] == ["string", "int64", "double", "double"]
    assert _warned_split(result.report.warnings, 30), result.report.warnings
    assert not any("continues across" in w for w in result.report.warnings)
    other = kit.convert_bytes(tmp_path, "two.pdf", data, pdf_table=1)
    assert other.report.rows == 20
    assert other.rows[other.report.columns[0].name][0] == "Region1"


def test_a_new_table_whose_header_holds_numbers_written_like_the_body_is_not_merged(
    tmp_path: Path,
) -> None:
    # The values in the header ('1', '2') are written as the body's integers, and no year.
    rnd = random.Random(3)
    first = [["Store", "Units", "Orders", "Revenue"]] + [
        [
            f"Store{i}",
            str(rnd.randrange(1, 500)),
            str(rnd.randrange(1, 90)),
            f"{rnd.uniform(100, 9999):.2f}",
        ]
        for i in range(1, 31)
    ]
    second = [["Question", "1", "2", "Mean"]] + [
        [f"Q{i}", str(rnd.randrange(1, 50)), str(rnd.randrange(1, 50)), f"{rnd.uniform(1, 5):.2f}"]
        for i in range(1, 21)
    ]
    result = kit.convert_bytes(tmp_path, "two.pdf", _two_tables(first, second, ruled=False))
    assert result.rows["store"] == [f"Store{i}" for i in range(1, 31)]
    assert [c.type for c in result.report.columns] == ["string", "int64", "int64", "double"]
    assert _warned_split(result.report.warnings, 30), result.report.warnings


def test_a_pending_row_is_body_only_with_text_the_body_holds() -> None:
    # A row with text where a value belongs and a value written like the body: body, when that
    # text is already in the body's column (a 'pending' earlier on). Seen for the first time,
    # nothing tells it from a header holding a number ('Region', '2023', 'Change %'): a split.
    first = [["Region", "Units", "Revenue"]] + [
        [f"Area{i}", str(100 + i), "pending" if i == 3 else f"{1000 + i}.50"] for i in range(9)
    ]
    more = [[f"Area{i}", str(100 + i), f"{1000 + i}.50"] for i in range(10, 13)]
    pending = ["Area9", "109", "pending"]
    (table,) = pdf_reader._merge([_found(1, first), _found(2, [pending, *more])])
    assert table.rows == [*first[1:], pending, *more]
    # Text not seen in that column; a year where the body holds no years (a column name, never
    # a value); a value written unlike the body's integers; cells no body row fills.
    for line in [
        ["Area9", "109", "TBD"],
        ["Area9", "2023", "pending"],
        ["Area9", "109.5", "pending"],
        ["", "109", "pending"],
    ]:
        tables = pdf_reader._merge([_found(1, first), _found(2, [line, *more])])
        assert [t.header for t in tables] == [first[0], line]


# ---------------------------------------------------------------- PR2: a suppressed row


def test_merge_after_a_suppressed_row_a_new_header_starts_a_new_table() -> None:
    body = [[f"Area{i}", str(100 + i), f"{1000 + i}.50"] for i in range(1, 9)]
    body[3] = ["Area4", "x", "x"]  # suppressed values: text in every cell
    first = [["Region", "Units", "Revenue"], *body]
    second = [["City", "Stores", "Sales"]] + [[f"City{i}", str(i), f"{i}.25"] for i in range(1, 6)]
    tables = pdf_reader._merge([_found(1, first), _found(2, second)])
    assert [t.header for t in tables] == [first[0], second[0]]


@pytest.mark.parametrize("ruled", [False, True], ids=["borderless", "ruled"])
@pytest.mark.parametrize("mark", ["x", "n.m."])
def test_a_new_table_after_a_suppressed_row_is_not_merged(
    tmp_path: Path, mark: str, ruled: bool
) -> None:
    # 6b5a217: 51 rows, ('City', 'Stores', 'Sales') and the 20 cities among the regions.
    rnd = random.Random(11)
    first = [["Region", "Units", "Revenue"]] + [
        [f"Area{i}", str(rnd.randrange(100, 900)), f"{rnd.uniform(1000, 9999):.2f}"]
        for i in range(1, 31)
    ]
    first[5] = ["Area5", mark, mark]
    second = [["City", "Stores", "Sales"]] + [
        [f"City{i}", str(rnd.randrange(1, 90)), f"{rnd.uniform(1000, 9999):.2f}"]
        for i in range(1, 21)
    ]
    result = kit.convert_bytes(tmp_path, "two.pdf", _two_tables(first, second, ruled=ruled))
    assert result.rows["region"] == [f"Area{i}" for i in range(1, 31)]
    assert _warned_split(result.report.warnings, 30), result.report.warnings


# ---------------------------------------------------------------- PR3: a title like a section

SECTION_HEADER = ["Area", "Units", "Revenue", "Date", "Seq"]
SECTION_AT = {1: "North", 30: "South", 70: "East", 100: "West"}


def _section_row(i: int, rnd: random.Random) -> list[str]:
    return [
        f"Area{i}",
        str(rnd.randrange(1, 900)),
        f"{rnd.uniform(10, 9999):.2f}",
        f"2025-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}",
        str(i),
    ]


def test_merge_a_running_title_is_not_a_section_row() -> None:
    first = [["Area", "Units", "Revenue"], ["North", "", ""]] + [
        [f"Area{i}", str(100 + i), f"{1000 + i}.50"] for i in range(6)
    ]
    more = [[f"Area{i}", str(100 + i), f"{1000 + i}.50"] for i in range(6, 9)]
    # The page-1 title with another page number: a caption, not a section row.
    (table,) = pdf_reader._merge(
        [_found(1, first, ["Ledger p1"]), _found(2, [["Ledger p2", "", ""], *more])]
    )
    assert table.rows == [*first[1:], *more]
    assert table.captions == ["Ledger p1", "Ledger p2"]
    # A line saying the table continues, with no title on page 1 to compare it with.
    for line in ["(continued)", "North (cont'd)", "Continued from page 1", "Table 1 - continued"]:
        (table,) = pdf_reader._merge([_found(1, first), _found(2, [[line, "", ""], *more])])
        assert table.rows == [*first[1:], *more]
        assert table.captions == [line]
    # A page number where the section labels go is no section row.
    (table,) = pdf_reader._merge([_found(1, first), _found(2, [["2", "", ""], *more])])
    assert table.rows == [*first[1:], *more]
    assert table.captions == ["2"]
    # A real section row still is one; so is a word that only ends like the marker. Round 4:
    # when another page starts with body (no running title); on the last page of two, nothing
    # tells it from a title printed there only, so the page splits (test_final_data_pdf.py).
    last = [[f"Area{i}", str(100 + i), f"{1000 + i}.50"] for i in range(9, 12)]
    for section in ["South", "Discontinued"]:
        (table,) = pdf_reader._merge(
            [
                _found(1, first, ["Ledger p1"]),
                _found(2, [[section, "", ""], *more]),
                _found(3, last),
            ]
        )
        assert table.rows == [*first[1:], [section, "", ""], *more, *last]


@pytest.mark.parametrize(
    ("title", "dropped"),
    [
        (lambda p: f"Ledger p{p}", "dropped 4 line(s) above the header: 'Ledger p1'; 'Ledger p2'"),
        (
            lambda p: "" if p == 1 else "(continued)",
            "dropped 3 line(s) above the header: '(continued)'",
        ),
    ],
    ids=["page-numbered", "continued"],
)
def test_a_running_title_in_the_section_column_is_not_a_data_row(
    tmp_path: Path, title: Callable[[int], str], dropped: str
) -> None:
    # 6b5a217: 127 rows, 'Ledger p2'..'Ledger p4' (or '(continued)' three times) in the area
    # column with every other value null, and no warning naming them.
    data = _ledger(
        120, header=SECTION_HEADER, row=_section_row, title=title, cell=30, sections=SECTION_AT
    )
    result = kit.convert_bytes(tmp_path, "sections.pdf", data)
    area = result.rows["area"]
    assert [a for a in area if not a.startswith("Area")] == ["North", "South", "East", "West"]
    assert [s for s in result.rows["seq"] if s is not None] == list(range(1, 121))
    assert result.report.rows == 124
    assert "merged a table that continues across 4 pages" in result.report.warnings
    assert any(w.startswith(dropped) for w in result.report.warnings), result.report.warnings
    assert not any(w.startswith("found ") for w in result.report.warnings)


def test_a_header_ending_in_a_word_like_continued_is_still_a_new_table() -> None:
    # Only the marker after punctuation ('(continued)', '- cont.') is a running title; a
    # column named 'Continued' (patients who stayed on treatment) is a header.
    first = [["Arm", "Started", "Stopped", "Ongoing"]] + [
        [f"A{i}", str(10 + i), str(i), str(9)] for i in range(1, 7)
    ]
    second = [["Site", "Enrolled", "Withdrawn", "Continued"]] + [
        [f"S{i}", str(20 + i), str(i), str(19)] for i in range(1, 5)
    ]
    tables = pdf_reader._merge([_found(1, first), _found(2, second)])
    assert [t.header for t in tables] == [first[0], second[0]]


# ---------------------------------------------------------------- PR4: a wide '(continued)'

LEDGER_HEADER = ["ID", "Date", "Region", "Amount", "Qty"]
WIDE_CONTINUED = (
    "Table 4. Quarterly sales ledger, ACME Corporation and subsidiaries, all regions and "
    "channels (continued)"
)


def _ledger_row(i: int, rnd: random.Random) -> list[str]:
    return [
        str(i),
        f"2025-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}",
        rnd.choice(["North", "South", "East"]),
        f"{rnd.uniform(10, 9999):.2f}",
        str(rnd.randrange(1, 900)),
    ]


@pytest.mark.parametrize(("rows", "pages"), [(120, 3), (70, 2)])
def test_a_continued_title_across_every_column_does_not_split_the_table(
    tmp_path: Path, rows: int, pages: int
) -> None:
    # 6b5a217 and 07fa450: 79 of 120 rows (41 of 70), the columns named after the title
    # ('table_4_quarterly_sale', ...), 'found 2 tables; used table 1'.
    data = _ledger(
        rows,
        header=LEDGER_HEADER,
        row=_ledger_row,
        title=lambda p: "" if p == 1 else WIDE_CONTINUED,
    )
    result = kit.convert_bytes(tmp_path, "continued.pdf", data)
    report = result.report
    assert [c.name for c in report.columns] == ["id", "date", "region", "amount", "qty"]
    assert result.rows["id"] == list(range(1, rows + 1))
    assert f"merged a table that continues across {pages} pages" in report.warnings
    assert any(
        w.startswith(f"dropped {pages - 1} line(s) above the header: 'Table 4. Quarterly")
        for w in report.warnings
    ), report.warnings
    assert not any(w.startswith("found ") for w in report.warnings)


def test_a_wide_title_that_does_not_say_continued_still_splits(tmp_path: Path) -> None:
    # One line of running text across every column, on continuation pages only, is the
    # page's header to the type rules; positions say it is running text, but nothing says it
    # belongs to the table above. That is one signal, so the page splits, with the warning.
    title = WIDE_CONTINUED.removesuffix(" (continued)")
    data = _ledger(
        70, header=LEDGER_HEADER, row=_ledger_row, title=lambda p: "" if p == 1 else title
    )
    result = kit.convert_bytes(tmp_path, "titled.pdf", data)
    assert any(w.startswith("found 2 tables") for w in result.report.warnings)


@pytest.mark.parametrize(
    ("layout", "passes"),
    [("repeated-header", 0), ("header-once", 0), ("continued", 2), ("new-table", 1)],
)
def test_positions_are_read_only_under_another_header_on_the_next_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str, passes: int
) -> None:
    # Reading positions is a pass over the page's words. A header repeated on every page, or
    # a page that starts with body, must not pay for it: only a top row of the same width
    # under another header than the previous page's (a cut title, or a new table) does.
    calls: list[int] = []
    running_text = pdf_reader._running_text

    def spy(table: Any, words: Any) -> set[int]:
        calls.append(1)
        return running_text(table, words)

    monkeypatch.setattr(pdf_reader, "_running_text", spy)
    if layout == "new-table":
        rnd = random.Random(1)
        first = [LEDGER_HEADER] + [_ledger_row(i, rnd) for i in range(1, 31)]
        second = [["Order", "Shipped", "Carrier", "Weight", "Boxes"]] + [
            _ledger_row(i, rnd) for i in range(1, 21)
        ]
        data = _two_tables(first, second, ruled=False)
    else:
        data = _ledger(
            120,
            header=LEDGER_HEADER,
            row=_ledger_row,
            title=lambda p: "" if p == 1 or layout != "continued" else WIDE_CONTINUED,
            repeat=layout == "repeated-header",
        )
    kit.convert_bytes(tmp_path, "ledger.pdf", data)
    assert len(calls) == passes


# ---------------------------------------------------------------- PR5: deferred


def _shipments(pdf: FPDF) -> None:
    pdf.ln(8)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 8, "Table 2. Shipments", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=9)
    rnd = random.Random(3)
    with pdf.table(borders_layout="NONE", headings_style=FontFace(emphasis="BOLD")) as table:
        line = table.row()
        for name in ["Order", "Shipped", "Carrier", "Weight", "Boxes"]:
            line.cell(name)
        for i in range(1, 11):
            line = table.row()
            for value in [
                str(900 + i),
                f"2024-0{(i % 9) + 1}-1{i % 9}",
                rnd.choice(["DHL", "UPS"]),
                f"{rnd.uniform(1, 99):.2f}",
                str(i),
            ]:
                line.cell(value)


@pytest.mark.xfail(
    reason=(
        "PR5, pre-existing at 07fa450: the page is one table to text alignment, and the only "
        "sign of the second table (a row naming every typed column) is also what a first "
        "suppressed row ('Area5', 'x', 'x') on a continuation page looks like"
    ),
    strict=True,
)
def test_a_different_table_after_the_last_row_is_not_appended(tmp_path: Path) -> None:
    data = _ledger(126, header=LEDGER_HEADER, row=_ledger_row, title=lambda _: "", after=_shipments)
    result = kit.convert_bytes(tmp_path, "spill.pdf", data)
    assert result.rows["id"] == list(range(1, 127))  # all three versions: 138 rows
