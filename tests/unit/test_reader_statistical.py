"""SPSS/Stata/SAS: the labels are the product, so they must survive into Parquet metadata."""

from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import aimart_testkit as kit
from aimart_parquet.engine.detect import _SAS7BDAT_MAGIC
from aimart_parquet.engine.readers import statistical
from aimart_parquet.engine.readers.statistical import (
    _code_key,
    _concat,
    _label_lookup,
    _maybe_int,
    _to_arrow,
    labels_json,
)
from aimart_parquet.errors import InvalidInput

VALUE_LABELS = {"1": "Male", "2": "Female"}


def _field_meta(path, name: str) -> dict[str, str]:
    field = pq.read_schema(path).field(name)
    return {k.decode(): v.decode() for k, v in (field.metadata or {}).items()}


def _columns_kv(path) -> dict:
    kv = pq.read_metadata(path).metadata
    return json.loads(kv[b"aimart.columns"])


@pytest.mark.parametrize("fmt", ["sav", "zsav", "dta"])
class TestLabelledFormats:
    def test_labels_land_in_field_metadata_and_the_kv_map(self, tmp_path, fmt) -> None:
        # No extension on purpose: detection must come from the bytes.
        src = kit.survey_file(tmp_path / f"survey.{fmt}", fmt)
        result = kit.convert_path(src, filename=None)
        assert result.report.source_format == fmt
        assert result.rows == {
            "gender": [1, 2, 1, None],
            "score": [1.5, 2.25, 3.0, 4.0],
            "name": ["a", "b", "c", "d"],
        }
        assert result.column("gender").type == "int64"  # integral doubles become integers
        assert result.column("gender").label == "Respondent gender"
        assert result.column("gender").value_labels == VALUE_LABELS
        meta = _field_meta(result.path, "gender")
        assert meta["label"] == "Respondent gender"
        assert json.loads(meta["value_labels"]) == VALUE_LABELS
        kv = _columns_kv(result.path)
        assert kv["gender"] == {"label": "Respondent gender", "value_labels": VALUE_LABELS}
        assert kv["score"] == {"label": "Satisfaction score"}

    def test_apply_replaces_codes_with_labels(self, tmp_path, fmt) -> None:
        src = kit.survey_file(tmp_path / f"survey.{fmt}", fmt)
        result = kit.convert_path(src, value_labels="apply")
        assert result.rows["gender"] == ["Male", "Female", "Male", None]
        assert result.column("gender").value_labels == VALUE_LABELS  # still documented

    def test_both_adds_a_label_column(self, tmp_path, fmt) -> None:
        src = kit.survey_file(tmp_path / f"survey.{fmt}", fmt)
        result = kit.convert_path(src, value_labels="both")
        assert result.rows["gender"] == [1, 2, 1, None]
        assert result.rows["gender_label"] == ["Male", "Female", "Male", None]
        assert result.column("gender_label").label == "Respondent gender"


def test_xpt_keeps_variable_labels(tmp_path) -> None:
    src = kit.survey_file(tmp_path / "survey.xpt", "xpt")
    result = kit.convert_path(src, filename=None)
    assert result.report.source_format == "xpt"
    assert result.column("score").label == "Satisfaction score"
    assert result.rows["gender"] == [1, 2, 1, None]


def test_por_is_read_and_names_normalized(tmp_path) -> None:
    src = kit.survey_file(tmp_path / "survey.por", "por")
    result = kit.convert_path(src, filename=None)
    assert result.report.source_format == "por"
    assert [c.name for c in result.report.columns] == ["gender", "score", "name"]
    assert result.column("gender").original_name == "GENDER"  # SPSS portable upper-cases
    assert _columns_kv(result.path)["gender"] == {"original_name": "GENDER"}


def test_truncated_file_is_invalid_input(tmp_path) -> None:
    src = kit.survey_file(tmp_path / "survey.sav", "sav")
    broken = kit.write(tmp_path / "broken.sav", src.read_bytes()[:300])
    with pytest.raises(InvalidInput, match="Could not read the sav file"):
        kit.convert_path(broken)


def test_fake_sas7bdat_is_detected_then_rejected_cleanly(tmp_path) -> None:
    src = kit.write(tmp_path / "data.bin", _SAS7BDAT_MAGIC + b"\x00" * 2000)
    with pytest.raises(InvalidInput, match="sas7bdat"):
        kit.convert_path(src, filename=None)


def test_an_all_missing_variable_converts_to_an_all_null_column(tmp_path) -> None:
    import polars as pl
    import pyreadstat

    df = pl.DataFrame({"a": [1.0, 2.0], "skipped": pl.Series([None, None], dtype=pl.Float64)})
    src = tmp_path / "survey.sav"
    pyreadstat.write_sav(df, str(src))
    result = kit.convert_path(src)
    assert result.rows == {"a": [1, 2], "skipped": [None, None]}


def test_reads_in_chunks(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(statistical, "_CHUNK_ROWS", 3)
    src = kit.survey_file(tmp_path / "survey.sav", "sav")
    result = kit.convert_path(src)
    assert result.rows["name"] == ["a", "b", "c", "d"]
    assert result.rows["gender"] == [1, 2, 1, None]


def test_encoding_option_is_passed_through(tmp_path) -> None:
    src = kit.survey_file(tmp_path / "survey.sav", "sav")
    result = kit.convert_path(src, encoding="utf-8")
    assert result.report.rows == 4


class TestArrowHelpers:
    def test_code_key(self) -> None:
        assert _code_key(1.0) == "1"
        assert _code_key(1.5) == "1.5"
        assert _code_key("A") == "A"

    def test_to_arrow_falls_back_to_text_for_mixed_values(self) -> None:
        assert _to_arrow([1, "x", None]).to_pylist() == ["1", "x", None]
        assert _to_arrow([1, 2]).type == pa.int64()

    def test_concat_unifies_null_chunks(self) -> None:
        out = _concat([pa.array([None, None]), pa.array([1.5])])
        assert out.type == pa.float64()
        assert out.to_pylist() == [None, None, 1.5]

    def test_concat_mixed_types_become_text(self) -> None:
        out = _concat([pa.array([1]), pa.array(["x"])])
        assert out.type == pa.string()
        assert out.to_pylist() == ["1", "x"]

    def test_concat_of_no_chunks_is_an_empty_text_column(self) -> None:
        out = _concat([])
        assert len(out) == 0
        assert out.type == pa.string()

    def test_concat_of_all_null_chunks_is_a_null_text_column(self) -> None:
        out = _concat([pa.array([None, None]), pa.array([None])])
        assert out.type == pa.string()
        assert out.to_pylist() == [None, None, None]

    def test_maybe_int(self) -> None:
        assert _maybe_int(pa.chunked_array([[1.0, 2.0, None]])).type == pa.int64()
        assert _maybe_int(pa.chunked_array([[1.0, 2.5]])).type == pa.float64()
        assert _maybe_int(pa.chunked_array([[2.0**60]])).type == pa.float64()
        assert _maybe_int(pa.chunked_array([[None, None]], type=pa.float64())).type == pa.float64()
        assert _maybe_int(pa.chunked_array([["a"]])).type == pa.string()

    def test_label_lookup_leaves_unlabelled_codes_null(self) -> None:
        codes, labels = _label_lookup(pa.chunked_array([[1, 3, None]]), {"1": "Male"})
        assert codes.to_pylist() == ["1", "3", None]
        assert labels.to_pylist() == ["Male", None, None]

    def test_labels_json_is_stable(self) -> None:
        assert labels_json({"2": "b", "1": "é"}) == '{"1": "é", "2": "b"}'
