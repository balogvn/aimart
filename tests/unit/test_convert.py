"""The conversion pipeline: unwrapping, schema validation, sorting, writing, honest reports."""

from __future__ import annotations

import bz2
import datetime as dt
import decimal
import gzip
import json

import pyarrow.parquet as pq
import pytest

import aimart_testkit as kit
from aimart_parquet import __version__
from aimart_parquet.engine.convert import READERS, unwrap
from aimart_parquet.engine.limits import Limits
from aimart_parquet.engine.models import InputFormat
from aimart_parquet.errors import InputTooLarge, InvalidInput, InvalidRequest, SchemaViolation

CSV = "id,amount,name,when\n1,12.5,b,2024-01-02\n2,7,a,2024-01-01\n3,x,c,2024-01-03\n"


def test_every_format_has_a_reader() -> None:
    assert set(READERS) == set(InputFormat)


class TestUnwrap:
    def test_inputs_outside_the_workdir_are_copied_in(self, tmp_path) -> None:
        src = kit.write(tmp_path / "outside" / "data.csv.gz", gzip.compress(b"a\n1\n"))
        workdir = tmp_path / "work"
        workdir.mkdir()
        path, name = unwrap(src, "data.csv.gz", workdir, Limits())
        assert path.parent == workdir
        assert name == "data.csv"
        assert path.read_bytes() == b"a\n1\n"

    @pytest.mark.parametrize(
        ("name", "pack", "expected"),
        [
            ("d.csv.gz", gzip.compress, "d.csv"),
            ("d.csv.GZIP", gzip.compress, "d.csv"),
            ("d.csv.bz2", bz2.compress, "d.csv"),
            ("noext", gzip.compress, "noext"),
            (None, gzip.compress, None),
        ],
    )
    def test_compression_suffix_is_dropped(self, tmp_path, name, pack, expected) -> None:
        workdir = tmp_path / "work"
        src = kit.write(workdir / "in", pack(b"a\n1\n"))
        path, out_name = unwrap(src, name, workdir, Limits())
        assert out_name == expected
        assert path.read_bytes() == b"a\n1\n"

    def test_zip_member_name_replaces_the_archive_name(self, tmp_path) -> None:
        workdir = tmp_path / "work"
        src = kit.write(workdir / "in", kit.zip_bytes({"folder/survey.sav": b"$FL2"}))
        path, name = unwrap(src, "upload.zip", workdir, Limits())
        assert name == "survey.sav"
        assert path.read_bytes() == b"$FL2"

    def test_office_zips_are_left_alone(self, tmp_path) -> None:
        workdir = tmp_path / "work"
        book = kit.messy_workbook(workdir / "book.xlsx")
        assert unwrap(book, "book.xlsx", workdir, Limits()) == (book, "book.xlsx")

    def test_plain_files_pass_through(self, tmp_path) -> None:
        workdir = tmp_path / "work"
        src = kit.write(workdir / "a.csv", b"a\n1\n")
        assert unwrap(src, "a.csv", workdir, Limits()) == (src, "a.csv")


class TestWrappedInputs:
    @pytest.mark.parametrize(
        ("name", "data"),
        [
            ("a.csv.gz", gzip.compress(CSV.encode())),
            ("a.csv.bz2", bz2.compress(CSV.encode())),
            ("a.zip", kit.zip_bytes({"inner/a.csv": CSV.encode(), "__MACOSX/._a.csv": b"x"})),
            ("a.gz", gzip.compress(CSV.encode())),
        ],
    )
    def test_compressed_csv(self, tmp_path, name, data) -> None:
        result = kit.convert_bytes(tmp_path, name, data)
        assert result.rows["id"] == [1, 2, 3]
        assert result.report.source_bytes == len(data)

    def test_gzipped_survey_keeps_labels(self, tmp_path) -> None:
        src = kit.survey_file(tmp_path / "raw" / "s.sav", "sav")
        result = kit.convert_bytes(tmp_path, "s.sav.gz", gzip.compress(src.read_bytes()))
        assert result.column("gender").value_labels == {"1": "Male", "2": "Female"}

    def test_zip_bomb(self, tmp_path) -> None:
        bomb = kit.zip_bytes({"bomb.csv": b"0" * (30 * 1024 * 1024)})
        with pytest.raises(InvalidInput, match="zip bomb"):
            kit.convert_bytes(tmp_path, "bomb.zip", bomb)

    def test_gzip_over_the_decompressed_cap(self, tmp_path) -> None:
        data = gzip.compress(b"a\n" + b"1\n" * 600_000)
        with pytest.raises(InputTooLarge):
            kit.convert_bytes(
                tmp_path, "big.csv.gz", data, limits=Limits(max_decompressed_bytes=1_000_000)
            )

    def test_raw_input_over_the_size_cap(self, tmp_path) -> None:
        with pytest.raises(InputTooLarge) as info:
            kit.convert_bytes(tmp_path, "a.csv", CSV, limits=Limits(max_input_bytes=10))
        assert info.value.details == {"limit_bytes": 10}

    def test_truncated_gzip(self, tmp_path) -> None:
        with pytest.raises(InvalidInput, match="corrupt or truncated"):
            kit.convert_bytes(tmp_path, "a.csv.gz", gzip.compress(CSV.encode())[:-10])


