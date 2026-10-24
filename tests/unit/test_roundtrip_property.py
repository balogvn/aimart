"""Property: any small table written as CSV comes back from Parquet with the same values.

The generator only produces values whose type is unambiguous as text (ints without leading
zeros and under 16 digits, finite floats, ISO dates/timestamps, words starting with a letter
that no rung can claim), so the expected Parquet type is known for every column.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import math
import tempfile
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import aimart_testkit as kit

_INTS = st.integers(min_value=-(10**15) + 1, max_value=10**15 - 1)
_FLOATS = st.floats(allow_nan=False, allow_infinity=False, width=64).filter(
    lambda f: abs(f) < 1e300 and not f.is_integer()
)
_WORDS = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 éß北"),
    min_size=0,
    max_size=12,
).map(lambda s: "x" + s.strip())
_DATES = st.dates(min_value=dt.date(1900, 1, 1), max_value=dt.date(2100, 12, 31))
_STAMPS = st.datetimes(min_value=dt.datetime(1900, 1, 1), max_value=dt.datetime(2100, 12, 31)).map(
    lambda t: t.replace(microsecond=0, second=t.second or 1)
)  # never exactly midnight
_BOOLS = st.booleans()

_KINDS: dict[str, tuple[st.SearchStrategy[Any], str]] = {
    "int": (_INTS, "int64"),
    "float": (_FLOATS, "double"),
    "word": (_WORDS, "string"),
    "date": (_DATES, "date32[day]"),
    "stamp": (_STAMPS, "timestamp[us]"),
    "bool": (_BOOLS, "bool"),
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


@st.composite
def tables(draw: st.DrawFn) -> tuple[list[str], list[list[Any]]]:
    kinds = draw(st.lists(st.sampled_from(sorted(_KINDS)), min_size=1, max_size=5))
    n_rows = draw(st.integers(min_value=1, max_value=25))
    columns: list[list[Any]] = []
    for kind in kinds:
        strategy = _KINDS[kind][0]
        values = draw(st.lists(st.one_of(strategy, st.none()), min_size=n_rows, max_size=n_rows))
        if all(v is None for v in values):
            values[0] = draw(strategy)  # an all-empty column is text by design
        columns.append(values)
    return kinds, columns


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(tables())
def test_csv_round_trips_through_parquet(table: tuple[list[str], list[list[Any]]]) -> None:
    kinds, columns = table
    names = [f"c{i}" for i in range(len(kinds))]
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(names)
    writer.writerows([[_text(v) for v in row] for row in zip(*columns, strict=True)])

    with tempfile.TemporaryDirectory() as tmp:
        result = kit.convert_bytes(Path(tmp), "t.csv", buf.getvalue())
        rows = result.rows
        types = {c.name: c.type for c in result.report.columns}

    assert list(rows) == names
    for name, kind, expected in zip(names, kinds, columns, strict=True):
        assert types[name] == _KINDS[kind][1], (kind, expected)
        actual = rows[name]
        assert len(actual) == len(expected)
        for got, want in zip(actual, expected, strict=True):
            if want is None:
                assert got is None
            elif kind == "float":
                assert math.isclose(got, want, rel_tol=1e-15, abs_tol=0.0), (got, want)
            else:
                assert got == want, (kind, got, want)
    assert result.report.rows == len(columns[0])


_AMOUNTS = st.decimals(
    min_value=-(10**9), max_value=10**9, places=2, allow_nan=False, allow_infinity=False
)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(st.lists(_AMOUNTS, min_size=1, max_size=20), st.sampled_from(["us", "eu"]), st.booleans())
def test_grouped_and_currency_numbers_round_trip(amounts, style: str, currency: bool) -> None:
    def fmt(value) -> str:
        text = f"{value:,.2f}"
        if style == "eu":
            text = text.replace(",", "_").replace(".", ",").replace("_", ".")
        return f"€{text}" if currency else text

    delimiter = ";" if style == "eu" else ","
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    # Two columns: a lone column of "0,00" is genuinely ambiguous with a comma delimiter.
    writer.writerow(["id", "amount"])
    writer.writerows([[i, fmt(a)] for i, a in enumerate(amounts)])
    with tempfile.TemporaryDirectory() as tmp:
        result = kit.convert_bytes(Path(tmp), "money.csv", buf.getvalue())
        got = result.rows["amount"]
        column = result.column("amount")
    assert column.type == "double", (style, [fmt(a) for a in amounts], column.notes)
    assert column.rejected_count == 0
    assert got == [float(a) for a in amounts]


def _money(value: Any, style: str) -> str:
    text = f"{value:,.2f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".") if style == "eu" else text


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    st.lists(st.tuples(_AMOUNTS, st.sampled_from(["us", "eu"])), min_size=1, max_size=60),
    st.sampled_from(["us", "eu"]),
    st.sampled_from(["infer", "expected_schema"]),
)
def test_a_number_is_its_true_value_or_a_counted_reject(cells, dialect: str, mode: str) -> None:
    """Whatever the mix of conventions, no value is read with another value's separators.

    The 98% tolerance once read the misfits with the winner's separators ('2,50' -> 250.0 in a
    US column), and expected_schema casts did the same to every value of a text column.
    """
    if dialect == "us":
        # A lone mixed column still needs an unambiguous delimiter to be sniffed.
        cells = cells + [(a, "us") for a, _ in cells[:1]]
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";" if dialect == "eu" else ",", lineterminator="\n")
    writer.writerow(["id", "amount"])
    writer.writerows([[i, _money(a, style)] for i, (a, style) in enumerate(cells)])
    options: dict[str, Any] = {}
    if mode == "expected_schema":
        options = {
            "infer_types": False,
            "expected_schema": [
                {"name": "id", "type": "int64"},
                {"name": "amount", "type": "float64"},
            ],
        }
    with tempfile.TemporaryDirectory() as tmp:
        result = kit.convert_bytes(Path(tmp), "money.csv", buf.getvalue(), **options)
        got = result.rows["amount"]
        column = result.column("amount")
    if column.type == "string":
        return  # kept as text: nothing was read as a number
    for value, (amount, style) in zip(got, cells, strict=True):
        assert value is None or value == float(amount), (dialect, mode, _money(amount, style))
    assert column.rejected_count == sum(v is None for v in got)


def _number_text(value: Any, style: str) -> str:
    if style == "int":
        return str(int(value))
    if style == "plain":
        return f"{value:.2f}"
    if style == "plain3":  # '1.230': a decimal, or 1230 with a dot group
        return f"{value:.3f}"
    if style in {"us_int", "eu_int"}:  # '1,234' / '1.234': thousands, or decimals
        text = f"{int(value):,}"
        return text.replace(",", ".") if style == "eu_int" else text
    if style == "space":
        return f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return _money(value, style)


@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    st.lists(
        st.tuples(
            _AMOUNTS,
            st.sampled_from(["us", "eu", "plain", "plain3", "int", "us_int", "eu_int", "space"]),
        ),
        min_size=1,
        max_size=60,
    ),
    st.booleans(),
)
def test_an_expected_schema_cast_agrees_with_inference(cells, decimal_comma: bool) -> None:
    """A value must not depend on whether inference left its column as text.

    expected_schema casts of text columns once read each value on its own, dialect first, so
    one junk value (which keeps a column text) turned 1.234 into 1234 in a semicolon file.
    """
    from aimart_parquet.engine.duck import connect
    from aimart_parquet.engine.infer import TypeInferrer
    from aimart_parquet.engine.models import DateOrder

    con = connect(memory_limit="256MB", threads=1)
    con.execute("CREATE TABLE raw (c VARCHAR)")
    con.executemany("INSERT INTO raw VALUES (?)", [(_number_text(a, s),) for a, s in cells])
    inferrer = TypeInferrer(
        con,
        "raw",
        extra_null_tokens=[],
        date_order=DateOrder.AUTO,
        prefer_decimal_comma=decimal_comma,
    )
    plan = inferrer.plan(["c"])["c"]
    if plan.type_name not in {"int64", "float64"}:
        return
    sql_type = "BIGINT" if plan.type_name == "int64" else "DOUBLE"
    cast = inferrer.cast_text("c", plan.type_name, sql_type)
    rows = con.sql(f"SELECT c, {plan.expr}, {cast} FROM raw").fetchall()
    for text, inferred, cast_value in rows:
        if inferred is not None and cast_value is not None:
            assert inferred == cast_value, (text, inferred, cast_value, decimal_comma)
