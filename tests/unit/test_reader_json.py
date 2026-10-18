"""JSON and NDJSON: fast DuckDB path, envelopes, repair, malformed lines, nesting."""

from __future__ import annotations

import json

import pytest

import aimart_testkit as kit
from aimart_parquet.engine.limits import Limits
from aimart_parquet.engine.readers import jsonish
from aimart_parquet.engine.readers.jsonish import _find_records
from aimart_parquet.errors import InputTooLarge, InvalidInput, InvalidRequest

RECORDS = [
    {"id": 1, "user": {"name": "a", "age": 3}, "tags": ["x", "y"]},
    {"id": 2, "user": {"name": "b", "age": 4}, "tags": []},
]


class TestFindRecords:
    def test_top_level_array(self) -> None:
        assert _find_records([{"a": 1}], None) == ([{"a": 1}], None)

    def test_envelope_is_found_and_reported(self) -> None:
        doc = {"meta": {"n": 3}, "data": {"items": [{"a": 1}, {"a": 2}]}}
        assert _find_records(doc, None) == ([{"a": 1}, {"a": 2}], "data.items")

    def test_the_longest_record_array_wins(self) -> None:
        doc = {"errors": [{"e": 1}], "results": [{"a": 1}, {"a": 2}, {"a": 3}]}
        assert _find_records(doc, None)[1] == "results"

    def test_arrays_of_scalars_are_not_records(self) -> None:
        doc = {"ids": [1, 2, 3], "rows": [{"a": 1}]}
        assert _find_records(doc, None)[1] == "rows"

    def test_search_depth_is_bounded(self) -> None:
        doc: dict = {"a": {"b": {"c": {"d": {"e": {"f": [{"x": 1}]}}}}}}
        records, path = _find_records(doc, None)
        assert path is None
        assert records == [doc]  # too deep to search: the object itself is one row

    def test_single_object_is_one_row(self) -> None:
        assert _find_records({"a": 1}, None) == ([{"a": 1}], None)

    def test_explicit_path_with_list_index(self) -> None:
        doc = {"pages": [{"rows": [{"a": 1}]}, {"rows": [{"a": 2}]}]}
        assert _find_records(doc, "pages.1.rows") == ([{"a": 2}], "pages.1.rows")

    def test_missing_path_is_a_400(self) -> None:
        with pytest.raises(InvalidRequest, match="failed at 'nope'"):
            _find_records({"data": {}}, "data.nope")

    def test_out_of_range_index(self) -> None:
        with pytest.raises(InvalidRequest):
            _find_records({"pages": [1]}, "pages.5")

    def test_path_to_a_non_array(self) -> None:
        with pytest.raises(InvalidRequest, match="points at a dict"):
            _find_records({"data": {"x": 1}}, "data")


