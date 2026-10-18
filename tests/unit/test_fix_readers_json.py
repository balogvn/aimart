"""Nested keys that differ only by letter case: DuckDB kept the first and dropped the rest.

DuckDB matches STRUCT field names case-insensitively, so ``{"a": {"k": 1, "K": 2}}`` (HTTP header
maps, mixed ``ID``/``id``) failed its type detection with ``Duplicate name "K" in struct``, and
with ``ignore_errors=true`` it silently kept ``k`` and lost every ``K`` value, on the fast path,
the NDJSON path and the Python fallback alike. Top-level keys were already fine (``id``, ``id_1``).
"""

from __future__ import annotations

import json

import pytest

import aimart_testkit as kit
from aimart_parquet.engine.readers import jsonish
from aimart_parquet.errors import InvalidInput

PAIRS = [{"a": {"k": 1, "K": 2}}, {"a": {"k": 3, "K": 4}}]

LAYOUTS = [
    pytest.param("p.json", json.dumps(PAIRS), id="array"),
    pytest.param("p.ndjson", "".join(json.dumps(r) + "\n" for r in PAIRS), id="ndjson"),
    pytest.param("p.json", json.dumps({"data": PAIRS}), id="envelope-python-path"),
    pytest.param("p.json", json.dumps(PAIRS)[:-1] + ",]", id="repaired-python-path"),
    pytest.param(
        "p.ndjson",
        json.dumps(PAIRS[0]) + "\n{not json\n" + json.dumps(PAIRS[1]) + "\n",
        id="ndjson-with-a-malformed-line",
    ),
]


def _renamed(result: kit.Converted) -> list[str]:
    return [w for w in result.report.warnings if "letter case" in w]


@pytest.mark.parametrize(("name", "text"), LAYOUTS)
def test_case_colliding_keys_keep_every_value(tmp_path, name: str, text: str) -> None:
    result = kit.convert_bytes(tmp_path, name, text)
    assert result.column("a").type == "struct<k: int64, K_2: int64>"
    assert result.rows["a"] == [{"k": 1, "K_2": 2}, {"k": 3, "K_2": 4}]
    (warning,) = _renamed(result)
    assert "a.K is now a.K_2" in warning


@pytest.mark.parametrize(("name", "text"), LAYOUTS)
def test_case_colliding_keys_flattened(tmp_path, name: str, text: str) -> None:
    result = kit.convert_bytes(tmp_path, name, text, flatten_nested=True)
    assert result.rows == {"a_k": [1, 3], "a_k_2": [2, 4]}


def test_the_same_key_spelled_differently_in_two_records(tmp_path) -> None:
    # Not in one object, but one struct type: DuckDB folded "K" into "k" and the 2 became null.
    result = kit.convert_bytes(tmp_path, "x.json", '[{"a": {"k": 1}}, {"a": {"K": 2}}]')
    assert result.rows["a"] == [{"k": 1, "K_2": None}, {"k": None, "K_2": 2}]


def test_arrays_of_objects(tmp_path) -> None:
    text = json.dumps([{"a": [{"k": 1, "K": 2}, {"K": 3}]}, {"a": [{"k": 4}]}])
    result = kit.convert_bytes(tmp_path, "x.json", text)
    assert result.rows["a"] == [
        [{"k": 1, "K_2": 2}, {"k": None, "K_2": 3}],
        [{"k": 4, "K_2": None}],
    ]
    assert "a[].K is now a[].K_2" in _renamed(result)[0]
    flat = kit.convert_bytes(tmp_path, "x.json", text, flatten_nested=True)
    assert json.loads(flat.rows["a"][0]) == [{"k": 1, "K_2": 2}, {"k": None, "K_2": 3}]


def test_deep_nesting_and_lists_of_lists(tmp_path) -> None:
    text = json.dumps([{"a": {"b": [[{"c": {"Id": 1, "ID": 2, "id": 3}}]]}}])
    result = kit.convert_bytes(tmp_path, "x.json", text)
    assert result.rows["a"] == [{"b": [[{"c": {"Id": 1, "ID_2": 2, "id_3": 3}}]]}]
    deep = kit.convert_bytes(
        tmp_path,
        "y.json",
        json.dumps([{"a": {"b": {"Id": 1, "ID": 2, "id": 3}}}]),
        flatten_nested=True,
    )
    assert deep.rows == {"a_b_id": [1], "a_b_id_2": [2], "a_b_id_3": [3]}


