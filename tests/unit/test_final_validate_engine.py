"""Request rules that belong to the request models, and engine failures that must be typed.

Each rule here used to be found only after the payment was verified, and several ended in an
untyped exception (a bare 500): ``csv.reader`` refusing a '"' delimiter, ``codecs`` decoding
with 'rot13' or 'base64', a forced 'utf-16' on a file without a byte-order mark, and the
report looking up a column by a name DuckDB had renamed (KeyError: 'A').
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

import aimart_testkit as kit
from aimart_parquet.engine.models import ConvertOptions, ExpectedColumn
from aimart_parquet.engine.text import encoding_problem, to_utf8
from aimart_parquet.errors import InvalidInput, InvalidRequest

if TYPE_CHECKING:
    from pathlib import Path

CSV = b"a,b\n1,x\n2,y\n"


def _problem(**options: Any) -> str:
    with pytest.raises(ValidationError) as info:
        ConvertOptions(**options)
    [error] = info.value.errors()
    return str(error["msg"])


# ------------------------------------------------------------------------------ expected_schema
@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ([("a", "int64"), ("a", "string")], "expected_schema names column 'a' twice."),
        (
            [("a", "int64"), ("b", "bool"), ("A", "string")],
            "expected_schema names column 'a' twice (as 'a' and 'A'; names are case-insensitive).",
        ),
        ([("z", "int64"), ("z", "string")], "expected_schema names column 'z' twice."),
    ],
)
def test_duplicate_expected_names_are_refused(schema: list[tuple[str, str]], message: str) -> None:
    columns = [{"name": n, "type": t} for n, t in schema]
    assert _problem(expected_schema=columns) == f"Value error, {message}"


@pytest.mark.parametrize("name", ["", "a\x00", "\x00"])
def test_expected_names_that_cannot_be_sql_identifiers_are_refused(name: str) -> None:
    # Before: "" and a NUL reached DuckDB as a zero-length or cut-off identifier, a bare 500.
    with pytest.raises(ValidationError):
        ExpectedColumn(name=name, type="int64")


def test_report_is_built_by_position_not_by_name(tmp_path: Path) -> None:
    # A name collision the validator does not see (a caller building options without
    # validation) must neither crash (KeyError: 'A') nor report column one's stats twice.
    options = ConvertOptions.model_construct(
        expected_schema=[
            ExpectedColumn(name="a", type="int64"),
            ExpectedColumn(name="A", type="string"),
        ]
    )
    from aimart_parquet.engine.convert import convert_file
    from aimart_parquet.engine.limits import Limits

    src = kit.write(tmp_path / "in" / "x.csv", CSV)
    out = src.parent / "out.parquet"
    report = convert_file(
        src, out, filename="x.csv", options=options, workdir=src.parent, limits=Limits()
    )
    schema = pq.read_schema(out)
    assert [c.name for c in report.columns] == schema.names
    assert [c.type for c in report.columns] == ["int64", "string", "string"]
    assert [str(t) for t in schema.types] == ["int64", "string", "string"]


# ------------------------------------------------------------------------------ delimiter
@pytest.mark.parametrize(
    ("delimiter", "why"),
    [('"', "the quote character"), ("\n", "a line break"), ("\r", "a line break"), ("\x00", "NUL")],
)
def test_delimiters_that_cannot_work_are_refused(delimiter: str, why: str) -> None:
    assert _problem(delimiter=delimiter) == (
        f"Value error, delimiter {delimiter!r} cannot separate fields: it is {why}. "
        "Omit delimiter to detect it."
    )


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|", " ", "'", "\\", "#"])
def test_ordinary_delimiters_still_convert(delimiter: str, tmp_path: Path) -> None:
    data = CSV.replace(b",", delimiter.encode())
    converted = kit.convert_bytes(tmp_path, "x.csv", data, delimiter=delimiter)
    assert [c.name for c in converted.report.columns] == ["a", "b"]


# ------------------------------------------------------------------------------ encoding
@pytest.mark.parametrize(
    ("encoding", "message"),
    [
        ("no-such-codec", "Unknown encoding 'no-such-codec'."),
        ("rot13", "'rot13' is not a text encoding."),
        ("base64", "'base64' is not a text encoding."),
        ("zlib", "'zlib' is not a text encoding."),
        ("idna", "'idna' cannot decode a file."),
        ("undefined", "'undefined' cannot decode a file."),
    ],
)
def test_encodings_that_cannot_decode_text_are_refused(encoding: str, message: str) -> None:
    assert encoding_problem(encoding) == message
    assert _problem(encoding=encoding) == f"Value error, {message}"


@pytest.mark.parametrize(
    "encoding", ["utf-8", "latin-1", "cp1252", "UTF-16LE", "shift_jis", "LATIN1"]
)
def test_text_encodings_are_accepted(encoding: str) -> None:
    assert encoding_problem(encoding) is None
    assert ConvertOptions(encoding=encoding).encoding == encoding


@pytest.mark.parametrize("encoding", ["rot13", "base64", "idna"])
def test_to_utf8_refuses_them_too(encoding: str, tmp_path: Path) -> None:
    # The engine repeats the check for callers that build options without validation.
    src = kit.write(tmp_path / "x.csv", CSV)
    with pytest.raises(InvalidRequest, match=encoding):
        to_utf8(src, tmp_path, encoding)


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_a_forced_encoding_the_bytes_do_not_fit_is_invalid_input(
    encoding: str, tmp_path: Path
) -> None:
    # Before: UnicodeDecodeError ('Stream does not start with BOM'), a bare 500 after payment.
    data = "a,b\n1,2\n".encode(encoding.replace("-", "_") + "_le")
    with pytest.raises(InvalidInput) as info:
        kit.convert_bytes(tmp_path, "x.csv", data, encoding=encoding)
    assert (
        info.value.message
        == f"The file cannot be decoded as {encoding}: Stream does not start with BOM."
    )
    assert f"'{encoding}-le'" in (info.value.hint or "")


def test_a_bom_file_still_decodes_with_the_generic_name(tmp_path: Path) -> None:
    converted = kit.convert_bytes(
        tmp_path, "x.csv", "a,b\n1,2\n".encode("utf-16"), encoding="utf-16"
    )
    assert converted.rows == {"a": [1], "b": [2]}


# ------------------------------------------------------------------------------ pdf_pages, NUL
@pytest.mark.parametrize(
    ("pages", "message"),
    [
        ("0", "pdf_pages range '0' is invalid: pages start at 1, and a range runs low to high."),
        (
            "5-2",
            "pdf_pages range '5-2' is invalid: pages start at 1, and a range runs low to high.",
        ),
        (
            "1, 0-3",
            "pdf_pages range '0-3' is invalid: pages start at 1, and a range runs low to high.",
        ),
        ("1,,2", "pdf_pages '1,,2' is not a page list. Use a form like '1-3,7'."),
        ("1-", "pdf_pages '1-' is not a page list. Use a form like '1-3,7'."),
        ("-", "pdf_pages '-' is not a page list. Use a form like '1-3,7'."),
    ],
)
def test_impossible_pdf_pages_are_refused(pages: str, message: str) -> None:
    assert _problem(pdf_pages=pages) == f"Value error, {message}"


@pytest.mark.parametrize("pages", ["1", "1-3,7", " 2 - 4 , 9 ", "3-3", "1-99999999999"])
def test_valid_pdf_pages_pass(pages: str) -> None:
    assert ConvertOptions(pdf_pages=pages).pdf_pages == pages


def test_null_values_with_a_nul_are_refused() -> None:
    # Before: the NUL cut DuckDB's SQL short inside a string literal: a bare 500.
    assert "NUL" in _problem(null_values=["NA", "\x00"])
