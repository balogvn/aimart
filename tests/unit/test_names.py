"""Column names an LLM can use unquoted: snake_case, unique, never empty."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aimart_parquet.engine.names import MAX_NAME_LENGTH, make_unique, snake_case


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Customer Name (USD)", "customer_name_usd"),
        ("orderID", "order_id"),
        ("HTTPServerError", "http_server_error"),
        ("  padded  ", "padded"),
        ("Signup-Date", "signup_date"),
        ("2024 revenue", "c_2024_revenue"),
        ("Crème Brûlée", "creme_brulee"),
        ("北京 人口", "北京_人口"),
        ("___x___", "x"),
        ("%%%", ""),
        ("a" * 100, "a" * MAX_NAME_LENGTH),
    ],
)
def test_snake_case_examples(raw: str, expected: str) -> None:
    assert snake_case(raw) == expected


def test_truncation_never_leaves_a_trailing_underscore() -> None:
    name = "a" * (MAX_NAME_LENGTH - 1) + " b"
    out = snake_case(name)
    assert not out.endswith("_")
    assert len(out) <= MAX_NAME_LENGTH


def test_make_unique_fills_blanks_and_numbers_duplicates() -> None:
    names = ["Name", "name", "", "NAME", "Total Amount", "  "]
    assert make_unique(names, normalize=True) == [
        "name",
        "name_2",
        "column_3",
        "name_3",
        "total_amount",
        "column_6",
    ]


def test_make_unique_without_normalization_keeps_text_but_dedupes_case_insensitively() -> None:
    assert make_unique(["Total", "total", " Total "], normalize=False) == [
        "Total",
        "total_2",
        "Total_3",
    ]


def test_generated_blank_name_cannot_collide_with_a_real_one() -> None:
    assert make_unique(["column_2", ""], normalize=True) == ["column_2", "column_2_2"]


_HEADERS = st.lists(st.text(max_size=80), max_size=40)


@settings(max_examples=300, deadline=None)
@given(_HEADERS, st.booleans())
def test_make_unique_properties(names: list[str], normalize: bool) -> None:
    out = make_unique(names, normalize=normalize)
    assert len(out) == len(names)
    assert all(out), "no empty column names"
    lowered = [n.lower() for n in out]
    assert len(set(lowered)) == len(lowered), "unique even case-insensitively"


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=120))
def test_snake_case_output_is_sql_friendly(raw: str) -> None:
    out = snake_case(raw)
    assert len(out) <= MAX_NAME_LENGTH
    assert out == out.strip("_")
    assert "__" not in out
    assert " " not in out
    if out:
        assert not out[0].isdigit()


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet=st.characters(codec="ascii"), max_size=60))
def test_snake_case_is_idempotent_on_ascii(raw: str) -> None:
    once = snake_case(raw)
    assert snake_case(once) == once
