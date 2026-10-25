"""Tables trapped in digital PDFs: invoices, statements, report appendices.

pdfplumber reads the PDF's own text positions — no OCR, no model — so digital PDFs come out
exact and cheap. Strategy:

1. Ruling-line detection first (bordered tables, the common case in generated reports).
2. Text-alignment detection when the pages read hold no usable bordered table (borderless
   layouts). A 1-row or 1-column box (a note, a signature block) is not a usable table: it once
   disabled the fallback for the whole document. Text alignment is not kept for the other pages
   of a document that has a bordered table, as before, and stops running once one is found: it
   costs ~3x a page's ruling-line pass, and running it on every such page took a 300-page
   report from 30 s to 76 s, toward the deadline. The report names those pages instead, so the
   caller can point ``pdf_pages`` at them.
3. The header is the widest row above the first data row: a title line above a table (or a
   merged title cell) is dropped and reported, not used as column names; a section row ('North'
   alone) between the header and the data stays a data row.
4. A table that continues across pages is merged in page order: by its repeated header (the
   repeats dropped), or, with no repeated header, when the next page's table has the same width
   and starts with a row typed like the body (numbers where the body has numbers).
5. The largest table wins unless the caller picks one (``pdf_table``); every table found is
   listed in the report, so nothing is chosen silently.

Hostile PDFs are bounded before they cost memory or time: every glyph, shape and form XObject
the interpreter draws counts against a page and a document budget (pdfplumber builds a ~2 KB
Python object per glyph, so 1M glyphs in a 1.6 KB file took a worker to 2 GB); pages are built
one at a time, only those read, and released once their tables are read; the page count is
capped at ``MAX_DOCUMENT_PAGES``; and indirect-reference chains are cut at ``MAX_REF_HOPS``,
because pdfminer follows them with no cycle check.

Scanned PDFs (pages that are only images) have no text to read and are rejected with a clear
error rather than an empty table. Cells keep text only; types come from the shared type ladder.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NoReturn

import pdfplumber
import pyarrow as pa
from pdfminer.pdfdocument import PDFEncryptionError, PDFPasswordIncorrect
from pdfminer.pdfinterp import PDFPageInterpreter
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFSyntaxError
from pdfminer.pdftypes import PDFObjRef
from pdfplumber.page import Page, PDFPageAggregatorWithMarkedContent
from pdfplumber.utils.exceptions import PdfminerException

from aimart_parquet.engine.duck import ident
from aimart_parquet.engine.infer import DEFAULT_NULL_TOKENS
from aimart_parquet.engine.models import InputFormat
from aimart_parquet.engine.readers import RAW_TABLE, RawColumn, RawTable
from aimart_parquet.engine.readers.delimited import _cell_kind
from aimart_parquet.errors import AimartError, InputTooLarge, InvalidInput, InvalidRequest

if TYPE_CHECKING:
    from pathlib import Path

    import duckdb
    from pdfplumber.pdf import PDF

    from aimart_parquet.engine.limits import Limits
    from aimart_parquet.engine.models import ConvertOptions

MAX_PAGES = 300
# Pages a document may have at all. Counting walks the page tree (~60 us a page), and a few MB
# of compressed object streams can declare millions of empty pages. 100k pages is more than any
# 50 MB document with a readable table on its pages holds.
MAX_DOCUMENT_PAGES = 100_000
# Glyphs plus shapes (paths, images, form XObjects) one page, and all pages read, may draw. A
# dense text page is ~5,000 glyphs and a small-font landscape spreadsheet ~40,000; each costs
# ~2.3 KB until the page is closed, so 100k bounds one page near 250 MB. 2M bounds a document's
# parse time near 30 s, inside the convert deadline. Module constants so tests can lower them.
MAX_PAGE_OBJECTS = 100_000
MAX_DOC_OBJECTS = 2_000_000
# A real PDF never chains references more than a hop or two; 32 is generous and still instant.
MAX_REF_HOPS = 32
_RANGE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$")
_TEXT_STRATEGY = {"vertical_strategy": "text", "horizontal_strategy": "text"}
_TYPED = frozenset({"number", "date"})
# What a data row may hold under a typed column: a value, a missing-value token, or nothing.
_DATA = _TYPED | {"missing", "empty"}
_YEAR = re.compile(r"(?:19|20)\d\d")
_BOUNDED_MARK = "_aimart_bounded"


def _bound_reference_chains() -> None:
    """Make ``PDFObjRef.resolve`` follow a whole reference chain, at most ``MAX_REF_HOPS`` long.

    pdfminer resolves with ``while isinstance(x, PDFObjRef): x = x.resolve()`` and no visited
    set (``resolve1``, ``resolve_all``, and inline copies), so a 660-byte PDF whose /Contents
    is ``6 0 R`` with ``6 = 7 0 R`` and ``7 = 6 0 R`` spun forever before any page limit: a job
    slot held to the deadline, or a server thread lost for good when jobs are not isolated.
    Patching the method on the class, not the ``resolve1`` function, covers every caller:
    modules import ``resolve1`` by name, but they all reach the same ``PDFObjRef.resolve``.
    Applied at import, so the forkserver (which preloads the engine) and every child inherit it.
    """
    if getattr(PDFObjRef.resolve, _BOUNDED_MARK, False):
        return
    unbounded = PDFObjRef.resolve

    def resolve(self: PDFObjRef, default: object = None) -> Any:
        obj = unbounded(self, default)
        hops = 1
        while isinstance(obj, PDFObjRef):
            if hops >= MAX_REF_HOPS:
                msg = (
                    f"indirect reference chain from object {self.objid} is longer than "
                    f"{MAX_REF_HOPS} hops (a reference cycle)"
                )
                raise PDFSyntaxError(msg)
            obj = unbounded(obj, default)
            hops += 1
        return obj

    setattr(resolve, _BOUNDED_MARK, True)
    setattr(PDFObjRef, "resolve", resolve)  # noqa: B010 - mypy forbids assigning to a method


_bound_reference_chains()


class _Budget:
    """Counts what the interpreter draws, so a hostile page fails before it costs memory."""

    def __init__(self) -> None:
        self.page = 0
        self.on_page = 0
        self.total = 0

    def start(self, page: int) -> None:
        self.page = page
        self.on_page = 0

    def spend(self) -> None:
        self.on_page += 1
        self.total += 1
        if self.on_page > MAX_PAGE_OBJECTS:
            raise InputTooLarge(
                f"PDF page {self.page} draws more than {MAX_PAGE_OBJECTS:,} characters and "
                "shapes; that is not a table page this service can read.",
                hint=f"Pass pdf_pages to skip page {self.page}, or export the table as CSV/Excel.",
                limit=MAX_PAGE_OBJECTS,
            )
        if self.total > MAX_DOC_OBJECTS:
            raise InputTooLarge(
                f"The pages read draw more than {MAX_DOC_OBJECTS:,} characters and shapes "
                f"(stopped on page {self.page}).",
                hint="Pass pdf_pages to read fewer pages per call, e.g. '1-50'.",
                limit=MAX_DOC_OBJECTS,
            )


class _CountingAggregator(PDFPageAggregatorWithMarkedContent):
    """pdfplumber's own layout device, charging each glyph, shape and figure to a ``_Budget``."""

    def __init__(self, *args: Any, budget: _Budget, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.budget = budget

    def render_char(self, *args: Any, **kwargs: Any) -> float:
        self.budget.spend()
        return super().render_char(*args, **kwargs)

    def render_image(self, *args: Any, **kwargs: Any) -> None:
        self.budget.spend()
        super().render_image(*args, **kwargs)

    def paint_path(self, *args: Any, **kwargs: Any) -> None:
        self.budget.spend()
        super().paint_path(*args, **kwargs)

    def begin_figure(self, *args: Any, **kwargs: Any) -> None:
        # Each form XObject drawn ('Do') is a figure object. X1 drawing X2 twice, X2 drawing X3
        # twice, ...: 24 levels in 5 KB were 16M figures and no glyph, 700 MB in 13 s.
        self.budget.spend()
        super().begin_figure(*args, **kwargs)


def _parse_layout(page: Page, budget: _Budget) -> None:
    """Interpret ``page`` once under ``budget``, as ``Page.layout`` would, and cache the result.

    ``Page.layout`` builds its device itself, so there is no hook for a budget. Seeding its
    cache attribute means the counted parse is the only one; if pdfplumber ever renamed that
    attribute the budget would still have run first, only the work would be done twice.
    """
    budget.start(page.page_number)
    device = _CountingAggregator(
        page.pdf.rsrcmgr, pageno=page.page_number, laparams=page.pdf.laparams, budget=budget
    )
    PDFPageInterpreter(page.pdf.rsrcmgr, device).process_page(page.page_obj)
    page._layout = device.get_result()  # private cache attribute: see the docstring


@dataclass
class _Found:
    page: int
    rows: list[list[str]]
    strategy: str
    captions: list[str] = field(default_factory=list)


@dataclass
class _Table:
    header: list[str]
    rows: list[list[str]] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    strategy: str = "ruling lines"
    captions: list[str] = field(default_factory=list)

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
    pages: set[int] = set()
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
        # Clamp before iterating: '1-99999999999' once walked 1e11 numbers to keep three,
        # holding a job slot until the deadline.
        pages.update(range(start, min(end, total) + 1))
        if len(pages) > MAX_PAGES:
            raise InvalidRequest(
                f"pdf_pages selects more than {MAX_PAGES} pages; the limit is {MAX_PAGES}.",
                hint="Narrow the range; read the rest in another call.",
            )
    if not pages:
        raise InvalidRequest(
            f"pdf_pages {spec!r} selects no page of this {total}-page PDF.", hint="Check the range."
        )
    return sorted(pages)


def _clean(raw: list[list[Any]]) -> list[list[str]]:
    rows = [[" ".join(str(c).split()) if c is not None else "" for c in row] for row in raw]
    return [r for r in rows if any(r)]


def _usable(rows: list[list[str]]) -> bool:
    """At least a header and one row, two columns wide. Anything less is a box, not a table."""
    return len(rows) >= 2 and len(rows[0]) >= 2


def _cell(row: list[str], col: int) -> str:
    return row[col] if col < len(row) else ""


def _typed_share(values: list[str]) -> float:
    return sum(_cell_kind(v) in _TYPED for v in values) / len(values) if values else 0.0


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
    return any(
        _typed_share([v for r in body if (v := _cell(r, col))]) >= 0.8
        for col in range(len(rows[0]))
    )


def _kind(cell: str) -> str:
    """``_cell_kind``, with the type ladder's null tokens ('n/a', '-') as their own kind."""
    if cell.strip() and cell.strip().lower() in DEFAULT_NULL_TOKENS:
        return "missing"
    return _cell_kind(cell)


def _values(rows: list[list[str]], col: int) -> list[str]:
    """The cells of one column that hold a value (not empty, not a missing-value token)."""
    return [v for r in rows if _kind(v := _cell(r, col)) not in {"empty", "missing"}]


def _filled(row: list[str]) -> int:
    return sum(1 for c in row if c)


def _year_names(row: list[str], typed: list[int], body: list[list[str]]) -> bool:
    """Years as column names ('Region', '2023', '2024'): a header the type check reads as data.

    Found reviewing the first header and continuation fixes: a title above such a header became
    the column names again, and a second year-comparison table on the next page ('Department',
    '2023', '2024') was merged into the first as a body row. Distinct years under every filled
    typed column, where the body of each of those columns is not mostly years (a 'Year' column
    holds data).
    """
    cells = {c: v for c in typed if (v := _cell(row, c))}
    if not cells or len(set(cells.values())) != len(cells):
        return False
    if not all(_YEAR.fullmatch(v) for v in cells.values()):
        return False
    for c in cells:
        values = _values(body, c)
        if values and sum(bool(_YEAR.fullmatch(v)) for v in values) * 2 >= len(values):
            return False
    return True


def _data_row(row: list[str], typed: list[int], body: list[list[str]]) -> bool:
    """A value or a missing-value token under some typed column, no text under any, and not a
    header of years (``_year_names``)."""
    kinds = [_kind(_cell(row, c)) for c in typed]
    if not all(k in _DATA for k in kinds) or all(k == "empty" for k in kinds):
        return False
    return not _year_names(row, typed, body)


def _split_captions(rows: list[list[str]]) -> tuple[list[list[str]], list[str]]:
    """Drop rows above the header: titles, and merged title cells of bordered tables.

    Text alignment returns the whole page as one table, so a title line above a borderless
    table was rows[0] and became the column names ('quarterly_sales_by_regi', 'on', ...), with
    the real header as a data row that turned every numeric column into strings: a 200 for bad
    data. The header is the widest row above the first data row (``_data_row``: a number, date
    or missing-value token under a typed column and no text under any), the lowest of equals.
    Widest, not merely the row directly above: a section row ('North', '', '') under a header
    with a blank corner ('', 'Units', 'Revenue') became the header and the real one was dropped.
    Typed columns are judged on the bottom half of the rows, which is body as long as there are
    no more leading rows than data rows.

    Rows are only dropped when every one is narrower than the body (fewer filled cells than
    most data rows), as titles and merged cells are: a body row with text where numbers belong
    ('pending' under an amount) must not make the real header and the rows above it vanish. In
    every unclear case nothing is dropped and rows[0] stays the header, as before.
    """
    if len(rows) < 3:
        return rows, []
    tail = rows[len(rows) // 2 :]
    typed = [c for c in range(len(rows[0])) if _typed_share(_values(tail, c)) >= 0.8]
    if not typed:
        return rows, []
    first = next((i for i, row in enumerate(rows) if _data_row(row, typed, tail)), None)
    if not first:  # no data row, or data from the first row on: nothing to choose
        return rows, []
    widest = max(_filled(r) for r in rows[:first])
    header = max(i for i in range(first) if _filled(rows[i]) == widest)
    if header == 0:
        return rows, []
    body_width = Counter(_filled(r) for r in rows[header + 1 :]).most_common(1)[0][0]
    if any(_filled(r) >= body_width for r in rows[:header]):
        return rows, []
    return rows[header:], [" ".join(c for c in r if c) for r in rows[:header]]


def _ruled_tables(page: Page, number: int) -> list[_Found]:
    found = []
    for raw in page.extract_tables():
        rows, captions = _split_captions(_clean(raw))
        if _usable(rows):
            found.append(_Found(number, rows, "ruling lines", captions))
    return found


def _aligned_tables(page: Page, number: int) -> list[_Found]:
    found = []
    for raw in page.extract_tables(_TEXT_STRATEGY):
        rows, captions = _split_captions(_clean(raw))
        if _looks_tabular(rows):
            found.append(_Found(number, rows, "text alignment", captions))
    return found


@dataclass
class _Scan:
    has_text: bool = False
    ruled: list[_Found] = field(default_factory=list)
    aligned: list[_Found] = field(default_factory=list)
    unruled: list[int] = field(default_factory=list)  # pages with text, no usable bordered table


def _count_pages(doc: PDF) -> int:
    total = 0
    for total, _ in enumerate(PDFPage.create_pages(doc.doc), 1):
        if total > MAX_DOCUMENT_PAGES:
            raise InputTooLarge(
                f"The PDF has more than {MAX_DOCUMENT_PAGES:,} pages.",
                hint="Split it (e.g. qpdf --split-pages) and send the part with the table.",
                limit=MAX_DOCUMENT_PAGES,
            )
    return total


def _scan(doc: PDF, pages: list[int]) -> _Scan:
    """Read ``pages`` in order, one page's objects in memory at a time, under one budget.

    Pages come from pdfminer's page tree walk, not pdfplumber's ``doc.pages``: that builds a
    Page (~2 KB) for every page of the document before one is read (100k empty pages in a
    12 MB file took 200 MB and 12 s), and builds them all again when the document closes.
    """
    scan = _Scan()
    if not pages:  # a document without pages
        return scan
    budget = _Budget()
    wanted = set(pages)
    for number, page_obj in enumerate(PDFPage.create_pages(doc.doc), 1):
        if number > pages[-1]:
            break
        if number not in wanted:
            continue
        page = Page(doc, page_obj, page_number=number)
        try:
            _parse_layout(page, budget)
            if not page.chars:
                continue
            scan.has_text = True
            bordered = _ruled_tables(page, number)
            scan.ruled.extend(bordered)
            if not bordered:
                scan.unruled.append(number)
                if not scan.ruled:  # pointless once a bordered table exists: it wins
                    scan.aligned.extend(_aligned_tables(page, number))
        finally:
            page.close()  # release its glyph objects before the next page is parsed
    return scan


def _continues(table: _Table, rows: list[list[str]]) -> bool:
    """Is ``rows``, a table at the top of the next page, more body for ``table``?

    Without this, a table whose header is not repeated on page 2 split in two: page 2's first
    data row became the second table's header ('area7', 'c_107', ...) and appeared in neither.
    Continuation needs the same width and a first row that is a data row under the columns
    typed in ``table``'s body (text there, or years, is what a new table's header looks like).
    """
    if len(rows[0]) != len(table.header):
        return False
    typed = [c for c in range(len(table.header)) if _typed_share(_values(table.rows, c)) >= 0.8]
    return bool(typed) and _data_row(rows[0], typed, table.rows)


def _span(pages: list[int]) -> str:
    """[2, 3, 4, 7] -> '2-4,7': a pdf_pages value the caller can send back as is."""
    parts: list[str] = []
    start = prev = pages[0]
    for page in [*pages[1:], 0]:
        if page != prev + 1:
            parts.append(str(start) if start == prev else f"{start}-{prev}")
            start = page
        prev = page
    return ",".join(parts)


def _merge(found: list[_Found]) -> list[_Table]:
    tables: list[_Table] = []
    last: _Table | None = None  # the table that took the previous found item
    last_page = 0
    for item in found:
        header = item.rows[0]
        same = next((t for t in tables if t.header == header), None)
        if same is not None:  # a repeated header: the table continues on this page
            same.rows.extend(r for r in item.rows[1:] if r != header)
            same.pages.append(item.page)
            same.captions.extend(item.captions)
            last = same
        elif last is not None and last_page == item.page - 1 and _continues(last, item.rows):
            # The first table on this page, right after the last one on the previous page.
            last.rows.extend(item.rows)
            last.pages.append(item.page)
            last.captions.extend(item.captions)
        else:
            last = _Table(
                header=header,
                rows=list(item.rows[1:]),
                pages=[item.page],
                strategy=item.strategy,
                captions=list(item.captions),
            )
            tables.append(last)
        last_page = item.page
    return [t for t in tables if t.rows]


def _unwrap(exc: BaseException) -> BaseException:
    """pdfplumber wraps pdfminer's exceptions in ``PdfminerException(original)``."""
    while (
        isinstance(exc, PdfminerException) and exc.args and isinstance(exc.args[0], BaseException)
    ):
        exc = exc.args[0]
    return exc


def _reason(exc: BaseException) -> str:
    inner = _unwrap(exc)
    # PDFPasswordIncorrect and friends have an empty str(): name the class instead.
    return str(inner) or type(inner).__name__


def _raise_unreadable(exc: Exception) -> NoReturn:
    inner = _unwrap(exc)
    if isinstance(inner, AimartError):
        raise inner from exc
    if isinstance(inner, PDFPasswordIncorrect):
        raise InvalidInput(
            "The PDF is password-protected.", hint="Remove the password and resend."
        ) from exc
    if isinstance(inner, PDFEncryptionError):
        raise InvalidInput(
            "The PDF is encrypted with a scheme this service cannot open.",
            hint="Save a copy without encryption and resend.",
        ) from exc
    if isinstance(inner, MemoryError):
        raise InputTooLarge(
            "The PDF needs more memory than this server allows.",
            hint="Pass pdf_pages to read fewer pages per call.",
        ) from exc
    raise InvalidInput(
        f"Could not read the PDF: {_reason(inner)}",
        hint="Check the file opens in a PDF viewer; if it is not a PDF, pass options.format.",
    ) from exc


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
        # The file is ours to close and pdfplumber's PDF is deliberately not closed: its close()
        # builds a Page for every page of the document (see ``_scan``) to close each, and when
        # building them is what failed (a reference cycle) it raised again and leaked the file.
        with path.open("rb") as fh:
            doc = pdfplumber.open(fh)
            total = _count_pages(doc)
            pages = parse_pages(options.pdf_pages, total)
            if not options.pdf_pages and total > MAX_PAGES:
                warnings.append(
                    f"read the first {MAX_PAGES} of {total} pages; pass pdf_pages for the rest"
                )
            scan = _scan(doc, pages)
    except AimartError:
        raise
    except Exception as exc:  # noqa: BLE001 - every parser failure becomes a typed error
        _raise_unreadable(exc)
    if not scan.has_text:
        raise InvalidInput(
            "The PDF has no text layer (scanned pages are images).",
            hint="Only digital PDFs are supported; scanned documents need OCR, which this service does not do.",
        )
    if scan.ruled and scan.unruled:
        # Text alignment also reads a table of contents or figure-heavy prose as a "table",
        # often a larger one, so a bordered table wins; say where borderless ones may be.
        span = _span(scan.unruled)
        warnings.append(
            f"page(s) {span} have no ruled table; borderless tables are read only when no page "
            f"has one, so pass pdf_pages='{span}' to read those pages"
        )
    tables = _merge(scan.ruled or scan.aligned)
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
    if chosen.captions:
        shown = "; ".join(repr(c[:80]) for c in chosen.captions[:3])
        warnings.append(f"dropped {len(chosen.captions)} line(s) above the header: {shown}")
    warnings.append(f"tables detected by {chosen.strategy}")

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
