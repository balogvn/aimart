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
3. The header is the row above the first data row naming the most typed columns, the topmost
   of equals: a title line above a table (a merged title cell, or a long line that text
   alignment cut into one piece per column) is dropped and reported, not used as column names;
   a section row ('North' alone) or a first row holding a value ('Total', 'TBD', '9999.00')
   stays a data row. A first row written like the rows under it is data, not names: the table
   has no header row, and its columns are column_1, column_2, ... (``options.header`` decides
   when the caller knows better).
4. A table that continues across pages is merged in page order: by its repeated header (the
   repeats dropped), or, with no repeated header, when the next page's table has the same width
   and starts with a row typed like the body (numbers where the body has numbers), down to a
   single row spilled onto the next page; one line above such rows (a running title, 'Table 1
   (continued)') is dropped and reported. A line that may be a new table's header splits the
   page off instead, with the 'found N tables' warning: a split is a reported partial result,
   a merged table is silent wrong data. The last one or two rows of a borderless table, too
   few for text alignment to see, are read at the previous page's column edges.
5. The largest table wins unless the caller picks one (``pdf_table``); every table found is
   listed in the report, so nothing is chosen silently.

Hostile PDFs are bounded before they cost memory or time: every glyph, shape and form XObject
the interpreter draws counts against a page and a document budget (pdfplumber builds a ~2 KB
Python object per glyph, so 1M glyphs in a 1.6 KB file took a worker to 2 GB); pages are built
one at a time, only those read, and released once their tables are read; the page count is
capped at ``MAX_DOCUMENT_PAGES``; indirect-reference chains are cut at ``MAX_REF_HOPS``,
because pdfminer follows them with no cycle check; and the table search, whose time grows with
the square of the line crossings on a page, is charged to the same budget before it runs.

Scanned PDFs (pages that are only images) have no text to read and are rejected with a clear
error rather than an empty table. Cells keep text only; types come from the shared type ladder.
"""

from __future__ import annotations

import re
import sys
from bisect import bisect_right
from contextvars import ContextVar
from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING, Any, NoReturn

import pdfplumber
import pyarrow as pa
from pdfminer import pdftypes
from pdfminer.pdfdocument import PDFEncryptionError, PDFPasswordIncorrect
from pdfminer.pdfinterp import PDFPageInterpreter
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFSyntaxError
from pdfminer.pdftypes import PDFObjRef, resolve1
from pdfplumber import pdf as plumber_pdf
from pdfplumber import table as plumber_table
from pdfplumber.page import Page, PDFPageAggregatorWithMarkedContent
from pdfplumber.table import Table, TableSettings
from pdfplumber.utils.exceptions import PdfminerException

from aimart_parquet.engine.duck import ident
from aimart_parquet.engine.infer import DEFAULT_NULL_TOKENS
from aimart_parquet.engine.models import InputFormat
from aimart_parquet.engine.readers import RAW_TABLE, RawColumn, RawTable
from aimart_parquet.engine.readers.delimited import _cell_kind
from aimart_parquet.errors import AimartError, InputTooLarge, InvalidInput, InvalidRequest

if TYPE_CHECKING:
    from collections.abc import Callable
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
# pdfplumber's table search (ruling lines and text alignment alike) pairs every horizontal edge
# with every vertical one (~70 ns a pair), then scans, for each crossing it found, every later
# crossing (~15-20 ns each, so N crossings cost about N**2). 140 ruled lines each way in a 2 KB
# page took 5.6 s, and the time grows as the fourth power of the lines, toward the deadline,
# with memory flat and a few hundred shapes, far inside the object budget. Measured on real
# layouts: a ruled 120 x 30 table has 3,751 crossings and its text-alignment search 7,440
# (0.9 s); 12k crossings bound one search near 2.5 s, 10M pairs near 0.7 s. The document budget
# is on the sum of crossings squared, ~40 s; 300 pages of 60 x 12 borderless tables use 0.7e9.
MAX_GRID_PAIRS = 10_000_000
MAX_GRID_CROSSINGS = 12_000
MAX_DOC_GRID_WORK = 2_000_000_000
# A real PDF never chains references more than a hop or two; 32 is generous and still instant.
MAX_REF_HOPS = 32
# Parts one nested object may unfold to when pdfminer resolves it whole (a font's /W widths or
# /FontBBox). A CJK font's /W lists ~65k widths, a few hundred thousand parts at most; objects
# that name a shared object twice per level unfold to 2**depth, 2**40 in 3 KB.
MAX_RESOLVED_PARTS = 1_000_000
_RANGE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$")
_TEXT_STRATEGY = {"vertical_strategy": "text", "horizontal_strategy": "text"}
_TYPED = frozenset({"number", "date"})
# What a data row may hold under a typed column: a value, a missing-value token, or nothing.
_DATA = _TYPED | {"missing", "empty"}
_YEAR = re.compile(r"(?:19|20)\d\d")
_DIGITS = re.compile(r"\d+")
# The end of a line saying a table goes on from an earlier page, read without spaces and in
# lower case: 'Table 1 (continued)', '(cont.)', "North (cont'd)", 'Table 1 - continued',
# 'Continued from page 1'. Only alone or after punctuation: a header's last column may be
# named 'Continued' (patients still on treatment), and 'Discontinued' is a section row.
_CONTINUED = re.compile(
    r"(?:^|[(\[\-\u2013\u2014.,:;])(?:continued|cont['\u2019]?d\.?|cont\.?)(?:from\w*)?[)\]]?[.:]?$"
)
_BOUNDED_MARK = "_aimart_bounded"
_MAX_REASON = 300


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


def _resolve_all(x: object, default: object = None) -> Any:
    """``pdfminer.pdftypes.resolve_all``, stopped after ``MAX_RESOLVED_PARTS`` parts.

    pdfminer's copy walks a nested object path by path, so a shared object is walked once per
    path to it. Not memoised instead: pdfminer logs what it could not parse with ``%r``, and
    ``repr`` walks a memoised result path by path just the same.
    """
    left = MAX_RESOLVED_PARTS

    def walk(obj: object) -> Any:
        nonlocal left
        left -= 1
        if left < 0:
            msg = f"a nested object unfolds to more than {MAX_RESOLVED_PARTS:,} parts"
            raise PDFSyntaxError(msg)
        while isinstance(obj, PDFObjRef):
            obj = obj.resolve(default=default)
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        if isinstance(obj, dict):  # in place, as pdfminer's own: font code relies on it
            for k, v in obj.items():
                obj[k] = walk(v)
        return obj

    return walk(x)


setattr(_resolve_all, _BOUNDED_MARK, True)


def _unread_metadata(value: Any) -> Any:
    return value


def _walk_shared_objects_once() -> None:
    """Stop the recursive walks pdfminer and pdfplumber make over nested objects from walking
    a shared object once per path to it: 2**40 times for 40 levels in a 3 KB file.

    A /Info dictionary naming the next dictionary twice per level, 40 levels deep, ran to the
    deadline inside ``pdfplumber.open`` (memory stays flat, so no budget saw it): pdfplumber
    resolves every metadata value with no visited set. Metadata is never read here, so it is
    left unresolved. pdfminer's ``resolve_all`` (font widths and boxes) gets a part budget
    (``_resolve_all``), in every module that imported it by name. The other walks are kept off
    hostile input where they happen: page labels in ``read_pdf``, page boxes in
    ``_flat_page_attrs``. Applied at import, so the forkserver and every child inherit it.
    """
    setattr(plumber_pdf, "resolve_and_decode", _unread_metadata)  # noqa: B010
    original = pdftypes.resolve_all
    if getattr(original, _BOUNDED_MARK, False):
        return
    ours = [m for n, m in sys.modules.items() if n.partition(".")[0] in {"pdfminer", "pdfplumber"}]
    for module in ours:
        if vars(module).get("resolve_all") is original:
            setattr(module, "resolve_all", _resolve_all)  # noqa: B010


_walk_shared_objects_once()


class _Budget:
    """Counts what the interpreter draws, so a hostile page fails before it costs memory."""

    def __init__(self) -> None:
        self.page = 0
        self.on_page = 0
        self.total = 0
        self.grid = 0

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

    def search(self, pairs: int) -> None:
        """Before one table search pairs ``pairs`` horizontal and vertical edges."""
        if pairs > MAX_GRID_PAIRS:
            raise InputTooLarge(
                f"PDF page {self.page} has more ruling edges than this service can search for "
                f"tables ({pairs:,} edge pairs; the limit is {MAX_GRID_PAIRS:,}).",
                hint=f"Pass pdf_pages to skip page {self.page}, or export the table as CSV/Excel.",
                limit=MAX_GRID_PAIRS,
            )

    def crossings(self, found: int) -> None:
        """Before the cell search scans ``found`` crossings, about ``found**2`` steps."""
        if found > MAX_GRID_CROSSINGS:
            raise InputTooLarge(
                f"PDF page {self.page} rules a grid of {found:,} line crossings; the limit is "
                f"{MAX_GRID_CROSSINGS:,}, and searching it for tables would take minutes.",
                hint=f"Pass pdf_pages to skip page {self.page}, or export the table as CSV/Excel.",
                limit=MAX_GRID_CROSSINGS,
            )
        self.grid += found * found
        if self.grid > MAX_DOC_GRID_WORK:
            raise InputTooLarge(
                "The pages read hold more table grid than one call can search "
                f"(stopped on page {self.page}).",
                hint="Pass pdf_pages to read fewer pages per call, e.g. '1-50'.",
                limit=MAX_DOC_GRID_WORK,
            )


# The budget of the PDF being read in this thread (or job), for the table-search hook below.
_SEARCHING: ContextVar[_Budget | None] = ContextVar("aimart_pdf_budget", default=None)


def _bound_table_search() -> None:
    """Charge pdfplumber's table search to the ``_Budget`` of the PDF being read.

    ``TableFinder`` looks ``edges_to_intersections`` up in its module on every search, so
    wrapping that one function sees the edges before the pair loop and the crossings before
    the cell search, for ruling lines and text alignment alike, at no extra cost. Outside
    ``read_pdf`` (no budget set) it is pdfplumber's own. Applied at import, idempotently.
    """
    original = plumber_table.edges_to_intersections
    if getattr(original, _BOUNDED_MARK, False):
        return

    def edges_to_intersections(edges: Any, x_tolerance: Any = 1, y_tolerance: Any = 1) -> Any:
        budget = _SEARCHING.get()
        if budget is not None:
            vertical = sum(1 for edge in edges if edge["orientation"] == "v")
            budget.search((len(edges) - vertical) * vertical)
        found = original(edges, x_tolerance, y_tolerance)
        if budget is not None:
            budget.crossings(len(found))
        return found

    setattr(edges_to_intersections, _BOUNDED_MARK, True)
    setattr(plumber_table, "edges_to_intersections", edges_to_intersections)  # noqa: B010


_bound_table_search()


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
    # The x positions of a borderless table's column edges, for reading the next page's top
    # with them (``_spilled_rows``).
    edges: tuple[float, ...] = ()
    # Rows read that way: only ever more body for the table above them, never a table.
    spill: bool = False


@dataclass
class _Table:
    header: list[str]
    rows: list[list[str]] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    strategy: str = "ruling lines"
    captions: list[str] = field(default_factory=list)
    # Per column: cells holding a value, and how many of those are numbers or dates. Kept as
    # rows are added (``add``): recounting the body on every page made merging quadratic, and
    # a 300-page ledger of 40 rows a page, its header printed once, took 18 s (now 0.14 s).
    _filled: list[int] = field(default_factory=list, init=False, repr=False)
    _typed: list[int] = field(default_factory=list, init=False, repr=False)
    # Which cells are filled, in the rows of the body (``shapes``), and in those holding no
    # number or date (``text_shapes``: a section row, 'North'). ``_continuation`` keeps a line
    # shaped like a section row as body; ``_spilled_body`` keeps only rows shaped like body.
    shapes: set[frozenset[int]] = field(default_factory=set, init=False, repr=False)
    text_shapes: set[frozenset[int]] = field(default_factory=set, init=False, repr=False)
    # Per column: how its values are written (``_form``), and the text it holds ('pending').
    # Kept as rows are added for the same reason as the counts; ``_pending`` reads both.
    written: list[set[str]] = field(default_factory=list, init=False, repr=False)
    texts: list[set[str]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        rows, self.rows = self.rows, []
        self._filled = [0] * len(self.header)
        self._typed = [0] * len(self.header)
        self.written = [set() for _ in self.header]
        self.texts = [set() for _ in self.header]
        self.add(rows)

    def add(self, rows: list[list[str]]) -> None:
        for row in rows:
            values = False
            for c in range(len(self.header)):
                value = _cell(row, c)
                kind = _kind(value)
                if kind in {"empty", "missing"}:
                    continue
                self._filled[c] += 1
                self.written[c].add(_form(value))
                if kind in _TYPED:
                    self._typed[c] += 1
                    values = True
                else:
                    self.texts[c].add(value)
            shape = _shape(row)
            self.shapes.add(shape)
            if not values:
                self.text_shapes.add(shape)
        self.rows.extend(rows)

    def typed(self) -> list[int]:
        """The columns whose body is mostly numbers or dates, as ``_typed_share`` judges."""
        return [c for c, n in enumerate(self._filled) if n and self._typed[c] / n >= 0.8]

    def forms(self, typed: list[int]) -> dict[int, set[str]]:
        """``_forms(self.rows, typed)``, without reading the body again."""
        return {c: self.written[c] for c in typed}

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


def _cells(raw: list[list[Any]]) -> list[list[str]]:
    return [[" ".join(str(c).split()) if c is not None else "" for c in row] for row in raw]


def _clean(raw: list[list[Any]]) -> list[list[str]]:
    return [r for r in _cells(raw) if any(r)]


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


def _names(row: list[str], typed: list[int], body: list[list[str]]) -> int:
    """How many typed columns ``row`` names: text under them and a value under none, or years
    when it is a header of years (``_year_names``). A row with a value is body, whatever else
    it holds ('Total', 'TBD', '9999.00')."""
    if _year_names(row, typed, body):
        return sum(1 for c in typed if _cell(row, c))
    kinds = [_kind(_cell(row, c)) for c in typed]
    if any(k in _TYPED for k in kinds):
        return 0
    return sum(k not in _DATA for k in kinds)


def _never(_: int) -> bool:
    return False


def _line(row: list[str]) -> str:
    """A row as the one line of text it is when it is not table data (a title, a caption)."""
    return " ".join(c for c in row if c)


def _form(value: str) -> str:
    """A value with its digits collapsed: '12.34' -> '0.0', '4000' -> '0', '2025-02-02' ->
    '0-0-0'. The sign is dropped, so a negative amount has the form of a positive one."""
    return _DIGITS.sub("0", value.strip().lstrip("+-"))


def _forms(body: list[list[str]], typed: list[int]) -> dict[int, set[str]]:
    """How the values of each typed column of ``body`` are written (``_form``)."""
    return {c: {_form(v) for v in _values(body, c)} for c in typed}


def _typed_like(row: list[str], forms: dict[int, set[str]]) -> bool:
    """Does ``row`` hold, under every typed column, nothing, a missing-value token, or a value
    written the way some value of that column in the body is (``_forms``)?

    A header of codes or dates over number columns ('Account', '4000', '5000' above amounts
    like '12.34'; 'Item', '2024-12-31', '2023-12-31' on a balance sheet) is a data row to
    ``_data_row``, but is not written like the body: it stays the header.
    """
    for c, written in forms.items():
        value = _cell(row, c)
        if _kind(value) not in {"empty", "missing"} and _form(value) not in written:
            return False
    return True


def _headless(rows: list[list[str]], typed: list[int]) -> list[list[str]]:
    """``rows`` under an empty header row when its first row is data: a table with no header.

    The first row was the header whatever it held, so a borderless table with no header row
    lost its first data row, and its columns were named after it ('c_1', 'c_2025_02_02',
    'east'): 29 of 30 rows, a 200. Under a running title it lost the same row once the title
    was dropped. A first row written like the rows under it (``_typed_like``) is data, as for
    a CSV. The empty row names the columns column_1, column_2, ..., and on later pages
    ``_continuation`` reads it as no header, never as a repeat of another table's.
    """
    if not _typed_like(rows[0], _forms(rows[1:], typed)):
        return rows
    return [[""] * max(len(r) for r in rows), *rows]


def _split_captions(
    rows: list[list[str]],
    running: Callable[[int], bool] = _never,
    continued: Callable[[int], bool] = _never,
) -> tuple[list[list[str]], list[str]]:
    """Drop rows above the header: titles, and merged title cells of bordered tables.

    Text alignment returns the whole page as one table, so a title line above a borderless
    table was rows[0] and became the column names ('quarterly_sales_by_regi', 'on', ...), with
    the real header as a data row that turned every numeric column into strings: a 200 for bad
    data. Above the first data row (``_data_row``: a number, date or missing-value token under
    a typed column and no text under any), the header is the row naming the most typed columns
    (``_names``), the topmost of equals; every row above it is a title and is dropped.

    Measured on the earlier rules. The row directly above the first data row: a section row
    ('North', '', '') under a header with a blank corner became the header. The widest row: a
    first body row ('Total', 'TBD', '9999.00') outranked that header, which was dropped as a
    title. Only rows narrower than the body: a long title, cut by the alignment pass into one
    piece per column, is as wide as the body and stayed the header. So width is not the test:
    a merged or short title names at most one typed column and a header names them all; a
    value marks a body row; and the topmost of equals keeps 'pending' rows (text where numbers
    belong) under their header. A title cut into text under every column ties with the header,
    which only positions tell apart: ``running(i)`` says whether row i is one line of running
    text (``_running_text``), which never names columns. It is asked only about rows that could
    become the header, as reading positions costs a pass over the page's glyphs. A title cut
    into text under every column above the data names them all, as a header does, so it is
    asked about only through ``continued(i)``: running text whose whole line, past the table's
    edge too, says the table continues ('... (continued)'), never a header.

    Typed columns are judged on the bottom half of the rows, which is body as long as there are
    no more leading rows than data rows. With no row naming a typed column nothing is dropped
    and rows[0] stays the header, as before. A header of two lines keeps its first line. With
    no header at all (the first row is data, or every row above it is running text), the rows
    come back under an empty header row (``_headless``).
    """
    if len(rows) < 3:
        return rows, []
    tail = rows[len(rows) // 2 :]
    typed = [c for c in range(len(rows[0])) if _typed_share(_values(tail, c)) >= 0.8]
    if not typed:
        return rows, []
    first = next((i for i, row in enumerate(rows) if _data_row(row, typed, tail)), None)
    if first is None:
        return rows, []
    if first == 0:
        return _headless(rows, typed), []
    if first == 1:
        # One row above the data: the header, or a running page title on a page that does
        # not repeat the header. Kept, that title became the page's header, so the page was
        # neither a repeat nor a continuation of page 1's table: a ledger titled on every
        # page came back as 80 of its 120 rows, its columns named after the title. A header
        # names every typed column, and only a row that does not is worth a glyph pass,
        # unless the caller has a reason to ask (``continued``).
        if (_names(rows[0], typed, tail) < len(typed) and running(0)) or continued(0):
            return _headless(rows[1:], typed), [_line(rows[0])]
        return rows, []
    names = [_names(rows[i], typed, tail) for i in range(first)]
    ranked = sorted((i for i in range(first) if names[i]), key=lambda i: (-names[i], i))
    header = next((i for i in ranked if not running(i)), None)
    if header is None and ranked:  # every row naming columns is running text: no header
        cut = max(ranked) + 1
        return _headless(rows[cut:], typed), [_line(r) for r in rows[:cut]]
    if not header:
        return rows, []
    return rows[header:], [_line(r) for r in rows[:header]]


def _running_text(table: Table, words: list[dict[str, Any]]) -> set[int]:
    """Rows of a text-alignment table that are one line of running text, cut at column edges.

    A title as wide as the table comes back as one piece per column ('Sales by region and
    product l', 'ine for the fiscal year, in US d', 'ollars'), text under every column like a
    header. Its words fall in two or more cells with no gap wider than a word space between
    them (half the text height; Helvetica's space is 0.28 em), where a header or body row has a
    column gap between its cells.
    """
    rows = [row.bbox for row in table.rows]
    tops = [bbox[1] for bbox in rows]
    lines: dict[int, list[dict[str, Any]]] = {}
    for word in words:
        mid = (word["top"] + word["bottom"]) / 2
        i = bisect_right(tops, mid) - 1
        if i >= 0 and mid <= rows[i][3] and word["x1"] > rows[i][0] and word["x0"] < rows[i][2]:
            lines.setdefault(i, []).append(word)
    running = set()
    for i, line in lines.items():
        line.sort(key=lambda w: w["x0"])
        cells = [c for c in table.rows[i].cells if c is not None]
        hit = {
            j for w in line for j, c in enumerate(cells) if c[0] <= (w["x0"] + w["x1"]) / 2 < c[2]
        }
        height = max(w["bottom"] - w["top"] for w in line)
        gaps = [b["x0"] - a["x1"] for a, b in pairwise(line)]
        if len(hit) >= 2 and max(gaps) <= height / 2:
            running.add(i)
    return running


def _ruled_tables(page: Page, number: int) -> list[_Found]:
    """Bordered pieces two or more columns wide, 1-row ones included: those are no table, but
    may be the last row of the previous page's table (``_merge`` decides)."""
    found = []
    for raw in page.extract_tables():
        rows, captions = _split_captions(_clean(raw))
        if rows and len(rows[0]) >= 2:
            found.append(_Found(number, rows, "ruling lines", captions))
    return found


class _Words:
    """A page's words, extracted on first use: only a titled borderless table needs them."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self._words: list[dict[str, Any]] | None = None

    def get(self) -> list[dict[str, Any]]:
        if self._words is None:
            self._words = self.page.extract_words()
        return self._words


def _whole_line(bbox: tuple[float, float, float, float], words: list[dict[str, Any]]) -> str:
    """The words at the height of a table row, past the table's left and right edges too."""
    line = [w for w in words if bbox[1] <= (w["top"] + w["bottom"]) / 2 <= bbox[3]]
    return " ".join(w["text"] for w in sorted(line, key=lambda w: w["x0"]))


def _aligned_table(
    table: Table, words: _Words, settings: TableSettings, after: list[str] | None = None
) -> tuple[list[list[str]], list[str]]:
    """``table``'s rows and the lines dropped above its header (``_split_captions``).

    ``after`` is the header of the borderless table that ended the previous page, when this is
    the first table on its page. A top row of the same width under another header may be a
    title cut into every column, and is then worth reading positions for (``continued``):
    'Table 4. Quarterly sales ledger, ... (continued)', printed on continuation pages only,
    was those pages' header and split the table (79 of 120 rows), and its '(continued)' was
    past the table's right edge, so the cut text never showed it. A repeated header, or a
    page that starts with body, never pays for the pass; 100 pages each holding a different
    table of the same width took 7.9 s instead of 6.8 s.
    """
    cells = _cells(table.extract(**(settings.text_settings or {})))
    kept = [i for i, row in enumerate(cells) if any(row)]
    running: set[int] | None = None

    def is_running(k: int) -> bool:
        nonlocal running
        if running is None:
            running = _running_text(table, words.get())
        return kept[k] in running

    def continued(k: int) -> bool:
        top = cells[kept[k]]
        if after is None or k or len(top) != len(after) or top == after or not is_running(k):
            return False
        line = _whole_line(table.rows[kept[k]].bbox, words.get())
        return _CONTINUED.search(_squashed(line).lower()) is not None

    return _split_captions([cells[i] for i in kept], is_running, continued)


def _aligned_tables(page: Page, number: int, after: list[str] | None = None) -> list[_Found]:
    # page.extract_tables(_TEXT_STRATEGY), keeping the Table objects for their row positions.
    settings = TableSettings.resolve(_TEXT_STRATEGY)
    words = _Words(page)
    found = []
    for i, table in enumerate(page.find_tables(settings)):  # topmost first
        rows, captions = _aligned_table(table, words, settings, None if i else after)
        if _looks_tabular(rows):
            edges = tuple(sorted({x for cell in table.cells for x in (cell[0], cell[2])}))
            found.append(_Found(number, rows, "text alignment", captions, edges))
    return found


def _spilled_rows(page: Page, number: int, edges: tuple[float, ...]) -> list[_Found]:
    """The top of a page with no borderless table, cut at the column ``edges`` of the one that
    ended the previous page: the rows of that table that spilled onto this page.

    Text alignment sees a column only where three words stack in it, so the last one or two
    rows of a table, alone on its last page (under a running title or not), were no table and
    were lost with a 200 and no warning: 125 of 126 rows. ``_merge`` keeps only the leading
    rows that are more body for that table (``_spilled_body``), so prose, a footer or a lone
    date below them stays out.
    """
    settings = TableSettings.resolve(
        {
            "vertical_strategy": "explicit",
            "explicit_vertical_lines": list(edges),
            "horizontal_strategy": "text",
        }
    )
    tables = page.find_tables(settings)  # topmost first
    if not tables:
        return []
    rows, captions = _aligned_table(tables[0], _Words(page), settings)
    return [_Found(number, rows, "text alignment", captions, spill=True)] if rows else []


@dataclass
class _Scan:
    has_text: bool = False
    ruled: list[_Found] = field(default_factory=list)
    aligned: list[_Found] = field(default_factory=list)
    text_pages: list[int] = field(default_factory=list)


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


def _flat_page_attrs(page_obj: PDFPage) -> PDFPage:
    """Give pdfplumber's Page the page's boxes and rotation as plain numbers.

    pdfplumber resolves /MediaBox, /CropBox, /TrimBox, /BleedBox, /ArtBox and /Rotate with its
    own recursive walk, which has no visited set: a /CropBox naming an array that names the
    next one twice, 40 levels deep, ran out of memory (and its error message held the walk).
    pdfminer has already read MediaBox, CropBox and Rotate one level deep, with defaults for
    bad values; the other boxes are kept only when they are four numbers.
    """
    attrs = page_obj.attrs
    attrs["MediaBox"] = list(page_obj.mediabox)
    attrs["Rotate"] = page_obj.rotate
    if "CropBox" in attrs:
        attrs["CropBox"] = list(page_obj.cropbox)
    for name in ("TrimBox", "BleedBox", "ArtBox"):
        if name in attrs:
            box = resolve1(attrs.pop(name))
            values = [resolve1(v) for v in box] if isinstance(box, list) and len(box) == 4 else []
            if len(values) == 4 and all(isinstance(v, int | float) for v in values):
                attrs[name] = values
    return page_obj


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
    searching = _SEARCHING.set(budget)
    try:
        _scan_pages(doc, pages, budget, scan)
    finally:
        _SEARCHING.reset(searching)
    return scan


def _scan_pages(doc: PDF, pages: list[int], budget: _Budget, scan: _Scan) -> None:
    wanted = set(pages)
    bordered_table = False
    tail: _Found | None = None  # the last borderless table found, for ``_spilled_rows``
    for number, page_obj in enumerate(PDFPage.create_pages(doc.doc), 1):
        if number > pages[-1]:
            break
        if number not in wanted:
            continue
        page = Page(doc, _flat_page_attrs(page_obj), page_number=number)
        try:
            _parse_layout(page, budget)
            if not page.chars:
                continue
            scan.has_text = True
            scan.text_pages.append(number)
            bordered = _ruled_tables(page, number)
            scan.ruled.extend(bordered)
            # Pointless once a bordered table exists: it wins.
            bordered_table = bordered_table or any(_usable(f.rows) for f in bordered)
            if not bordered_table:
                after = tail if tail is not None and tail.page == number - 1 else None
                aligned = _aligned_tables(page, number, after.rows[0] if after else None)
                if not aligned and after is not None:
                    aligned = _spilled_rows(page, number, after.edges)
                scan.aligned.extend(aligned)
                tail = next((f for f in reversed(aligned) if not f.spill), None)
        finally:
            page.close()  # release its glyph objects before the next page is parsed


def _continues(table: _Table, rows: list[list[str]]) -> bool:
    """Is ``rows``, a table at the top of the next page, more body for ``table``?

    Without this, a table whose header is not repeated on page 2 split in two: page 2's first
    data row became the second table's header ('area7', 'c_107', ...) and appeared in neither.
    Continuation needs the same width and a first row that is a data row under the columns
    typed in ``table``'s body (text there, or years, is what a new table's header looks like).
    """
    if len(rows[0]) != len(table.header):
        return False
    typed = table.typed()
    return bool(typed) and _data_row(rows[0], typed, table.rows)


def _shape(row: list[str]) -> frozenset[int]:
    return frozenset(c for c, v in enumerate(row) if v)


def _running_title(row: list[str], captions: list[str]) -> bool:
    """Is ``row`` a running title above a continuation: a line saying the table continues
    ('(continued)', ``_CONTINUED``), or a line already dropped above this table with other
    numbers in it ('Ledger p1', then 'Ledger p2')? Compared without spaces, as the alignment
    pass cuts a line at each page's column edges.

    Found reviewing 6b5a217: in the column of the section rows such a title is shaped like one
    ('North'), so 'Ledger p2', 'Ledger p3', ... became data rows, with no warning. Printed on
    continuation pages only and across every typed column, '(continued)' names the columns as
    a header does, and the table split: 79 of 120 rows. Neither is ever a header or body.
    """
    text = _squashed(_line(row))
    if _CONTINUED.search(text.lower()):
        return True
    form = _form(text)
    return any(_form(_squashed(c)) == form for c in captions)


def _section_row(table: _Table, row: list[str]) -> bool:
    """Is ``row`` shaped like a section row of ``table``'s body ('North', '', '')? Text only,
    filling the cells some text-only body row fills, and at most half of them.

    The half is what tells a section row from a new table's header. Found reviewing 6b5a217:
    after a suppressed row in the body ('Area5', 'x', 'x': text in every cell), a different
    table's header on the next page ('City', 'Stores', 'Sales') had that row's shape and was
    taken as body, the whole table with it: 51 rows for 30.
    """
    shape = _shape(row)
    return (
        shape in table.text_shapes
        and 2 * len(shape) <= len(table.header)
        and not any(_kind(v) in _TYPED for v in row)
    )


def _pending(table: _Table, row: list[str], typed: list[int]) -> bool:
    """Is ``row`` a body row with text where a value belongs ('Area9', '109', 'pending')?

    A value under a typed column makes a row body (``_names``), but a new table's header holds
    values too. Found reviewing 6b5a217: ('Region', '2023', '2024', 'Change %') on the page
    after a table of the same width was taken as body, and the second table merged into the
    first with its header as a data row: 51 rows for 30, 'units' int64 to double, a 200. So
    each value must be written as the body's are (``_form``: '1' over integers is, '2024' over
    amounts like '12.34' is not) and not be a year, the text beside it must be text that column
    of the body already holds ('pending' seen before, never 'Mean' or 'Change %'), and the row
    must fill cells as a body row does. A first 'pending' at the top of a page splits.
    """
    if _shape(row) not in table.shapes:
        return False
    valued = False
    for c in typed:
        value = _cell(row, c)
        kind = _kind(value)
        if kind in _TYPED:
            if _YEAR.fullmatch(value) or _form(value) not in table.written[c]:
                return False
            valued = True
        elif kind not in {"empty", "missing"} and value not in table.texts[c]:
            return False
    return valued


def _continuation(table: _Table, rows: list[list[str]]) -> tuple[list[list[str]], str] | None:
    """The part of ``rows``, the first table on the page after ``table``'s last, that is more
    body for ``table``, and the line dropped above it ('' for none). None: a new table.

    The page starts with body (``_continues``), with no header row (``_headless``), or with one
    line above body. That line was the page's header, so the page neither repeated page 1's
    header nor continued its body: a ledger split into one table per page and the largest was
    returned with a 200, 41 of 120 rows under 'Ledger p2', 79 under 'Table 1 (continued)'.

    A header of years starts a new table. A running title (``_running_title``) is dropped as a
    caption. The line is body when it is a 'pending' row (``_pending``) or a section row
    (``_section_row``, 'South'). It starts a new table when it names every typed column or
    fills more than half the cells: a new table's header with a blank corner ('', 'Shipped',
    'Carrier', ...) over a body typed like this one's was dropped as a title and the two tables
    merged. Anything else (a title, in one or two cells) is dropped as a caption. Each body
    rule is narrow on purpose: a page split off is a reported partial result ('found 2
    tables'), a different table merged in is silent wrong data.
    """
    if not any(rows[0]):
        return (rows[1:], "") if len(rows) > 1 and _continues(table, rows[1:]) else None
    if _continues(table, rows):
        return rows, ""
    if len(rows) < 2 or not _continues(table, rows[1:]):
        return None
    line = rows[0]
    typed = table.typed()
    if _year_names(line, typed, table.rows):
        return None
    if _running_title(line, table.captions):
        return rows[1:], _line(line)
    if _pending(table, line, typed) or _section_row(table, line):
        return rows, ""
    if _names(line, typed, table.rows) >= len(typed) or 2 * len(_shape(line)) > len(line):
        return None
    return rows[1:], _line(line)


def _spilled_body(table: _Table, rows: list[list[str]]) -> list[list[str]]:
    """The leading ``rows`` of a page read at ``table``'s column edges (``_spilled_rows``) that
    are rows of its body: data rows, written like the body (``_typed_like``) and filling cells
    a body row fills, and section rows shaped like the body's ('South') above data rows.
    Nothing tells them from the page's other text but how they look, so a page number or a
    date alone under a number column ('4', '2025-01-31') is not taken.
    """
    typed = table.typed()
    forms = table.forms(typed)

    def data(row: list[str]) -> bool:
        return (
            _data_row(row, typed, table.rows)
            and _typed_like(row, forms)
            and _shape(row) in table.shapes
        )

    def body(row: list[str]) -> bool:
        return data(row) or _section_row(table, row)

    end = next((i for i, row in enumerate(rows) if not body(row)), len(rows))
    while end and not data(rows[end - 1]):  # a section row heads data rows, or is not body
        end -= 1
    return rows[:end]


def _squashed(text: str) -> str:
    return "".join(text.split())


def _repeats_caption(item: _Found, table: _Table) -> bool:
    """Is ``item``'s first row a line dropped above ``table``'s header (a running title)?

    Compared without spaces: the alignment pass cuts the line at each page's column edges.
    """
    if len(item.rows) < 2 or not table.captions:
        return False
    line = _squashed("".join(item.rows[0]))
    return bool(line) and line in {_squashed(c) for c in table.captions}


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
        if last is not None and item.page > last.pages[-1] and _repeats_caption(item, last):
            # The title already dropped above this table's header, repeated at the top of a
            # later page. In one column, positions cannot tell it from a header row.
            item.captions.append(_line(item.rows[0]))
            item.rows = item.rows[1:]
        header = item.rows[0]
        # An empty header row is no header (``_headless``), never a repeat of another table's.
        same = next((t for t in tables if t.header == header), None) if any(header) else None
        taken = None
        if same is None and last is not None and last_page == item.page - 1:
            # The first table on this page, right after the last one on the previous page.
            taken = _continuation(last, item.rows)
        if same is not None:  # a repeated header: the table continues on this page
            target, body = same, [r for r in item.rows[1:] if r != header]
        elif last is not None and taken is not None:
            target, (body, line) = last, taken
            item.captions.extend([line] if line else [])
        elif item.spill or not _usable(item.rows):
            # A 1-row box that continues nothing (a note, a signature block) is not a table,
            # nor the first table on its page. Dropping such pieces before merging lost the
            # last row of a table whenever it alone spilled onto the next page.
            continue
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
            continue
        if item.spill and not (body := _spilled_body(target, body)):
            continue
        target.add(body)
        target.pages.append(item.page)
        target.captions.extend(item.captions)
        last = target
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
    # PDFPasswordIncorrect and friends have an empty str(): name the class instead. Some quote
    # the object they could not read, which a hostile file can make as large as it likes.
    reason = str(inner) or type(inner).__name__
    return reason if len(reason) <= _MAX_REASON else reason[: _MAX_REASON - 3] + "..."


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


def _settle_header(table: _Table, header: bool | None) -> list[str]:
    """Apply ``options.header`` to the chosen table, and say when the header is a guess.

    Whether the first row holds names is judged from how it is written (``_headless``), as
    for a CSV: a header of plain numbers ('Region', '1', '2', '3') reads as data, and a first
    data row written unlike the rest ('1,204.50' above no other thousands) as the header.
    Either way the warning names the option that settles it.
    """
    width = len(table.header)
    if not any(table.header):
        if header and table.rows:  # the caller says the first row holds the names
            table.header = table.rows.pop(0)
        elif header is None:
            return [
                f"no header row: the first row is data, so the columns are named column_1 to "
                f"column_{width}; pass header=true if it holds the column names"
            ]
        return []
    if header is False:  # the caller says the first row is data
        table.rows.insert(0, table.header)
        table.header = [""] * width
    elif header is None and (typed := table.typed()) and _data_row(table.header, typed, table.rows):
        shown = ", ".join(repr(c[:20]) for c in table.header[:6])
        return [
            f"the header row ({shown}) holds numbers or dates as the data does; pass "
            "header=false if it is the first data row"
        ]
    return []


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
            # pdfminer walks the page-label number tree, with no visited set, before it yields
            # the first page: a tree naming each child twice took 2**40 steps. Never read here.
            doc.doc.catalog.pop("PageLabels", None)
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
    tables = _merge(scan.ruled)
    if tables:
        # Text alignment also reads a table of contents or figure-heavy prose as a "table",
        # often a larger one, so a bordered table wins; say where borderless ones may be.
        covered = {page for table in tables for page in table.pages}
        if unruled := [page for page in scan.text_pages if page not in covered]:
            span = _span(unruled)
            warnings.append(
                f"page(s) {span} have no ruled table; borderless tables are read only when no "
                f"page has one, so pass pdf_pages='{span}' to read those pages"
            )
    else:
        tables = _merge(scan.aligned)
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
    warnings.extend(_settle_header(chosen, options.header))
    if len(tables) > 1:
        warnings.append(
            f"found {len(tables)} tables; used {chosen.describe(index)} (pass pdf_table to choose)"
        )
    if len(chosen.pages) > 1:
        warnings.append(f"merged a table that continues across {len(chosen.pages)} pages")
    if chosen.captions:
        # A running title is dropped once a page: show each line once.
        shown = "; ".join(repr(c[:80]) for c in list(dict.fromkeys(chosen.captions))[:3])
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
