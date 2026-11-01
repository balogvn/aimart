"""Round 5 of the final data review: a new table's header read as a running title.

A regression-only review of d13688e (against 07fa450, which split each of these files with a
'found N tables' warning) found two 200s for wrong data, both from the order of the checks on
the one line above rows that continue the table: ``_continuation`` asked ``_running_title``
whether the line was a title before it asked whether the line was a header.

- PT1: a page label anywhere in the line was 'it names its page'. A header of fiscal periods
  ('Cost centre', 'P01', 'P02', 'P03', 'P04', 'Total', the ERP default) holds 'P02', which
  named page 2; percentiles ('P5', 'P10', 'P50', ...) named pages 5, 10 and 50. So a different
  statement on the next page, with its own header row, was merged into the one above, its
  header dropped as a title: 60 rows for 30, and pdf_table=1 no longer existed.
- PT2: 'continued' was read off the end of the whole line. A header whose last columns are
  'Ret.' and 'Cont.' (return and contribution, in performance attribution) squashes to
  '...ret.cont.', which ends like 'Table 1. Cont.'. A different attribution table on the next
  page merged into the one above the same way.

A line that may be a new table's header (it names every typed column, or fills more than half
of the cells) is now this table's only when it repeats a line already dropped above it; a page
label or 'continued' is asked only of a line that cannot be a header, and a page label counts
only when the line holds one ('Page 2 of 4', 'Ledger p2'), not one per column.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

import pytest
from fpdf import FPDF
from fpdf.fonts import FontFace

import aimart_testkit as kit
from aimart_parquet.engine.readers import pdf as pdf_reader
from aimart_parquet.errors import InvalidRequest

if TYPE_CHECKING:
    from pathlib import Path

Rows = list[list[str]]


def _statements(tables: list[tuple[list[str], Rows]], *, ruled: bool, running: str = "") -> bytes:
    """Each table from a new page, its header row bold; ``running`` atop every page. A table
    longer than a page goes on to the next with its header repeated (fpdf2's default)."""

    class Doc(FPDF):
        def header(self) -> None:
            if running:
                self.set_font("Helvetica", "B", 10)
                self.cell(0, 8, running, new_x="LMARGIN", new_y="NEXT")
            self.set_font("Helvetica", size=9)

    pdf = Doc()
    pdf.set_auto_page_break(True, margin=18)
    borders: dict[str, Any] = {} if ruled else {"borders_layout": "NONE"}
    for header, body in tables:
        pdf.add_page()
        with pdf.table(headings_style=FontFace(emphasis="BOLD"), **borders) as table:
            for values in [header, *body]:
                row = table.row()
                for value in values:
                    row.cell(value)
    return bytes(pdf.output())


def _amounts(rnd: random.Random, label: str, n: int, values: int) -> Rows:
    return [
        [f"{label} {4000 + i}", *(f"{rnd.uniform(100, 99999):,.2f}" for _ in range(values))]
        for i in range(n)
    ]


def _split(warnings: list[str], tables: int) -> bool:
    return any(w.startswith(f"found {tables} tables") for w in warnings)


# ---------------------------------------------------------------- PT1: a page label per column

PERIODS = ["P01", "P02", "P03", "P04", "Total"]


@pytest.mark.parametrize("running", ["", "ACME Ltd - management accounts - March 2025"])
@pytest.mark.parametrize("ruled", [False, True], ids=["borderless", "ruled"])
def test_a_header_of_period_columns_on_the_next_page_is_a_new_table(
    tmp_path: Path, ruled: bool, running: str
) -> None:
    # d13688e: 60 rows, the cost centres under 'revenue_account', 'P02' read as page 2's label.
    rnd = random.Random(4)
    data = _statements(
        [
            (["Revenue account", *PERIODS], _amounts(rnd, "Sales", 30, 5)),
            (["Cost centre", *PERIODS], _amounts(rnd, "CC", 30, 5)),
        ],
        ruled=ruled,
        running=running,
    )
    result = kit.convert_bytes(tmp_path, "accounts.pdf", data)
    assert result.rows["revenue_account"] == [f"Sales {4000 + i}" for i in range(30)]
    assert _split(result.report.warnings, 2), result.report.warnings
    costs = kit.convert_bytes(tmp_path, "accounts.pdf", data, pdf_table=1)
    assert [c.name for c in costs.report.columns][:2] == ["cost_centre", "p01"]
    assert costs.rows["cost_centre"] == [f"CC {4000 + i}" for i in range(30)]


def test_a_statement_over_two_pages_after_another_is_its_own_table(tmp_path: Path) -> None:
    # d13688e: 100 rows, the revenue statement and the cost statement (pages 2-3) one table.
    # 07fa450 kept them apart and chose the larger, the cost statement.
    rnd = random.Random(5)
    data = _statements(
        [
            (["Revenue account", *PERIODS], _amounts(rnd, "Sales", 30, 5)),
            (["Cost centre", *PERIODS], _amounts(rnd, "CC", 70, 5)),
        ],
        ruled=True,
    )
    result = kit.convert_bytes(tmp_path, "accounts.pdf", data)
    assert result.rows["cost_centre"] == [f"CC {4000 + i}" for i in range(70)]
    assert _split(result.report.warnings, 2), result.report.warnings


def test_a_header_of_percentiles_on_page_5_is_a_new_table(tmp_path: Path) -> None:
    # d13688e: page 5's table merged into page 4's ('P5' named page 5), 'found 4 tables'.
    rnd = random.Random(4)
    pct = ["P5", "P10", "P50", "P90", "P95"]
    regions = ["North", "South", "East", "West", "Central"]
    data = _statements(
        [
            (
                [f"Region: {r}", *pct],
                [
                    [f"{r} item {i}", *(f"{rnd.uniform(1, 999):.1f}" for _ in pct)]
                    for i in range(30)
                ],
            )
            for r in regions
        ],
        ruled=True,
    )
    result = kit.convert_bytes(tmp_path, "percentiles.pdf", data)
    assert _split(result.report.warnings, 5), result.report.warnings
    for index, region in enumerate(regions):
        one = kit.convert_bytes(tmp_path, "percentiles.pdf", data, pdf_table=index)
        name = f"region_{region.lower()}"
        assert one.rows[name] == [f"{region} item {i}" for i in range(30)]
    with pytest.raises(InvalidRequest):  # five tables: 0-4
        kit.convert_bytes(tmp_path, "percentiles.pdf", data, pdf_table=5)


# ---------------------------------------------------------------- PT2: 'Cont.' as a column name

ATTRIBUTION = ["Port. wt.", "Bench. wt.", "Ret.", "Cont."]


@pytest.mark.parametrize("ruled", [False, True], ids=["borderless", "ruled"])
def test_a_header_ending_in_cont_on_the_next_page_is_a_new_table(
    tmp_path: Path, ruled: bool
) -> None:
    # d13688e: 60 rows, the bond sectors under 'equity_sector', the header read as 'cont.'.
    rnd = random.Random(4)

    def rows(label: str) -> Rows:
        return [
            [
                f"{label} {i}",
                f"{rnd.uniform(1, 30):.1f}",
                f"{rnd.uniform(1, 30):.1f}",
                f"{rnd.uniform(-9, 9):.2f}",
                f"{rnd.uniform(-1, 1):.2f}",
            ]
            for i in range(30)
        ]

    data = _statements(
        [
            (["Equity sector", *ATTRIBUTION], rows("Equity")),
            (["Bond sector", *ATTRIBUTION], rows("Bond")),
        ],
        ruled=ruled,
    )
    # Text alignment cuts the label column in two ('Equity' | '0'), as it did at 07fa450.
    result = kit.convert_bytes(tmp_path, "attribution.pdf", data)
    labels = result.rows[result.report.columns[0].name]
    assert len(labels) == 30
    assert {str(v).split()[0] for v in labels} == {"Equity"}
    assert _split(result.report.warnings, 2), result.report.warnings
    bonds = kit.convert_bytes(tmp_path, "attribution.pdf", data, pdf_table=1)
    labels = bonds.rows[bonds.report.columns[0].name]
    assert len(labels) == 30
    assert {str(v).split()[0] for v in labels} == {"Bond"}


# ---------------------------------------------------------------- the rule, on found tables


def _found(page: int, rows: Rows, captions: list[str] | None = None) -> pdf_reader._Found:
    return pdf_reader._Found(page, rows, "text alignment", list(captions or []))


SECTORS = [["Sector", "Wt.", "Ret.", "Cont."]] + [
    [f"Equity {i}", f"{10 + i}.5", f"{i}.25", f"0.{10 + i}"] for i in range(8)
]
BONDS = [[f"Bond {i}", f"{20 + i}.5", f"{i}.75", f"0.{30 + i}"] for i in range(5)]


@pytest.mark.parametrize(
    "line",
    [
        ["Bond sector", "Wt.", "Ret.", "Cont."],  # names every typed column
        ["Bond sector", "2024", "Ret.", "Cont."],  # a year among the names: _names counts 0
        ["Bond sector", "", "Ret.", "Cont."],  # a blank over a typed column
        ["Plan P1 vs P2", "", "", ""],  # two page labels, one of them the page's
    ],
)
def test_merge_a_line_that_may_be_a_header_is_no_running_title(line: list[str]) -> None:
    # Each was dropped as a running title at d13688e, and the next table merged in.
    tables = pdf_reader._merge([_found(1, SECTORS), _found(2, [line, *BONDS])])
    assert [(t.header, t.rows) for t in tables] == [(SECTORS[0], SECTORS[1:]), (line, BONDS)]


def test_merge_a_header_line_that_repeats_the_title_above_page_1_is_dropped() -> None:
    # A title cut into every column, as the alignment pass cuts a wide one, names every typed
    # column like a header; printed above page 1's header too, it is the table's running title.
    title = ["ACME Corp -", "sales le", "dger by", "sector"]
    (table,) = pdf_reader._merge(
        [_found(1, SECTORS, [" ".join(title)]), _found(2, [title, *BONDS])]
    )
    assert table.rows == [*SECTORS[1:], *BONDS]
    # And a line that cannot be a header still says so with one page label or 'continued'.
    for line in (["Page 2 of 4", "", "", ""], ["Table 1. Cont.", "", "", ""]):
        (table,) = pdf_reader._merge([_found(1, SECTORS), _found(2, [line, *BONDS])])
        assert table.rows == [*SECTORS[1:], *BONDS]
        assert table.captions == [line[0]]