class TestReadJson:
    def test_array_keeps_nested_types(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "a.json", json.dumps(RECORDS))
        types = {c.name: c.type for c in result.report.columns}
        assert types["id"] == "int64"
        assert types["user"].startswith("struct<")
        assert types["tags"].startswith("list<")
        assert result.rows["user"] == [{"name": "a", "age": 3}, {"name": "b", "age": 4}]
        assert result.report.source_format == "json"

    def test_flatten_nested(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path,
            "a.json",
            json.dumps(RECORDS),
            flatten_nested=True,
            normalize_column_names=False,
        )
        rows = result.rows
        assert rows["user__name"] == ["a", "b"]
        assert rows["user__age"] == [3, 4]
        assert rows["tags"] == ['["x","y"]', "[]"]  # lists become JSON text

    def test_flattened_names_do_not_collide_with_real_ones(self, tmp_path) -> None:
        records = [{"user_name": "top", "user": {"name": "nested"}}]
        result = kit.convert_bytes(tmp_path, "a.json", json.dumps(records), flatten_nested=True)
        names = [c.name for c in result.report.columns]
        assert len(set(names)) == len(names) == 2

    def test_envelope_is_unwrapped_with_a_warning(self, tmp_path) -> None:
        doc = {"meta": {"count": 3}, "data": {"items": [{"a": 1}, {"a": 2}, {"a": 3}]}}
        result = kit.convert_bytes(tmp_path, "e.json", json.dumps(doc))
        assert result.rows == {"a": [1, 2, 3]}
        assert "read records from JSON path 'data.items'" in result.report.warnings

    def test_explicit_records_path(self, tmp_path) -> None:
        doc = {"a": [{"x": 1}], "b": [{"y": 1}, {"y": 2}]}
        result = kit.convert_bytes(tmp_path, "e.json", json.dumps(doc), json_records_path="a")
        assert result.rows == {"x": [1]}

    def test_broken_json_is_repaired(self, tmp_path) -> None:
        text = "[{'a': 1, 'b': 'x',}, {'a': 2, 'b': 'y',},]"
        result = kit.convert_bytes(tmp_path, "b.json", text)
        assert result.rows == {"a": [1, 2], "b": ["x", "y"]}
        assert any(w.startswith("repaired malformed JSON") for w in result.report.warnings)

    def test_repair_has_a_size_cap(self, tmp_path) -> None:
        text = "[{'a': 1}," + " " * 2000 + "]"
        with pytest.raises(InvalidInput) as info:
            kit.convert_bytes(tmp_path, "b.json", text, limits=Limits(max_json_repair_bytes=100))
        assert info.value.hint is not None
        assert "not auto-repaired" in info.value.hint

    def test_unrepairable_garbage(self, tmp_path) -> None:
        with pytest.raises(InvalidInput, match="could not be repaired"):
            kit.convert_bytes(tmp_path, "g.json", "nonsense")

    def test_ndjson_with_bad_lines_skips_and_counts(self, tmp_path) -> None:
        text = '{"a": 1}\n{"a": 2}\nnot json\n\n{"a": 3}\n'
        result = kit.convert_bytes(tmp_path, "n.ndjson", text)
        assert result.rows == {"a": [1, 2, 3]}
        assert result.report.source_format == "ndjson"
        assert "skipped 1 malformed or empty JSON lines" in result.report.warnings

    def test_ndjson_heterogeneous_records_are_unified(self, tmp_path) -> None:
        text = '{"a": 1}\n{"b": "x"}\n{"a": 3, "b": "y"}\n'
        result = kit.convert_bytes(tmp_path, "n.jsonl", text)
        assert result.rows == {"a": [1, None, 3], "b": [None, "x", "y"]}

    def test_json_extension_holding_ndjson(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "n.json", '{"a": 1}\n{"a": 2}\n')
        assert result.report.source_format == "ndjson"
        assert result.rows == {"a": [1, 2]}

    def test_python_fallback_for_lines_duckdb_rejects(self, tmp_path) -> None:
        # Records that are not one-per-line objects: concatenated arrays on lines.
        text = '[{"a": 1}]\n[{"a": 2}]\n'
        result = kit.convert_bytes(tmp_path, "x.json", text)
        assert result.report.rows >= 1

    def test_mixed_scalar_types_become_text_then_climb_the_ladder(self, tmp_path) -> None:
        result = kit.convert_bytes(tmp_path, "m.json", '[{"v": 1}, {"v": "two"}, {"v": 3}]')
        assert result.rows == {"v": ["1", "two", "3"]}
        result = kit.convert_bytes(tmp_path, "m2.json", '[{"v": 1}, {"v": "2"}, {"v": 3.5}]')
        assert result.column("v").type == "double"

    def test_string_numbers_climb_the_ladder(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path, "s.json", '[{"zip": "02134", "n": "12", "d": "2024-01-15"}]'
        )
        assert result.rows["zip"] == ["02134"]
        assert result.rows["n"] == [12]
        assert result.column("d").type == "date32[day]"

    def test_envelope_without_records_is_invalid(self, tmp_path) -> None:
        with pytest.raises(InvalidInput, match="no records"):
            kit.convert_bytes(tmp_path, "e.json", '{"data": []}', json_records_path="data")

    def test_too_large_for_python_parse(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(jsonish, "_MAX_PYTHON_PARSE", 10)
        with pytest.raises(InputTooLarge) as info:
            kit.convert_bytes(tmp_path, "e.json", '{"data": {"items": [{"a": 1}]}}')
        assert info.value.hint is not None
        assert "newline-delimited" in info.value.hint

    # Depth 20,000 is past Python's json recursion limit, so these fail in Python with a clean
    # error. (Shallower documents, ~2,000 levels, reach DuckDB and crash it outright: that is
    # what job isolation is for, and it is tested in test_isolate.py, never in-process.)
    _DEEP = '{"a": ' * 20_000 + "1" + "}" * 20_000

    def test_ndjson_sniff_reports_deep_nesting_cleanly(self, tmp_path) -> None:
        with pytest.raises(InvalidInput, match="nested too deeply"):
            kit.convert_bytes(tmp_path, "d.json", self._DEEP)

    def test_python_path_reports_deep_nesting_cleanly(self, tmp_path) -> None:
        with pytest.raises(InvalidInput, match="nested too deeply"):
            kit.convert_bytes(tmp_path, "d.json", self._DEEP, json_records_path="a")

    def test_python_path_ndjson_recovery_tolerates_a_few_bad_lines(self, tmp_path) -> None:
        good = [json.dumps({"a": i}) for i in range(20)]
        text = "[1,\n" + "\n".join(good) + "\n"  # starts like an array, is really NDJSON
        result = kit.convert_bytes(tmp_path, "x.json", text)
        assert result.rows["a"] == list(range(20))
        assert "skipped 1 malformed JSON lines" in result.report.warnings

    def test_records_that_are_not_objects_get_a_value_column(self, tmp_path) -> None:
        result = kit.convert_bytes(
            tmp_path, "e.json", '{"data": {"rows": [{"a": 1}, {"a": 2}, 3]}}'
        )
        assert result.rows == {"a": [1, 2, None], "value": [None, None, 3]}


@pytest.mark.parametrize("text", ["[]", "[{}, {}]"])
def test_arrays_without_usable_records_are_invalid(tmp_path, text: str) -> None:
    with pytest.raises(InvalidInput):
        kit.convert_bytes(tmp_path, "e.json", text)
