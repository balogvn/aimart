"""CSV/TSV: dialect and header sniffing, encodings, ragged rows, messy headers."""

from __future__ import annotations

import datetime as dt

import pytest

import aimart_testkit as kit
from aimart_parquet.engine.readers.delimited import (
    _cell_kind,
    _read_sample,
    detect_header,
    sniff_dialect,
)
from aimart_parquet.errors import InvalidInput


class TestSniffDialect:
    @pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
    def test_finds_the_delimiter(self, delimiter: str) -> None:
        sample = "\n".join(delimiter.join(row) for row in [["a", "b", "c"], ["1", "2", "3"]])
        dialect, rows = sniff_dialect(sample, None)
        assert dialect.delimiter == delimiter
        assert dialect.width == 3
        assert dialect.consistency == 1.0
        assert rows[0] == ["a", "b", "c"]

    def test_quoted_delimiters_do_not_count(self) -> None:
        sample = 'name;note\n"Smith, J";"a, b, c"\n"Doe, K";"x"\n'
        dialect, _ = sniff_dialect(sample, None)
        assert dialect.delimiter == ";"
        assert dialect.width == 2

    def test_forced_delimiter_is_the_only_candidate(self) -> None:
        dialect, rows = sniff_dialect("a,b|c\n1,2|3\n", "|")
        assert dialect.delimiter == "|"
        assert rows[0] == ["a,b", "c"]

    def test_empty_sample(self) -> None:
        dialect, rows = sniff_dialect("", None)
        assert rows == []
        assert dialect.delimiter == ","

    def test_blank_lines_are_skipped(self) -> None:
        _, rows = sniff_dialect("a,b\n\n , \n1,2\n", None)
        assert rows == [["a", "b"], ["1", "2"]]


class TestHeaderDetection:
    @pytest.mark.parametrize(
        ("cell", "kind"),
        [
            ("12", "number"),
            ("-1.234,56", "number"),
            ("$1,234", "number"),
            ("5%", "number"),
            ("2024-01-15", "date"),
            ("15/01/2024 10:00", "date"),
            ("yes", "bool"),
            ("Name", "text"),
            ("  ", "empty"),
        ],
    )
    def test_cell_kinds(self, cell: str, kind: str) -> None:
        assert _cell_kind(cell) == kind

    def test_text_over_typed_body_is_a_header(self) -> None:
        assert detect_header([["id", "amount"], ["1", "2.5"], ["2", "3.5"]], 2)

    def test_typed_first_row_is_data(self) -> None:
        assert not detect_header([["1", "2.5"], ["2", "3.5"], ["3", "4.5"]], 2)

    def test_all_text_table_assumes_a_header(self) -> None:
        assert detect_header([["a", "b"], ["c", "d"]], 2)

    def test_single_row_is_a_header(self) -> None:
        assert detect_header([["a", "b"]], 2)

    def test_empty_header_cell_over_typed_column_votes_header(self) -> None:
        assert detect_header([["", "x"], ["1", "a"], ["2", "b"]], 2)


def test_read_sample_drops_a_half_read_last_line(tmp_path, monkeypatch) -> None:
    from aimart_parquet.engine.readers import delimited

    monkeypatch.setattr(delimited, "_SAMPLE_BYTES", 10)
    path = kit.write(tmp_path / "f.csv", "﻿ab,cd\nef,gh\nij,kl\n")
    text, complete = _read_sample(path)
    assert not complete
    assert text == "ab,cd"  # BOM stripped, the partial "ef," line dropped


