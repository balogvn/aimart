"""The guide and README quote the notes and warnings the merged data fixes produce, word for word.

fix/final-data-fix added a column note (a zone that is not read), three PDF warnings (no header
row, a header of numbers, a running title dropped once a page) and a new tie rule for the
decimal convention, and the guide still described the old behaviour: an agent that greps a
report for the guide's text found nothing, or read 'the delimiter breaks a tie' and expected
1234 where the file's own numbers make it 1.234. Each quote here comes from a real conversion.
"""

from __future__ import annotations

import re
from pathlib import Path

from fpdf import FPDF
from fpdf.fonts import FontFace

import aimart_testkit as kit

ROOT = Path(__file__).resolve().parents[2]


def text(rel: str, heading: str) -> str:
    """One ``## heading`` section of a doc, with line wrapping collapsed."""
    body = (ROOT / rel).read_text(encoding="utf-8")
    start = body.index(heading)
    end = body.find("\n## ", start + len(heading))
    return " ".join(body[start : end if end != -1 else None].split())


GUIDE = text("docs/agents-guide.md", "## 3. Convert")
README = text("README.md", "## Supported formats")


def _ledger(rows: int, *, title: str, header: bool) -> bytes:
    """A borderless ledger, its header (if any) on page 1 only, ``title`` % page atop each page."""

    class Doc(FPDF):
        def header(self) -> None:
            if title:
                self.set_font("Helvetica", "B", 10)
                self.cell(0, 8, title % self.page_no(), new_x="LMARGIN", new_y="NEXT")
                self.set_font("Helvetica", size=9)

    pdf = Doc()
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
            for name in ("ID", "Date", "Region", "Amount", "Qty"):
                row.cell(name)
        for i in range(1, rows + 1):
            row = table.row()
            region = ("North", "South", "East")[i % 3]
            for value in (str(i), f"2025-{i % 12 + 1:02d}-{i % 27 + 1:02d}", region):
                row.cell(value)
            row.cell(f"{i * 7.31 % 9000 + 10:.2f}")
            row.cell(str(i * 37 % 900 + 1))
    return bytes(pdf.output())


def test_the_guide_states_the_decimal_tie_rule_as_the_code_applies_it(tmp_path: Path) -> None:
    points = kit.convert_bytes(
        tmp_path, "shop.csv", "price;weight\n398.53;1.234\n12.10;2.500\n7.25;12.750\n"
    )
    assert points.rows["weight"] == [1.234, 2.5, 12.75]
    bare = kit.convert_bytes(tmp_path, "bare.csv", "a;weight\nx;1.234\ny;2.500\nz;12.750\n")
    assert bare.rows["weight"] == [1234, 2500, 12750]
    assert "the file's other number columns, then its delimiter, break a tie" in GUIDE
    assert "the file's delimiter breaks a tie" not in GUIDE
    assert (
        "(`398.53` elsewhere in a semicolon file makes `1.234` 1.234; with no such evidence a "
        "semicolon file reads it as 1234)"
    ) in GUIDE


def test_the_guide_and_readme_quote_the_unread_zone_note(tmp_path: Path) -> None:
    rows = "".join(f"{d},2025-07-{d:02d} 09:30:00 BST\n" for d in range(1, 6))
    result = kit.convert_bytes(tmp_path, "uk.csv", "id,ts\n" + rows)
    assert result.column("ts").type == "string"
    (note,) = [n for n in result.column("ts").notes if n.startswith("zone ")]
    quoted = re.sub(r"\(\d+ of \d+\)", "(k of n)", note)
    assert quoted.startswith("zone 'BST' was not read as a zone (k of n): ")
    assert f"`{quoted}`" in GUIDE
    assert "zone 'BST' was not read as a zone (k of n)" in README
    for zone in ("`+01:00`", "`Z`", "`UTC`", "`Europe/London`", "`EST5EDT`"):
        assert zone in GUIDE
    assert "GMT, EST, MST, HST and JST" in GUIDE


def test_the_guide_quotes_the_running_title_and_merge_warnings(tmp_path: Path) -> None:
    result = kit.convert_bytes(tmp_path, "l.pdf", _ledger(120, title="Ledger p%d", header=True))
    assert result.rows["id"] == list(range(1, 121))
    warnings = result.report.warnings
    dropped = "dropped 3 line(s) above the header: 'Ledger p1'; 'Ledger p2'; 'Ledger p3'"
    assert dropped in warnings
    assert f"`{dropped}`" in GUIDE
    assert "merged a table that continues across 3 pages" in warnings
    assert "`merged a table that continues across 3 pages`" in GUIDE


def test_the_guide_quotes_the_no_header_row_warning(tmp_path: Path) -> None:
    result = kit.convert_bytes(tmp_path, "n.pdf", _ledger(30, title="", header=False))
    assert result.rows["column_1"] == list(range(1, 31))
    (warning,) = [w for w in result.report.warnings if w.startswith("no header row")]
    assert f"`{warning.replace('column_5', 'column_N')}`" in GUIDE
    assert "`header` applies to PDFs" in GUIDE


def test_the_guide_quotes_the_numeric_header_warning(tmp_path: Path) -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for row in [["Account", "4000", "5000"], *([f"R{i}", f"{i}.25", f"{i}.75"] for i in range(7))]:
        for cell in row:
            pdf.cell(50, 7, cell, border=1)
        pdf.ln()
    result = kit.convert_bytes(tmp_path, "codes.pdf", bytes(pdf.output()))
    (warning,) = [w for w in result.report.warnings if w.startswith("the header row")]
    assert f"`{warning}`" in GUIDE
