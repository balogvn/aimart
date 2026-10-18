"""Tables in digital PDFs: bordered, borderless, multi-page, selection, and honest failures."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pyarrow.parquet as pq
import pytest
from fpdf import FPDF

from aimart_parquet.engine.convert import convert_file
from aimart_parquet.engine.limits import Limits
from aimart_parquet.engine.models import ConvertOptions, InputFormat
from aimart_parquet.engine.readers.pdf import parse_pages
from aimart_parquet.errors import InvalidInput, InvalidRequest

if TYPE_CHECKING:
    from pathlib import Path


def _bordered(
    path: Path,
    pages: int = 2,
    rows: int = 20,
    header: tuple[str, ...] = ("Invoice", "Customer", "Amount", "Date"),
) -> Path:
    pdf = FPDF()
    pdf.set_font("Helvetica", size=9)
    for page in range(pages):
        pdf.add_page()
        pdf.cell(0, 8, f"Invoice report, page {page + 1}", new_x="LMARGIN", new_y="NEXT")
        with pdf.table() as table:
            table.row(list(header))
            for i in range(rows):
                n = page * rows + i
                table.row(
                    [
                        f"INV-{n:04d}",
                        f"Customer {n}",
                        f"{1000 + n * 13.5:,.2f}",
                        f"2025-0{1 + n % 9}-1{n % 9}",
                    ]
                )
    pdf.output(str(path))
    return path


def _borderless(path: Path) -> Path:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=9)
    rows = [
        ["Region", "Units", "Revenue"],
        ["North", "10", "99.90"],
        ["South", "12", "119.88"],
        ["East", "7", "69.93"],
    ]
    for row in rows:
        for width, cell in zip((40, 30, 30), row, strict=True):
            pdf.cell(width, 6, cell)
        pdf.ln()
    pdf.output(str(path))
    return path


def _convert(
    src: Path, tmp_path: Path, **options: object
) -> tuple[object, list[dict[str, object]]]:
    work = tmp_path / "work"
    work.mkdir()
    out = work / "out.parquet"  # inside the job dir, as the service does: DuckDB is sandboxed to it
    report = convert_file(
        src,
        out,
        filename=src.name,
        options=ConvertOptions(**options),
        workdir=work,
        limits=Limits(),
    )
    return report, pq.read_table(out).to_pylist()


def test_multi_page_bordered_table_is_merged_and_typed(tmp_path: Path) -> None:
    report, rows = _convert(_bordered(tmp_path / "inv.pdf"), tmp_path)
    assert report.source_format is InputFormat.PDF
    assert report.rows == 40  # 2 pages x 20 rows; the repeated header on page 2 is dropped
    assert [c.name for c in report.columns] == ["invoice", "customer", "amount", "date"]
    assert [c.type for c in report.columns] == ["string", "string", "double", "date32[day]"]
    assert rows[0]["amount"] == 1000.0
    assert rows[1]["amount"] == 1013.5  # "1,013.50" read as a US-formatted number
    assert any("continues across 2 pages" in w for w in report.warnings)


def test_borderless_table_falls_back_to_text_alignment(tmp_path: Path) -> None:
    report, rows = _convert(_borderless(tmp_path / "b.pdf"), tmp_path)
    assert report.rows == 3
    assert rows[0] == {"region": "North", "units": 10, "revenue": 99.9}
    assert any("text alignment" in w for w in report.warnings)


def test_page_selection(tmp_path: Path) -> None:
    report, rows = _convert(_bordered(tmp_path / "inv.pdf"), tmp_path, pdf_pages="2")
    assert report.rows == 20
    assert rows[0]["invoice"] == "INV-0020"


def test_several_tables_are_listed_and_selectable(tmp_path: Path) -> None:
    pdf = FPDF()
    pdf.set_font("Helvetica", size=9)
    pdf.add_page()
    with pdf.table() as t:
        t.row(["A", "B"])
        for i in range(3):
            t.row([str(i), str(i * 2)])
    pdf.ln(10)
    with pdf.table() as t:
        t.row(["X", "Y", "Z"])
        for i in range(8):
            t.row([str(i), "y", "z"])
    path = tmp_path / "two.pdf"
    pdf.output(str(path))
    report, _ = _convert(path, tmp_path)
    assert report.rows == 8  # the larger table wins by default...
    assert len(report.available_sheets or []) == 2  # ...and every table is listed
    assert any("found 2 tables" in w for w in report.warnings)
    shutil.rmtree(tmp_path / "work")
    report, rows = _convert(path, tmp_path, pdf_table=0)
    assert report.rows == 3
    assert rows[0] == {"a": 0, "b": 0}


def test_unknown_table_index_lists_what_exists(tmp_path: Path) -> None:
    with pytest.raises(InvalidRequest) as info:
        _convert(_bordered(tmp_path / "inv.pdf", pages=1), tmp_path, pdf_table=5)
    assert info.value.details["available_sheets"]


def test_scanned_pdf_without_text_layer_is_rejected_clearly(tmp_path: Path) -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.rect(10, 10, 50, 50, style="F")  # graphics only: no text layer, like a scan
    path = tmp_path / "scan.pdf"
    pdf.output(str(path))
    with pytest.raises(InvalidInput, match="no text layer"):
        _convert(path, tmp_path)


def test_text_without_tables_is_rejected_clearly(tmp_path: Path) -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 6, "Just a paragraph of prose. " * 20)
    path = tmp_path / "prose.pdf"
    pdf.output(str(path))
    with pytest.raises(InvalidInput, match="No tables"):
        _convert(path, tmp_path)


def test_corrupt_pdf_is_invalid_input(tmp_path: Path) -> None:
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"%PDF-1.7\n" + b"\x00garbage" * 100)
    with pytest.raises(InvalidInput):
        _convert(path, tmp_path)


@pytest.mark.parametrize(
    ("spec", "total", "expected"),
    [
        (None, 3, [1, 2, 3]),
        ("2", 3, [2]),
        ("1-2, 3", 5, [1, 2, 3]),
        ("2-9", 3, [2, 3]),
        ("3,1,3", 4, [1, 3]),
    ],
)
def test_parse_pages(spec: str | None, total: int, expected: list[int]) -> None:
    assert parse_pages(spec, total) == expected


@pytest.mark.parametrize("spec", ["0", "3-1", "a", "9"])
def test_parse_pages_rejects(spec: str) -> None:
    with pytest.raises(InvalidRequest):
        parse_pages(spec, 3)