class TestReadDelimited:
    def test_messy_csv_end_to_end(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "customers.csv", kit.MESSY_CSV.encode())
        report = result.report
        assert report.source_format == "csv"
        assert report.encoding == "utf-8"
        assert report.delimiter == ","
        assert [c.name for c in report.columns] == [
            "customer_id",
            "customer_name",
            "amount_eur",
            "signup_date",
            "active",
            "column_6",
            "customer_name_2",
        ]
        assert report.columns[0].original_name == "Customer ID"  # BOM gone from the header
        rows = result.rows
        assert rows["customer_id"] == ["00123", "00124", "00125"]
        assert rows["customer_name"] == ["José", "Zoë", "NA"]  # NA kept in text columns
        assert rows["signup_date"] == [
            dt.date(2025, 4, 3),
            dt.date(2024, 12, 25),
            dt.date(2025, 1, 13),
        ]
        assert rows["active"] == [True, False, True]
        assert rows["customer_name_2"] == ["dup", "dup", None]  # ragged short row padded
        assert result.column("amount_eur").type == "double"
        assert rows["amount_eur"] == pytest.approx([1234.56, 12.5, 7.0])
        assert report.rows == 3

    def test_semicolon_files_prefer_decimal_commas(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "eu.csv", "Preis;Menge\n1,234;3\n5,678;4\n")
        assert result.report.delimiter == ";"
        assert result.rows["preis"] == pytest.approx([1.234, 5.678])
        assert result.rows["menge"] == [3, 4]

    def test_tab_delimited_reports_tsv(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "data.csv", "a\tb\n1\t2\n")
        assert result.report.source_format == "tsv"
        assert result.report.delimiter == "\t"

    def test_tsv_extension_forces_tabs(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "data.tsv", "a,b\tc\n1,2\t3\n")
        assert [c.name for c in result.report.columns] == ["a_b", "c"]

    def test_explicit_delimiter_and_header_options(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "data.txt", "a|b\n1|2\n", delimiter="|", header=False)
        assert result.rows == {"column_1": ["a", "1"], "column_2": ["b", "2"]}

    def test_headerless_numeric_file(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "n.csv", "1,2.5\n2,3.5\n")
        assert result.rows == {"column_1": [1, 2], "column_2": [2.5, 3.5]}

    def test_ragged_rows_are_reported(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "r.csv", "a,b,c\n1,2,3\n4,5\n6,7,8,9\n10,11,12\n")
        assert result.rows["c"] == [3, None, 8, 12]
        assert any("more fields than the header" in w for w in result.report.warnings)

    def test_irregular_widths_warn(self, tmp_path) -> None:
        text = "a,b,c\n1,2\n3\n4,5,6,7\n8,9,10,11,12\n"
        result = kit.convert_bytes(tmp_path, "w.csv", text)
        assert any("row widths are irregular" in w for w in result.report.warnings)

    def test_latin1_is_transcoded_and_reported(self, tmp_path) -> None:
        data = "Name,Stadt\nJürgen,München\nGroß,Köln\n".encode("latin-1")
        result = kit.convert_bytes(tmp_path, "de.csv", data)
        assert result.rows["stadt"] == ["München", "Köln"]
        assert result.report.encoding != "utf-8"

    def test_utf16_csv(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "w.csv", "a,b\n1,2\n".encode("utf-16"))
        assert result.rows == {"a": [1], "b": [2]}
        assert result.report.encoding is not None
        assert result.report.encoding.startswith("utf-16")

    def test_forced_encoding_with_undecodable_bytes_warns(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "x.csv", b"a\nx\x81y\n", encoding="cp1252")
        assert result.rows["a"] == ["x�y"]
        assert any("could not be decoded as cp1252" in w for w in result.report.warnings)

    def test_empty_file_is_invalid(self, tmp_path) -> None:
        with pytest.raises(InvalidInput, match="no rows"):
            kit.convert_bytes(tmp_path, "e.csv", b"")

    def test_whitespace_only_file_is_invalid(self, tmp_path) -> None:
        with pytest.raises(InvalidInput, match="no rows"):
            kit.convert_bytes(tmp_path, "e.csv", b"\n\n  \n")

    def test_header_only_file_has_zero_rows(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "h.csv", "a,b\n")
        assert result.report.rows == 0
        assert [c.name for c in result.report.columns] == ["a", "b"]

    def test_quoted_newlines_and_escaped_quotes(self, tmp_path) -> None:
        text = 'id,note\n1,"line one\nline two"\n2,"she said ""hi"""\n'
        result = kit.convert_bytes(tmp_path, "q.csv", text)
        assert result.rows["note"] == ["line one\nline two", 'she said "hi"']

    def test_text_bytes_drive_the_token_estimate(self, tmp_path) -> None:
        text = "a,b\n" + "1,2\n" * 1000
        result = kit.convert_bytes(tmp_path, "t.csv", text)
        assert result.report.estimated_tokens_raw == len(text) // 4
        assert result.report.estimated_tokens_schema < result.report.estimated_tokens_raw
