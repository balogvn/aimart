"""Format detection: magic bytes beat extensions, and mislabeled text is re-sniffed."""

from __future__ import annotations

import pytest

import aimart_testkit as kit
from aimart_parquet.engine.detect import (
    _SAS7BDAT_MAGIC,
    SUPPORTED_EXTENSIONS,
    detect_format,
    format_from_magic,
    format_from_name,
    format_from_text,
)
from aimart_parquet.engine.models import InputFormat as F
from aimart_parquet.engine.safety import read_magic
from aimart_parquet.errors import UnsupportedFormat


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("data.csv", F.CSV),
        ("DATA.CSV", F.CSV),
        ("notes.txt", F.CSV),
        ("x.tsv", F.TSV),
        ("x.tab", F.TSV),
        ("x.json", F.JSON),
        ("x.geojson", F.JSON),
        ("x.ndjson", F.NDJSON),
        ("x.jsonl", F.NDJSON),
        ("x.xlsx", F.XLSX),
        ("x.xlsm", F.XLSX),
        ("x.xlsb", F.XLSX),
        ("x.xls", F.XLS),
        ("x.ods", F.ODS),
        ("x.sav", F.SAV),
        ("x.zsav", F.ZSAV),
        ("x.por", F.POR),
        ("x.dta", F.DTA),
        ("x.sas7bdat", F.SAS7BDAT),
        ("x.xpt", F.XPT),
        ("x.parquet", F.PARQUET),
        ("x.pq", F.PARQUET),
        ("x.csv.gz", F.CSV),
        ("x.json.GZIP", F.JSON),
        ("x.sav.bz2", F.SAV),
        ("x.dta.zip", F.DTA),
        ("x.pdf", F.PDF),
        ("x.docx", None),
        ("noextension", None),
        ("", None),
        (None, None),
    ],
)
def test_format_from_name(name: str | None, expected: F | None) -> None:
    assert format_from_name(name) is expected


def test_supported_extensions_is_sorted_and_complete() -> None:
    assert list(SUPPORTED_EXTENSIONS) == sorted(SUPPORTED_EXTENSIONS)
    assert {".csv", ".sav", ".dta", ".xpt", ".sas7bdat", ".parquet"} <= set(SUPPORTED_EXTENSIONS)


@pytest.mark.parametrize(
    ("magic", "expected"),
    [
        (b"PAR1\x15\x04", F.PARQUET),
        (b"$FL3@(#) SPSS DATA FILE", F.ZSAV),
        (b"$FL2@(#) SPSS DATA FILE", F.SAV),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest", F.XLS),
        (_SAS7BDAT_MAGIC + b"\x00" * 32, F.SAS7BDAT),
        (b"HEADER RECORD*******LIBRARY HEADER RECORD", F.XPT),
        (b"HEADER RECORD*******LIBV8   HEADER RECORD", F.XPT),
        (b"<stata_dta><header><release>119", F.DTA),
        (b"0" * 200 + b"SPSSPORT" + b"0" * 10, F.POR),
        (b"a,b,c\n1,2,3\n", None),
    ],
)
def test_format_from_magic(tmp_path, magic: bytes, expected: F | None) -> None:
    path = kit.write(tmp_path / "blob", magic)
    assert format_from_magic(path, magic) is expected


def test_zip_magic_distinguishes_office_from_plain_zip(tmp_path) -> None:
    xlsx = kit.messy_workbook(tmp_path / "book.xlsx")
    assert format_from_magic(xlsx, read_magic(xlsx)) is F.XLSX
    ods = kit.write(
        tmp_path / "sheet.ods",
        kit.zip_bytes(
            {"mimetype": b"application/vnd.oasis.opendocument.spreadsheet", "content.xml": b"<x/>"}
        ),
    )
    assert format_from_magic(ods, read_magic(ods)) is F.ODS
    odt = kit.write(
        tmp_path / "doc.odt",
        kit.zip_bytes({"mimetype": b"application/vnd.oasis.opendocument.text"}),
    )
    assert format_from_magic(odt, read_magic(odt)) is None
    plain = kit.write(tmp_path / "data.zip", kit.zip_bytes({"data.csv": b"a\n1\n"}))
    assert format_from_magic(plain, read_magic(plain)) is None
    broken = kit.write(tmp_path / "broken.zip", b"PK\x03\x04 not really a zip")
    assert format_from_magic(broken, read_magic(broken)) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (b'[{"a": 1}]', F.JSON),
        (b'{"a": 1}', F.JSON),
        (b'{"a": 1}\n{"a": 2}\n', F.NDJSON),
        (b'\xef\xbb\xbf  \n{"a": 1}\n{"a": 2}', F.NDJSON),
        (b'{"a": {\n"b": 1}}\n', F.JSON),
        (b"a\tb\tc\n1\t2\t3\n", F.TSV),
        (b"a,b,c\n1,2,3\n", F.CSV),
        (b"a;b;c\n1;2;3\n", F.CSV),
        (b"just text", F.CSV),
    ],
)
def test_format_from_text(text: bytes, expected: F) -> None:
    assert format_from_text(text) is expected


