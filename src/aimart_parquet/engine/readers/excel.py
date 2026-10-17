"""Excel (xlsx/xlsm/xlsb/xls) and OpenDocument spreadsheets via calamine (Rust, no XML DTDs).

Real workbooks put a title in A1, leave a blank row, then start the table; merge a header cell
across two columns; and mix numbers with "n/a" in one column. We skip leading rows that are too
sparse to be a header, carry merged header text across the merged span, keep a column's native
cell type when every cell agrees, and send everything else through the text type ladder so the
rejects get counted rather than guessed.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import TYPE_CHECKING, Any

import pyarrow as pa
from python_calamine import CalamineError, CalamineWorkbook

from aimart_parquet.engine.duck import ident
from aimart_parquet.engine.models import InputFormat
from aimart_parquet.engine.readers import RAW_TABLE, RawColumn, RawTable
from aimart_parquet.engine.safety import check_zip
from aimart_parquet.errors import InvalidInput, InvalidRequest

if TYPE_CHECKING:
    from pathlib import Path

    import duckdb

    from aimart_parquet.engine.limits import Limits
    from aimart_parquet.engine.models import ConvertOptions

_HEADER_SEARCH_ROWS = 30


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _pick_sheet(wb: CalamineWorkbook, sheet: str | int | None) -> tuple[Any, str]:
    names: list[str] = list(wb.sheet_names)
    if not names:
        raise InvalidInput("The workbook has no sheets.", hint="Send a workbook with data.")
    if sheet is not None:
        if isinstance(sheet, int):
            if not 0 <= sheet < len(names):
                raise InvalidRequest(
                    f"Sheet index {sheet} is out of range.",
                    hint=f"Use 0..{len(names) - 1} or a name from available_sheets.",
                    available_sheets=names,
                )
            return wb.get_sheet_by_index(sheet), names[sheet]
        if sheet not in names:
            raise InvalidRequest(
                f"Sheet {sheet!r} does not exist.",
                hint="Use one of available_sheets.",
                available_sheets=names,
            )
        return wb.get_sheet_by_name(sheet), sheet
    for i, name in enumerate(names):
        candidate = wb.get_sheet_by_index(i)
        if candidate.height and candidate.width:
            return candidate, name
    raise InvalidInput("Every sheet in the workbook is empty.", hint="Send a workbook with data.")


def _header_row(rows: list[list[Any]], width: int) -> int:
    need = max(2, math.ceil(width * 0.5)) if width > 2 else 1
    for i, row in enumerate(rows[:_HEADER_SEARCH_ROWS]):
        if sum(not _blank(v) for v in row) >= need:
            return i
    return next((i for i, row in enumerate(rows) if any(not _blank(v) for v in row)), 0)


def _looks_like_header(row: list[Any]) -> bool:
    """A header row is all text; a number or date in it means the table has no header."""
    return all(_blank(v) or isinstance(v, str) for v in row)


def _stringify(v: Any) -> str | None:
    if _blank(v):
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer() and abs(v) < 2**53:
        return str(int(v))
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    return str(v).strip()


def _column_array(values: list[Any]) -> tuple[pa.Array, bool]:
    """Arrow array for one column plus whether its text should climb the type ladder."""
    present = [v for v in values if not _blank(v)]
    kinds = {type(v) for v in present}
    clean = [None if _blank(v) else v for v in values]
    if not present:
        return pa.array([None] * len(values), type=pa.string()), False
    if kinds == {bool}:
        return pa.array(clean, type=pa.bool_()), False
    if kinds <= {int, float}:
        if all(float(v).is_integer() and abs(v) < 2**53 for v in present):
            return pa.array([None if v is None else int(v) for v in clean], type=pa.int64()), False
        return pa.array([None if v is None else float(v) for v in clean], type=pa.float64()), False
    if kinds == {dt.date}:
        return pa.array(clean, type=pa.date32()), False
    if kinds <= {dt.datetime, dt.date}:
        promoted = [
            v if v is None or isinstance(v, dt.datetime) else dt.datetime(v.year, v.month, v.day)
            for v in clean
        ]
        return pa.array(promoted, type=pa.timestamp("us")), False
    if kinds == {dt.time}:
        return pa.array(clean, type=pa.time64("us")), False
    return pa.array([_stringify(v) for v in values], type=pa.string()), True


def _fill_merged_headers(
    header: list[Any], merged: list[Any], header_index: int, offset: tuple[int, int]
) -> None:
    """Copy a merged header cell's text across every column it spans."""
    row0, col0 = offset
    for (r1, c1), (r2, c2) in merged or []:
        if r1 - row0 <= header_index <= r2 - row0 and c2 > c1:
            text = header[c1 - col0] if 0 <= c1 - col0 < len(header) else None
            for c in range(c1 + 1 - col0, min(c2 - col0, len(header) - 1) + 1):
                if _blank(header[c]):
                    header[c] = text


