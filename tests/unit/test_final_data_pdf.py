"""Regression from the final data review: a running page title cost a borderless table pages.

Every page of a ledger carries the same title line ('ACME Corp - Confidential sales ledger')
and the column header is printed on page 1 only. On page 1 the title was dropped as a caption;
on each continuation page it was the only row above the first data row, so it stayed, became
that page's header, and the page neither repeated page 1's header nor started with a data
row. The table split into one piece per page and the largest won: 80 of 120 rows, columns
named 'acme_corp_confiden', 'tial_sales_ledger', a 200. No pdf_table or pdf_pages value
returned the whole table.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

import pytest
from fpdf import FPDF
from fpdf.fonts import FontFace

import aimart_testkit as kit
from aimart_parquet.engine.readers import pdf as pdf_reader

if TYPE_CHECKING:
    from pathlib import Path

TITLE = "ACME Corp - Confidential sales ledger"
HEADER = ["ID", "Date", "Region", "Amount", "Qty"]


def _ledger(rows: int, *, title: str = TITLE, footer: bool = False, cell: float = 0) -> bytes:
    class Doc(FPDF):
        def header(self) -> None:
            if title:
                self.set_font("Helvetica", "B", 10)
                self.cell(cell, 8, title, new_x="LMARGIN", new_y="NEXT")
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
    with pdf.table(
        borders_layout="NONE", repeat_headings=0, headings_style=FontFace(emphasis="BOLD")
    ) as table:
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


def test_one_running_line_above_the_data_is_a_caption() -> None:
    cut = ["ACME Corp - Confiden", "tial sales ledger", "", "", ""]
    rows = [cut, *BODY]
    assert pdf_reader._split_captions(rows, running={0}.__contains__) == (
        BODY,
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