def test_a_new_name_never_takes_a_real_key(tmp_path) -> None:
    result = kit.convert_bytes(tmp_path, "x.json", '[{"a": {"k": 1, "K": 2, "K_2": 3}}]')
    assert result.rows["a"] == [{"k": 1, "K_3": 2, "K_2": 3}]
    # One that arrives in a later record cannot be foreseen: it is renamed in turn.
    later = kit.convert_bytes(tmp_path, "y.json", '[{"a": {"k": 1, "K": 2}}, {"a": {"K_2": 3}}]')
    assert later.rows["a"] == [
        {"k": 1, "K_2": 2, "K_2_2": None},
        {"k": None, "K_2": None, "K_2_2": 3},
    ]


def test_children_of_renamed_keys_are_their_own_struct(tmp_path) -> None:
    text = '[{"a": {"k": {"x": 1}, "K": {"X": 2}}}]'
    result = kit.convert_bytes(tmp_path, "x.json", text, flatten_nested=True)
    assert result.rows == {"a_k_x": [1], "a_k_2_x": [2]}


def test_top_level_keys_are_left_to_duckdb(tmp_path) -> None:
    result = kit.convert_bytes(tmp_path, "x.json", '[{"id": 1, "ID": 2, "a": {"k": 1, "K": 2}}]')
    assert [c.name for c in result.report.columns] == ["id", "id_1", "a"]
    assert result.rows["id"] == [1]
    assert result.rows["id_1"] == [2]


def test_non_ascii_case_variants_are_already_distinct(tmp_path) -> None:
    # DuckDB folds ASCII letters only: renaming these would change names for nothing.
    result = kit.convert_bytes(tmp_path, "x.json", '[{"a": {"é": 1, "É": 2}}]')
    assert result.rows["a"] == [{"é": 1, "É": 2}]
    assert not _renamed(result)


def test_clean_input_gets_no_warning(tmp_path) -> None:
    result = kit.convert_bytes(tmp_path, "x.json", '[{"a": {"k": 1, "j": 2}}]')
    assert result.report.warnings == []


def test_malformed_lines_are_still_counted_when_keys_are_renamed(tmp_path) -> None:
    text = '{"a": {"k": 1, "K": 2}}\nnope\n\n{"a": {"k": 3}}\n'
    result = kit.convert_bytes(tmp_path, "x.ndjson", text)
    assert result.rows["a"] == [{"k": 1, "K_2": 2}, {"k": 3, "K_2": None}]
    assert "skipped 1 malformed or empty JSON lines" in result.report.warnings


def test_a_malformed_line_before_the_collision(tmp_path) -> None:
    # The strict read stops at the bad line, before it can say anything about keys: the key
    # structure of the lenient read is what finds them.
    text = '{"a": {"k": 1}}\n{oops\n{"a": {"k": 3, "K": 4}}\n'
    result = kit.convert_bytes(tmp_path, "x.ndjson", text)
    assert result.rows["a"] == [{"k": 1, "K_2": None}, {"k": 3, "K_2": 4}]
    assert "skipped 1 malformed or empty JSON lines" in result.report.warnings
    assert _renamed(result)


def test_the_rename_pass_alone_drops_malformed_lines(tmp_path, monkeypatch) -> None:
    # Treat "malformed" as "keys to rename": the pass must skip bad lines like the lenient read
    # (they are counted from the source), and rename nothing it does not have to.
    monkeypatch.setattr(jsonish, "_UNKEEPABLE_KEY_ERRORS", ("Malformed JSON",))
    result = kit.convert_bytes(tmp_path, "x.ndjson", '{"a": {"k": 1}}\n{oops\n{"a": {"k": 2}}\n')
    assert result.rows == {"a": [{"k": 1}, {"k": 2}]}
    assert result.report.warnings == ["skipped 1 malformed or empty JSON lines"]