class TestExpectedSchema:
    def test_non_strict_cast_counts_rejects_and_keeps_extras(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            CSV,
            expected_schema=[{"name": "id", "type": "int64"}, {"name": "amount", "type": "int64"}],
        )
        assert [c.name for c in result.report.columns] == ["id", "amount", "name", "when"]
        assert result.rows["amount"] == [None, 7, None]  # 12.5 and x do not fit int64
        amount = result.column("amount")
        assert amount.type == "int64"
        assert amount.rejected_count == 2
        assert "cast to int64 per expected_schema" in amount.notes

    def test_int_cast_of_a_float_column_never_rounds(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path, "v.csv", "v\n1\n2\n3.5\n", expected_schema=[{"name": "v", "type": "int64"}]
        )
        assert result.rows["v"] == [1, 2, None]
        assert result.column("v").rejected_count == 1

    def test_strict_fails_on_rejects_and_extra_columns(self, tmp_path) -> None:
        with pytest.raises(SchemaViolation) as info:
            kit.convert_bytes(
                tmp_path,
                "a.csv",
                CSV,
                expected_schema=[
                    {"name": "id", "type": "int64"},
                    {"name": "amount", "type": "float64"},
                ],
                strict=True,
            )
        assert info.value.http_status == 422
        assert info.value.details["violations"] == [
            {"column": "amount", "problem": "1 values do not fit float64"},
            {"column": "name, when", "problem": "not in expected_schema"},
        ]

    def test_strict_success(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            CSV,
            expected_schema=[
                {"name": "ID", "type": "string"},  # matched case-insensitively
                {"name": "name", "type": "string"},
                {"name": "amount", "type": "string"},
                {"name": "when", "type": "date"},
            ],
            strict=True,
        )
        assert result.rows["ID"] == ["1", "2", "3"]
        assert result.column("ID").original_name == "id"

    def test_missing_nullable_column_is_added_as_nulls(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            CSV,
            expected_schema=[{"name": "region", "type": "string"}],
        )
        assert result.rows["region"] == [None, None, None]
        assert "added as all-null: missing from input" in result.column("region").notes

    @pytest.mark.parametrize(
        "schema",
        [
            {"expected_schema": [{"name": "region", "type": "string", "nullable": False}]},
            {"expected_schema": [{"name": "region", "type": "string"}], "strict": True},
        ],
    )
    def test_missing_column_violations(self, tmp_path, schema) -> None:
        with pytest.raises(SchemaViolation) as info:
            kit.convert_bytes(tmp_path, "a.csv", CSV, **schema)
        assert {"column": "region", "problem": "missing"} in info.value.details["violations"]

    def test_nulls_in_a_non_nullable_column(self, tmp_path) -> None:
        with pytest.raises(SchemaViolation) as info:
            kit.convert_bytes(
                tmp_path,
                "a.csv",
                "a,b\n1,\n2,3\n",
                expected_schema=[{"name": "b", "type": "int64", "nullable": False}],
            )
        assert info.value.details["violations"] == [
            {"column": "b", "problem": "1 nulls in a non-nullable column"}
        ]

    def test_decimal_precision_and_scale(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            CSV,
            expected_schema=[{"name": "amount", "type": "decimal", "precision": 10, "scale": 3}],
        )
        assert result.column("amount").type == "decimal128(10, 3)"
        assert result.rows["amount"] == [decimal.Decimal("12.500"), decimal.Decimal("7.000"), None]

    def test_typed_column_cast_to_another_type(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            CSV,
            expected_schema=[
                {"name": "when", "type": "timestamp"},
                {"name": "id", "type": "float64"},
            ],
        )
        assert result.rows["when"][0] == dt.datetime(2024, 1, 2)
        assert result.rows["id"] == [1.0, 2.0, 3.0]

    def test_bool_cast_from_text(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "b.csv",
            "v\n1\n0\nyes\n",
            expected_schema=[{"name": "v", "type": "bool"}],
        )
        assert result.rows["v"] == [True, False, True]

    def test_schema_on_uninferred_text(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.csv",
            "a\n1\n2\n",
            infer_types=False,
            expected_schema=[{"name": "a", "type": "int64"}],
        )
        assert result.rows["a"] == [1, 2]
        assert result.column("a").type == "int64"


