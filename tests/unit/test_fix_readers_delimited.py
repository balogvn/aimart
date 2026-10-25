"""Blank lines before the header: the sniffer skipped them, DuckDB did not.

The column names came from the first non-blank row, but ``read_csv(header=true)`` took the first
physical line (the blank one) as the header, so the real header became a data row: one extra
row, and a 25-row integer column failed the 98% rung (25/26) and stayed text, with no warning.
"""

from __future__ import annotations

import pytest

import aimart_testkit as kit
from aimart_parquet.engine.readers.delimited import sniff_dialect

BODY = "".join(f"p{i},{20 + i}\n" for i in range(25))


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"\nname,age\n" + BODY.encode(), id="one-blank-line"),
        pytest.param(b"\n\n\nname,age\n" + BODY.encode(), id="several-blank-lines"),
        pytest.param(b"  \t \nname,age\n" + BODY.encode(), id="whitespace-only-line"),
        pytest.param(b" , ,\n\nname,age\n" + BODY.encode(), id="blank-cells-line"),
        pytest.param(
            b"\r\n\r\nname,age\r\n" + BODY.replace("\n", "\r\n").encode(), id="crlf-blank-lines"
        ),
        pytest.param(b"\xef\xbb\xbf\n\nname,age\n" + BODY.encode(), id="bom-then-blank-lines"),
        pytest.param(("\nname,age\n" + BODY).encode("utf-16"), id="utf16-blank-line"),
        pytest.param(("\n\nname,age\n" + BODY).encode("latin-1"), id="transcoded-blank-lines"),
        pytest.param(b'"\n"\nname,age\n' + BODY.encode(), id="quoted-newline-only-record"),
    ],
)
def test_leading_blank_lines_are_not_read_as_the_header(tmp_path, data: bytes) -> None:
    result = kit.convert_bytes(tmp_path, "people.csv", data)
    assert [c.name for c in result.report.columns] == ["name", "age"]
    assert result.report.rows == 25
    assert result.rows["name"][:2] == ["p0", "p1"]
    assert result.column("age").type == "int64"
    assert result.rows["age"] == list(range(20, 45))


def test_forced_header_also_skips_them(tmp_path) -> None:
    result = kit.convert_bytes(tmp_path, "p.csv", "\n\nname,age\n" + BODY, header=True)
    assert result.report.rows == 25
    assert result.column("age").type == "int64"


def test_headerless_file_gets_no_junk_row_from_a_whitespace_line(tmp_path) -> None:
    result = kit.convert_bytes(tmp_path, "n.csv", "   \n1,2.5\n2,3.5\n")
    assert result.rows == {"column_1": [1, 2], "column_2": [2.5, 3.5]}


@pytest.mark.parametrize(
    ("sample", "skip"),
    [
        ("a,b\n1,2\n", 0),
        ("\na,b\n1,2\n", 1),
        ("\n \n , \na,b\n1,2\n", 3),
        ("a,b\n\n1,2\n", 0),  # blank lines after the header are DuckDB's to skip, not ours
    ],
)
def test_sniffer_counts_the_blank_rows_before_the_first_real_one(sample: str, skip: int) -> None:
    dialect, rows = sniff_dialect(sample, None)
    assert dialect.skip == skip
    assert rows[0] == ["a", "b"]
