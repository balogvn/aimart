"""The spill guard recognises the next table's header instead of counting the rows below it.

a84df39 stopped a different table's scale header ('Statement', '1', ..., '5') atop the next page
being appended to the table before it as a data row, by keeping nothing from a page with three
or more rows holding values under two typed columns below the kept ones. Two cases the count
got wrong (``test_final_integrate_pdf_spill.py`` has the cases it got right):

- The second table has only one or two statements: fewer than three valued rows, so its
  scale header was still taken, 21 rows for 20 with the last 'Statement', 1, 2, 3, 4, 5 typed
  as counts, under "merged a table that continues across 2 pages". 07fa450 returned the 20.
- The first table really spills its last rows onto the page, above a second table with a
  text header ('Statement', 'Never', ..., 'Always') or a scale header: the statements below
  are three or more valued rows, so the spilled rows were dropped too, a 200 with 40 of 45
  rows and no warning (bebfe33 recovered them for the text header).

The row that ends the spilled body is now judged itself: a header of scale points (the typed
cells are consecutive integers from 0 or 1) ends it, and when the row that ends it is another
table's header (it names every typed column, or is a scale header), the rows above it are this
table's spill. Otherwise, with three valued rows below, the reader cannot tell this table's
spill from the other table's first rows: they are split off as a table of their own, which the
'found N tables' warning names, instead of being dropped with no word (a84df39) or merged.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

import pyarrow.parquet as pq
import pytest
from fpdf import FPDF
from fpdf.fonts import FontFace

from aimart_parquet.engine.convert import convert_file
from aimart_parquet.engine.limits import Limits
from aimart_parquet.engine.models import ConvertOptions

if TYPE_CHECKING:
    from pathlib import Path

QUESTIONS = [
    f"Q{i} The staff explained the next steps clearly and in good time" for i in range(1, 21)
]
FIRST = ["Department", "Very poor", "Poor", "Fair", "Good", "Excellent"]
SCALE = ["Statement", "1", "2", "3", "4", "5"]
WORDS = ["Statement", "Never", "Rarely", "Sometimes", "Often", "Always"]
MERGED = "merged a table that continues"


class _Doc(FPDF):
    def __init__(self) -> None:
        super().__init__()
        self.set_auto_page_break(True, margin=18)

    def header(self) -> None:
        self.set_font("Helvetica", "B", 10)
        self.cell(0, 8, "Customer survey 2025 - results", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", size=9)


def _table(pdf: FPDF, header: list[str], rows: list[list[str]]) -> None:
    with pdf.table(
        borders_layout="NONE", headings_style=FontFace(emphasis="BOLD"), repeat_headings=1
    ) as table:
        for values in [header, *rows]:
            row = table.row()
            for value in values:
                row.cell(value)


def _convert(path: Path, tmp_path: Path) -> tuple[list[str], list[str], list[str]]:
    out = tmp_path / "o.parquet"
    report = convert_file(
        path, out, filename=path.name, options=ConvertOptions(), workdir=tmp_path, limits=Limits()
    )
    table = pq.read_table(out)
    return table.column(0).to_pylist(), [str(t) for t in table.schema.types[1:]], report.warnings


@pytest.mark.parametrize("statements", [1, 2, 3])
def test_a_short_next_table_under_a_scale_header_is_not_appended(
    statements: int, tmp_path: Path
) -> None:
    rnd = random.Random(11)
    pdf = _Doc()
    pdf.set_font("Helvetica", size=9)
    pdf.add_page()
    departments = [f"Dept {c}" for c in "ABCDEFGHIJKLMNOPQRST"]
    _table(pdf, FIRST, [[d, *(str(rnd.randint(0, 60)) for _ in range(5))] for d in departments])
    pdf.add_page()
    rows = [[q, *(str(rnd.randint(0, 60)) for _ in range(5))] for q in QUESTIONS[:statements]]
    _table(pdf, SCALE, rows)
    src = tmp_path / "short.pdf"
    pdf.output(str(src))
    labels, types, warnings = _convert(src, tmp_path)
    assert labels == departments, warnings  # before, with 1 or 2: a 21st row, 'Statement'
    assert all(t == "int64" for t in types)
    assert not any(MERGED in w for w in warnings)


@pytest.mark.parametrize("header", [WORDS, SCALE], ids=["text", "scale"])
@pytest.mark.parametrize("n", [41, 43, 45])
def test_rows_spilled_above_the_next_tables_header_are_kept(
    header: list[str], n: int, tmp_path: Path
) -> None:
    rnd = random.Random(11)
    pdf = _Doc()
    pdf.add_page()
    departments = [f"Dept {i}" for i in range(1, n + 1)]
    _table(pdf, FIRST, [[d, *(str(rnd.randint(0, 60)) for _ in range(5))] for d in departments])
    _table(pdf, header, [[q, *(str(rnd.randint(0, 60)) for _ in range(5))] for q in QUESTIONS])
    src = tmp_path / "spill.pdf"
    pdf.output(str(src))
    labels, types, warnings = _convert(src, tmp_path)
    assert labels == departments, warnings  # before: Dept 1..40, the spilled rows dropped
    assert all(t == "int64" for t in types)


TENS = ["Statement", "10", "20", "30", "40", "50"]  # a header that reads like body, not scale


@pytest.mark.parametrize("n", [42, 45])
def test_rows_that_may_be_spill_or_the_next_table_are_split_off_and_reported(
    n: int, tmp_path: Path
) -> None:
    """Rows written like the body above a table alignment cannot read, whose header is no
    header the reader knows ('Statement', '10', ..., '50' reads like a row of counts): they may
    be this table's spill or the other table's first rows. Merged (bebfe33) the other table's
    header and first statement were rows of this one; dropped (07fa450, 65183f0) the spilled
    rows were lost with no word. Split off, they are a second table the warning names, and the
    rows returned are 07fa450's."""
    rnd = random.Random(11)
    pdf = _Doc()
    pdf.add_page()
    departments = [f"Dept {i}" for i in range(1, n + 1)]
    _table(pdf, FIRST, [[d, *(str(rnd.randint(0, 60)) for _ in range(5))] for d in departments])
    _table(pdf, TENS, [[q, *(str(rnd.randint(0, 60)) for _ in range(5))] for q in QUESTIONS])
    src = tmp_path / "unsure.pdf"
    pdf.output(str(src))
    labels, types, warnings = _convert(src, tmp_path)
    assert labels == departments[:40], warnings
    assert all(t == "int64" for t in types)
    assert not any(MERGED in w for w in warnings)
    [found] = [w for w in warnings if w.startswith("found 2 tables")]
    assert "used table 0: page 1, 40 rows" in found
    out = tmp_path / "split.parquet"
    convert_file(
        src,
        out,
        filename=src.name,
        options=ConvertOptions(pdf_table=1),
        workdir=tmp_path,
        limits=Limits(),
    )
    assert pq.read_table(out).column(0).to_pylist()[: n - 40] == departments[40:]