class TestShaping:
    def test_sort_by_orders_rows_nulls_last(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "a.csv", "k,v\nb,1\n,2\na,3\n", sort_by=["K"])
        assert result.rows == {"k": ["a", "b", None], "v": [3, 1, 2]}

    def test_sort_by_unknown_column_lists_the_real_ones(self, tmp_path) -> None:
        with pytest.raises(InvalidRequest) as info:
            kit.convert_bytes(tmp_path, "a.csv", CSV, sort_by=["nope"])
        assert info.value.details == {"columns": ["id", "amount", "name", "when"]}

    @pytest.mark.parametrize(
        ("compression", "codec"),
        [("zstd", "ZSTD"), ("snappy", "SNAPPY"), ("gzip", "GZIP"), ("none", "UNCOMPRESSED")],
    )
    def test_compression_options(self, tmp_path, compression, codec) -> None:
        result = kit.convert_bytes(tmp_path, "a.csv", CSV, compression=compression)
        meta = pq.read_metadata(result.path)
        assert meta.row_group(0).column(0).compression == codec

    @pytest.mark.parametrize("compression", ["zstd", "snappy", "none"])
    def test_compression_options_with_the_labelled_writer(self, tmp_path, compression) -> None:
        src = kit.survey_file(tmp_path / "s.sav", "sav")
        result = kit.convert_path(src, compression=compression)
        codec = {"zstd": "ZSTD", "snappy": "SNAPPY", "none": "UNCOMPRESSED"}[compression]
        assert pq.read_metadata(result.path).row_group(0).column(0).compression == codec

    def test_row_group_size(self, tmp_path) -> None:
        text = "a\n" + "".join(f"{i}\n" for i in range(2500))
        result = kit.convert_bytes(tmp_path, "a.csv", text, row_group_size=1000)
        # DuckDB treats ROW_GROUP_SIZE as a target rounded to its 2048-row vectors.
        assert pq.read_metadata(result.path).num_row_groups >= 2
        assert result.report.rows == 2500

    def test_names_can_be_kept_verbatim(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path, "a.csv", "Total Amount,X\n1,2\n", normalize_column_names=False
        )
        assert [c.name for c in result.report.columns] == ["Total Amount", "X"]


class TestReport:
    def test_report_is_read_back_from_the_written_file(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "a.csv", "a,b\n1,\n2,x\n,y\n")
        footer = pq.read_metadata(result.path)
        assert result.report.rows == footer.num_rows == 3
        assert result.report.output_bytes == result.path.stat().st_size
        a, b = result.report.columns
        assert (a.type, a.null_count) == ("int64", 1)
        assert (b.type, b.null_count) == ("string", 1)
        assert result.report.duration_ms >= 0

    def test_renamed_columns_record_their_original_name(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "a.csv", "Order ID,qty\n1,2\n")
        field = pq.read_schema(result.path).field("order_id")
        kv = pq.read_metadata(result.path).metadata
        assert json.loads(kv[b"aimart.columns"]) == {"order_id": {"original_name": "Order ID"}}
        assert field.metadata is None  # the fast DuckDB writer cannot write field metadata

    def test_provenance_record(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "sales.csv", "a\n1\n")
        kv = pq.read_metadata(result.path).metadata
        provenance = json.loads(kv[b"aimart.provenance"])
        assert provenance["tool"] == "aimart-parquet"
        assert provenance["version"] == __version__
        assert provenance["source_format"] == "csv"
        assert provenance["source_file"] == "sales.csv"
        converted = dt.datetime.fromisoformat(provenance["converted_at"])
        assert converted.tzinfo is not None

    def test_column_metadata_map_is_dropped_when_huge(self, tmp_path, monkeypatch) -> None:
        from aimart_parquet.engine import convert

        monkeypatch.setattr(convert, "_MAX_COLUMNS_META", 10)
        result = kit.convert_bytes(tmp_path, "a.csv", "Order ID\n1\n")
        assert b"aimart.columns" not in pq.read_metadata(result.path).metadata