class TestDetectFormat:
    def test_forced_format_always_wins(self, tmp_path) -> None:
        path = kit.write(tmp_path / "x.csv", b"PAR1")
        assert detect_format(path, b"PAR1", "x.csv", F.JSON) is F.JSON

    def test_magic_beats_a_wrong_extension(self, tmp_path) -> None:
        path = kit.write(tmp_path / "survey.csv", b"$FL2@(#) SPSS")
        assert detect_format(path, b"$FL2@(#) SPSS", "survey.csv", None) is F.SAV

    def test_xlsx_saved_as_xls_is_xlsx(self, tmp_path) -> None:
        book = kit.messy_workbook(tmp_path / "book.xls")
        assert detect_format(book, read_magic(book), "book.xls", None) is F.XLSX

    @pytest.mark.parametrize("name", ["old.dta", "old.por", "old.xpt", "old.sas7bdat"])
    def test_signatureless_binary_formats_trust_the_name(self, tmp_path, name: str) -> None:
        magic = b"\x71\x02\x01\x00" + b"\x00" * 60
        path = kit.write(tmp_path / name, magic)
        assert detect_format(path, magic, name, None) is format_from_name(name)

    def test_unknown_binary_without_a_name_is_unsupported(self, tmp_path) -> None:
        magic = b"\x89PNG\r\n\x1a\n\x00\x00"
        path = kit.write(tmp_path / "blob", magic)
        with pytest.raises(UnsupportedFormat) as info:
            detect_format(path, magic, "image.png", None)
        assert info.value.hint is not None
        assert "options.format" in info.value.hint
        assert info.value.details == {"filename": "image.png"}
        assert info.value.http_status == 415

    def test_binary_with_a_known_name_is_trusted(self, tmp_path) -> None:
        magic = b"\x00\x01\x02garbage"
        path = kit.write(tmp_path / "x.xlsx", magic)
        assert detect_format(path, magic, "x.xlsx", None) is F.XLSX

    def test_utf16_text_is_not_mistaken_for_binary(self, tmp_path) -> None:
        magic = "a,b\n1,2\n".encode("utf-16")
        path = kit.write(tmp_path / "x.csv", magic)
        assert detect_format(path, magic, "x.csv", None) is F.CSV

    @pytest.mark.parametrize(
        ("name", "content", "expected"),
        [
            ("x.json", b'{"a": 1}\n{"a": 2}\n', F.NDJSON),  # .json that is really NDJSON
            ("x.csv", b'[{"a": 1}]', F.JSON),  # JSON served as .csv
            ("x.tsv", b'{"a": 1}\n{"a": 2}\n', F.NDJSON),
            (
                "x.csv",
                b"a\tb\n1\t2\n",
                F.CSV,
            ),  # a TSV called .csv keeps CSV; the sniffer finds tabs
            ("x.ndjson", b'[{"a": 1}]', F.NDJSON),
            ("x.json", b'[{"a": 1}]', F.JSON),
        ],
    )
    def test_text_formats_are_resniffed(self, tmp_path, name, content, expected) -> None:
        path = kit.write(tmp_path / name, content)
        assert detect_format(path, content, name, None) is expected

    def test_no_name_falls_back_to_sniffing(self, tmp_path) -> None:
        content = b"a\tb\n1\t2\n"
        path = kit.write(tmp_path / "blob", content)
        assert detect_format(path, content, None, None) is F.TSV
