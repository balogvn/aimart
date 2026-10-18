"""Tables trapped in digital PDFs: invoices, statements, report appendices.

pdfplumber reads the PDF's own text positions — no OCR, no model — so digital PDFs come out
exact and cheap. Strategy:

1. Ruling-line detection first (bordered tables, the common case in generated reports).
2. Text-alignment detection only if no page has a bordered table (borderless layouts).
3. A table that continues across pages usually repeats its header: tables with the same header
   are merged in page order with the repeats dropped, so a 40-page statement becomes one table.
4. The largest table wins unless the caller picks one (``pdf_table``); every table found is
   listed in the report, so nothing is chosen silently.

Scanned PDFs (pages that are only images) have no text to read and are rejected with a clear
error rather than an empty table. Cells keep text only; types come from the shared type ladder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pdfplumber
import pyarrow as pa

from aimart_parquet.engine.duck import ident
from aimart_parquet.engine.models import InputFormat
from aimart_parquet.engine.readers import RAW_TABLE, RawColumn, RawTable
from aimart_parquet.engine.readers.delimited import _cell_kind
from aimart_parquet.errors import AimartError, InvalidInput, InvalidRequest

if TYPE_CHECKING:
    from pathlib import Path

    import duckdb

    from aimart_parquet.engine.limits import Limits
    from aimart_parquet.engine.models import ConvertOptions

MAX_PAGES = 300
_RANGE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$")
_TEXT_STRATEGY = {"vertical_strategy": "text", "horizontal_strategy": "text"}


@dataclass
class _Table:
    header: list[str]
    rows: list[list[str]] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)

    @property
    def cells(self) -> int:
        return len(self.rows) * len(self.header)

    def describe(self, index: int) -> str:
        span = (
            f"page {self.pages[0]}"
            if len(set(self.pages)) == 1
            else f"pages {self.pages[0]}-{self.pages[-1]}"
        )
        return f"table {index}: {span}, {len(self.rows)} rows x {len(self.header)} columns"


def parse_pages(spec: str | None, total: int) -> list[int]:
    """'1-3,7' -> [1, 2, 3, 7] (1-based, in range). None -> every page up to MAX_PAGES."""
    if not spec:
        return list(range(1, min(total, MAX_PAGES) + 1))
    pages: list[int] = []
    for part in spec.split(","):
        match = _RANGE.match(part)
        if not match:
            raise InvalidRequest(
                f"pdf_pages {spec!r} is not a page list.", hint="Use a form like '1-3,7'."
            )
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start:
            raise InvalidRequest(
                f"pdf_pages range {part.strip()!r} is invalid.", hint="Pages start at 1."
            )
        pages.extend(p for p in range(start, end + 1) if p <= total)
    pages = sorted(set(pages))
    if not pages:
        raise InvalidRequest(
            f"pdf_pages {spec!r} selects no page of this {total}-page PDF.", hint="Check the range."
        )
    if len(pages) > MAX_PAGES:
        raise InvalidRequest(
            f"pdf_pages selects {len(pages)} pages; the limit is {MAX_PAGES}.",
            hint="Narrow the range.",
        )
    return pages


def _clean(raw: list[list[Any]]) -> list[list[str]]:
    rows = [[" ".join(str(c).split()) if c is not None else "" for c in row] for row in raw]
    return [r for r in rows if any(r)]


def _looks_tabular(rows: list[list[str]]) -> bool:
    """Guard for text-alignment detection, which happily splits prose into a 'table' of words.

    Measured: a paragraph came back as 19 columns of single words. Real borderless tables have
    at least one column that is mostly numbers or dates, and are reasonably filled.
    """
    body = rows[1:]
    if len(body) < 2 or len(rows[0]) < 2:
        return False
    cells = [c for r in body for c in r]
    if sum(1 for c in cells if c) / max(1, len(cells)) < 0.5:
        return False
    for col in range(len(rows[0])):
        values = [r[col] for r in body if col < len(r) and r[col]]
        if values and sum(_cell_kind(v) in {"number", "date"} for v in values) / len(values) >= 0.8:
            return True
    return False


def _merge(found: list[tuple[int, list[list[str]]]]) -> list[_Table]:
    tables: list[_Table] = []
    for page, rows in found:
        if len(rows) < 2 or len(rows[0]) < 2:
            continue
        header = rows[0]
        same = next((t for t in tables if t.header == header), None)
        if same is not None:  # a repeated header: the table continues on this page
            same.rows.extend(r for r in rows[1:] if r != header)
            same.pages.append(page)
        else:
            tables.append(_Table(header=header, rows=list(rows[1:]), pages=[page]))
    return [t for t in tables if t.rows]


def read_pdf(
    con: duckdb.DuckDBPyConnection,
    path: Path,
    workdir: Path,
    options: ConvertOptions,
    fmt: InputFormat,
    limits: Limits,
) -> RawTable:
    warnings: list[str] = []
    try:
        with pdfplumber.open(path) as doc:
            total = len(doc.pages)
            pages = parse_pages(options.pdf_pages, total)
            if not options.pdf_pages and total > MAX_PAGES:
                warnings.append(
                    f"read the first {MAX_PAGES} of {total} pages; pass pdf_pages for the rest"
                )
            has_text = False
            found: list[tuple[int, list[list[str]]]] = []
            for number in pages:
                page = doc.pages[number - 1]
                has_text = has_text or bool(page.chars)
                found.extend((number, _clean(t)) for t in page.extract_tables())
            strategy = "ruling lines"
            if not found and has_text:
                strategy = "text alignment"
                for number in pages:
                    for raw in doc.pages[number - 1].extract_tables(_TEXT_STRATEGY):
                        rows = _clean(raw)
                        if _looks_tabular(rows):
                            found.append((number, rows))
    except AimartError:
        raise
    except Exception as exc:
        raise InvalidInput(
            f"Could not read the PDF: {exc}", hint="Check the file opens in a PDF viewer."
        ) from exc
    if not has_text:
        raise InvalidInput(
            "The PDF has no text layer (scanned pages are images).",
            hint="Only digital PDFs are supported; scanned documents need OCR, which this service does not do.",
        )
    tables = _merge(found)
    if not tables:
        raise InvalidInput(
            "No tables were found in the PDF.",
            hint="Pass pdf_pages to point at the pages with tables, or export the table as CSV/Excel.",
        )
    if options.pdf_table is not None:
        if options.pdf_table >= len(tables):
            raise InvalidRequest(
                f"pdf_table {options.pdf_table} does not exist; this PDF has {len(tables)} table(s).",
                hint="Use an index from available_sheets.",
                available_sheets=[t.describe(i) for i, t in enumerate(tables)],
            )
        index = options.pdf_table
    else:
        index = max(range(len(tables)), key=lambda i: tables[i].cells)
    chosen = tables[index]
    if len(tables) > 1:
        warnings.append(
            f"found {len(tables)} tables; used {chosen.describe(index)} (pass pdf_table to choose)"
        )
    if len(chosen.pages) > 1:
        warnings.append(f"merged a table that continues across {len(chosen.pages)} pages")
    warnings.append(f"tables detected by {strategy}")

    width = len(chosen.header)
    arrays: dict[str, pa.Array] = {}
    for c in range(width):
        values = [r[c] if c < len(r) and r[c] != "" else None for r in chosen.rows]
        arrays[f"c{c}"] = pa.array(values, type=pa.string())
    con.register("raw_arrow", pa.table(arrays))
    con.execute(f"CREATE TABLE {ident(RAW_TABLE)} AS SELECT * FROM raw_arrow")
    con.unregister("raw_arrow")
    return RawTable(
        table=RAW_TABLE,
        columns=[
            RawColumn(
                source=f"c{c}",
                original=chosen.header[c] or f"column_{c + 1}",
                infer=True,
                text=True,
            )
            for c in range(width)
        ],
        source_format=InputFormat.PDF,
        sheet=chosen.describe(index),
        available_sheets=[t.describe(i) for i, t in enumerate(tables)],
        warnings=warnings,
    )