@pytest.mark.parametrize(
    ("name", "text"),
    [
        pytest.param("x.json", '[{"a": {"": 1, "k": 2}}]', id="array"),
        pytest.param("x.ndjson", '{"a": {"": 1, "k": 2}}\n{"a": {"k": 3}}\n', id="ndjson"),
        pytest.param("x.json", '{"data": [{"a": {"": 1, "k": 2}}]}', id="envelope"),
    ],
)
def test_an_empty_nested_key_gets_a_name(tmp_path, name: str, text: str) -> None:
    # DuckDB cannot name it: the whole read failed with a bare error (a 500, or "Internal error").
    result = kit.convert_bytes(tmp_path, name, text)
    assert result.rows["a"][0] == {"unnamed": 1, "k": 2}
    assert 'a."" is now a.unnamed' in _renamed(result)[0]


def test_an_empty_key_next_to_a_real_unnamed_one(tmp_path) -> None:
    result = kit.convert_bytes(tmp_path, "x.json", '[{"a": [{"": 2, "unnamed": 1}]}]')
    assert result.rows["a"] == [[{"unnamed_2": 2, "unnamed": 1}]]


def test_repeated_identical_keys_keep_the_first_on_every_path(tmp_path) -> None:
    # DuckDB keeps the first of {"k": 1, "k": 9}; the rename pass must not switch to Python's
    # last-wins, or the value would depend on whether some other key collided.
    plain = kit.convert_bytes(tmp_path, "x.json", '[{"a": {"k": 1, "k": 9}}]')
    renamed = kit.convert_bytes(tmp_path, "y.json", '[{"a": {"k": 1, "k": 9, "K": 2}}]')
    assert plain.rows["a"] == [{"k": 1}]
    assert renamed.rows["a"] == [{"k": 1, "K_2": 2}]


def test_many_renames_are_summarised(tmp_path) -> None:
    record = {"a": {f"k{i}": i for i in range(8)} | {f"K{i}": -i for i in range(8)}}
    result = kit.convert_bytes(tmp_path, "x.json", json.dumps([record]))
    (warning,) = _renamed(result)
    assert warning.startswith("renamed 8 nested keys")
    assert "and 3 more" in warning


def test_a_collision_that_survives_renaming_fails_closed(tmp_path, monkeypatch) -> None:
    # If DuckDB ever folds a character we do not, refuse (unpaid) rather than drop values.
    monkeypatch.setattr(jsonish, "_fold", lambda key: key)
    with pytest.raises(InvalidInput, match="could not be kept apart") as info:
        kit.convert_bytes(tmp_path, "x.json", json.dumps(PAIRS))
    assert info.value.hint is not None
    assert "letter case" in info.value.hint


# Pure Python: a document this deep must never reach DuckDB in-process (it crashes at ~1,500
# levels; job isolation covers that, see test_isolate.py). Python's own limit is ~1,000.
_DEEP = '{"a": ' * 5000 + "1" + "}" * 5000


def test_too_deep_to_check_is_a_clean_error() -> None:
    with pytest.raises(InvalidInput, match="nested too deeply"):
        jsonish._structure_collides(_DEEP)


def test_too_deep_to_rename_is_a_clean_error() -> None:
    with pytest.raises(InvalidInput, match="nested too deeply"):
        jsonish._renamed_record(_DEEP, jsonish._KeyRenamer())


@pytest.mark.parametrize(
    ("structure", "collides"),
    [
        ('{"id": "UBIGINT", "ID": "UBIGINT"}', False),  # columns: DuckDB keeps them apart
        ('{"a": {"k": "UBIGINT", "K": "UBIGINT"}}', True),
        ('{"a": [{"x": [{"k": "UBIGINT", "K": "UBIGINT"}]}]}', True),
        ('[{"k": "UBIGINT", "K": "UBIGINT"}]', True),  # records that are lists: all nested
        ('{"a": {"\u00e9": "UBIGINT", "\u00c9": "UBIGINT"}}', False),
        ('{"a": "JSON"}', False),  # mixed types: DuckDB keeps the JSON text, nothing is lost
        ('"JSON"', False),
    ],
)
def test_structure_check(structure: str, collides: bool) -> None:
    assert jsonish._structure_collides(structure) is collides
