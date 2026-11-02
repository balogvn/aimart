"""A different table's header atop the next page is no spilled row of the table before it.

Regression from 07fa450 (the spilled-rows path, 6b5a217 and d4d0ad0). When text alignment finds
no table on a page after a borderless table, because the page's first column wraps (long
survey statements), ``_spilled_rows`` reads the top of that page at the previous table's column
edges and ``_spilled_body`` keeps its leading rows that look like body. A different table whose
header is its scale points ('Statement', '1', '2', '3', '4', '5') looks like body: its numbers
are written like the first table's single-digit counts and it fills every cell. It was appended
to the first table as a data row: 21 rows for 20, the last 'Statement', 1, 2, 3, 4, 5, typed as
counts, and the warning "merged a table that continues across 2 pages". 07fa450 returned the
first table's 20 rows. (In both, the second table itself is not read: alignment cannot see it.)

A real spill is a table's last one or two rows (alignment sees a table from three), so a page
with three or more rows below the kept ones that hold values under two typed columns holds a
table alignment could not read: nothing of it is taken. The intended spills still merge
(``test_final_data_pdf_next_page.py``, ``test_final_data_pdf_page_tops.py``).
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

STATEMENT = "The staff explained the next steps clearly and in good time"


class _Doc(FPDF):
    def __init__(self, running: str | None) -> None:
        super().__init__()
        self.running = running
        self.set_auto_page_break(True, margin=18)

    def header(self) -> None:
        if self.running:
            self.set_font("Helvetica", "B", 10)
            line = self.running.format(page=self.page_no())
            self.cell(0, 8, line, new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", size=9)


def _survey(
    path: Path,
    running: str | None,
    tables: list[tuple[list[str], list[str]]],
    seed: int,
) -> None:
    """One borderless table per page: (header, first-column labels) each, counts 0-60."""
    rnd = random.Random(seed)
    pdf = _Doc(running)
    pdf.set_font("Helvetica", size=9)
    for header, labels in tables:
        pdf.add_page()
        with pdf.table(
            borders_layout="NONE", headings_style=FontFace(emphasis="BOLD"), repeat_headings=1
        ) as table:
            for label in [None, *labels]:
                row = table.row()
                values = header if label is None else [label]
                if label is not None:
                    values += [str(rnd.randint(0, 60)) for _ in header[1:]]
                for value in values:
                    row.cell(value)
    pdf.output(str(path))


DEPARTMENTS = [f"Dept {c}" for c in "ABCDEFGHIJKLMNOPQRST"]
REGIONS = [f"Region {i}" for i in range(1, 26)]
# (layout, running line, tables, seed, the first table's labels)
LAYOUTS = [
    (
        "X2_scale_header_6cols",
        "Customer survey 2025 - results",
        [
            (["Department", "Very poor", "Poor", "Fair", "Good", "Excellent"], DEPARTMENTS),
            (
                ["Statement", "1", "2", "3", "4", "5"],
                [f"Q{i} {STATEMENT}" for i in range(1, 21)],
            ),
        ],
        11,
        DEPARTMENTS,
    ),
    *(
        (
            name,
            running,
            [
                (["Region", "Poor", "Fair", "Good", "Excellent"], REGIONS),
                (
                    ["Question", "1", "2", "3", "4"],
                    [f"Q{i} The service was quick" for i in range(1, 26)],
                ),
            ],
            7,
            REGIONS,
        )
        for name, running in (
            ("N1_same_title", "Customer survey 2025 - results"),
            ("N4_title_page_label", "Customer survey 2025 - page {page}"),
        )
    ),
]


@pytest.mark.parametrize(
    ("running", "tables", "seed", "labels"),
    [layout[1:] for layout in LAYOUTS],
    ids=[layout[0] for layout in LAYOUTS],
)
def test_a_next_tables_header_of_scale_points_is_no_spilled_row(
    running: str,
    tables: list[tuple[list[str], list[str]]],
    seed: int,
    labels: list[str],
    tmp_path: Path,
) -> None:
    src = tmp_path / "survey.pdf"
    _survey(src, running, tables, seed)
    out = tmp_path / "o.parquet"
    report = convert_file(
        src, out, filename=src.name, options=ConvertOptions(), workdir=tmp_path, limits=Limits()
    )
    table = pq.read_table(out)
    first = table.column(0).to_pylist()
    assert first == labels, report.warnings  # before: one more row, the next table's header
    assert all(str(t) == "int64" for t in table.schema.types[1:])
    assert not any("merged a table that continues" in w for w in report.warnings)