def read_excel(
    con: duckdb.DuckDBPyConnection,
    path: Path,
    workdir: Path,
    options: ConvertOptions,
    fmt: InputFormat,
    limits: Limits,
) -> RawTable:
    if fmt in {InputFormat.XLSX, InputFormat.ODS}:
        check_zip(path, limits.max_decompressed_bytes)  # xlsx/ods are zips of XML: bomb check
    wb: CalamineWorkbook | None = None
    try:
        wb = CalamineWorkbook.from_path(str(path))
        sheet, sheet_name = _pick_sheet(wb, options.sheet)
        rows: list[list[Any]] = sheet.to_python(skip_empty_area=True)
        merged = sheet.merged_cell_ranges
        offset = sheet.start or (0, 0)
        names = list(wb.sheet_names)
    except CalamineError as exc:
        raise InvalidInput(
            f"Could not read the spreadsheet: {exc}",
            hint="Check it opens in a spreadsheet app; password-protected files are not supported.",
        ) from exc
    finally:
        if wb is not None:
            wb.close()
    if not rows:
        raise InvalidInput(f"Sheet {sheet_name!r} is empty.", hint="Pick another sheet.")
    width = max(
        (max((i + 1 for i, v in enumerate(r) if not _blank(v)), default=0) for r in rows), default=0
    )
    if width == 0:
        raise InvalidInput(f"Sheet {sheet_name!r} is empty.", hint="Pick another sheet.")
    rows = [list(r[:width]) + [None] * (width - len(r)) for r in rows]
    start = _header_row(rows, width)
    warnings: list[str] = []
    if start:
        warnings.append(f"skipped {start} title or blank rows above the table")
    first, body = rows[start], rows[start + 1 :]
    has_header = options.header if options.header is not None else _looks_like_header(first)
    if has_header:
        header = list(first)
        _fill_merged_headers(header, merged, start, (int(offset[0]), int(offset[1])))
    else:
        header, body = [None] * width, rows[start:]
    before = len(body)
    body = [r for r in body if any(not _blank(v) for v in r)]
    if before - len(body):
        warnings.append(f"dropped {before - len(body)} empty rows")
    arrays: dict[str, pa.Array] = {}
    columns: list[RawColumn] = []
    for c in range(width):
        array, infer = _column_array([r[c] for r in body])
        source = f"c{c}"
        arrays[source] = array
        original = _stringify(header[c]) or f"column_{c + 1}"
        columns.append(
            RawColumn(
                source=source, original=original, infer=infer, text=pa.types.is_string(array.type)
            )
        )
    con.register("raw_arrow", pa.table(arrays))
    con.execute(f"CREATE TABLE {ident(RAW_TABLE)} AS SELECT * FROM raw_arrow")
    con.unregister("raw_arrow")
    return RawTable(
        table=RAW_TABLE,
        columns=columns,
        source_format=fmt,
        sheet=sheet_name,
        available_sheets=names,
        warnings=warnings,
    )
