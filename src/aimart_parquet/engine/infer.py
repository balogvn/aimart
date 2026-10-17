"""Infer real types for text columns, entirely inside DuckDB.

Each text column climbs a ladder — bool, int, float (plain, US ``1,234.5``, EU ``1.234,5``,
spaced ``1 234,5``, with an optional currency symbol), timestamptz, timestamp/date (ISO,
day-first, month-first) — and takes the first rung that every non-null value fits. With 20+
values, a rung that fits at least 98% wins anyway: the misfits become nulls, and the report
counts them with examples, so nothing is lost silently.

Correctness beats cleverness on purpose:

* Integers are matched by regex, never ``TRY_CAST`` — DuckDB happily casts '12.7' to BIGINT 13.
* Leading zeros (``00123``) and 16+ digit numbers stay text: they are IDs, zip codes and
  account numbers, and a float would corrupt them.
* Null tokens such as ``NA`` are only applied to columns that become typed. In a text column
  ``NA`` may be Namibia, so text keeps it unless the caller lists it in ``null_values``.
* When ``03/04/2025`` parses both ways and no value disambiguates, ``date_order`` decides and
  the report says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aimart_parquet.engine.duck import ident, literal
from aimart_parquet.engine.models import DateOrder

if TYPE_CHECKING:
    import duckdb

DEFAULT_NULL_TOKENS: tuple[str, ...] = (
    "",
    "na",
    "n/a",
    "n.a.",
    "nan",
    "null",
    "none",
    "nil",
    "-",
    "--",
    "?",
    "#n/a",
    "#null!",
    "#value!",
    "#div/0!",
    "missing",
)
TRUE_TOKENS = ("true", "t", "yes", "y")
FALSE_TOKENS = ("false", "f", "no", "n")

MIN_FIT = 0.98
MIN_ROWS_FOR_TOLERANCE = 20
_BATCH = 40

_CURRENCY = "[$€£¥₦]"
_RE_INT = r"[+-]?\d+"
_RE_PLAIN = r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?"
_RE_US = r"[+-]?\d{1,3}(,\d{3})+(\.\d+)?"
_RE_EU = r"[+-]?\d{1,3}(\.\d{3})+(,\d+)?|[+-]?\d+,\d+"
_RE_SPACE = r"[+-]?\d{1,3}([ \x{00A0}\x{202F}]\d{3})+([.,]\d+)?"
_RE_LEADING_ZERO = r"[+-]?0\d+"
_RE_DATE_LIKE = (
    r"\d{1,4}[-/. ]\d{1,2}[-/. ]\d{1,4}.*|.*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec).*"
)
_RE_TZ_SUFFIX = r".*(Z|[+-]\d{2}:?\d{2})$"

_ISO = ["%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"]
_DMY = ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%b-%Y", "%d %b %Y", "%d %B %Y"]
_MDY = ["%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%b %d %Y", "%b %d, %Y", "%B %d, %Y", "%B %d %Y"]
_TIMES = ["", " %H:%M", " %H:%M:%S", "T%H:%M:%S", " %I:%M %p", " %I:%M:%S %p"]


def _with_times(formats: list[str]) -> list[str]:
    return [f + t for f in formats for t in _TIMES]


def _fmt_list(formats: list[str]) -> str:
    return "[" + ", ".join(literal(f) for f in formats) + "]"


@dataclass
class ColumnPlan:
    source: str
    expr: str
    type_name: str
    inferred: bool = False
    rejected_count: int = 0
    rejected_examples: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Exprs:
    """SQL fragments for one column, derived from its raw text."""

    v: str  # trimmed text with null tokens removed (used for typed rungs)
    n: str  # v without a single currency symbol (numeric rungs)

    @classmethod
    def build(cls, col: str, tokens: tuple[str, ...]) -> _Exprs:
        token_list = ", ".join(literal(t) for t in tokens)
        v = f"(CASE WHEN lower(trim({col})) IN ({token_list}) THEN NULL ELSE trim({col}) END)"
        n = (
            f"regexp_replace(regexp_replace({v}, '^([+-]?)\\s*{_CURRENCY}\\s*', '\\1'), "
            f"'\\s*{_CURRENCY}$', '')"
        )
        return cls(v, n)


def _match(expr: str, pattern: str) -> str:
    return f"regexp_full_match({expr}, {literal(pattern)})"


def _numeric_conversion(n: str, style: str) -> str:
    if style == "us":
        return f"replace({n}, ',', '')"
    if style == "eu":
        return f"replace(replace({n}, '.', ''), ',', '.')"
    if style == "space":
        return f"replace(regexp_replace({n}, '[ \\x{{00A0}}\\x{{202F}}]', '', 'g'), ',', '.')"
    return n


def _date_formats(order: DateOrder) -> tuple[list[str], list[str]]:
    iso = _with_times(_ISO)
    return iso + _with_times(_DMY), iso + _with_times(_MDY)


def _parse_ts(v: str, formats: list[str]) -> str:
    return f"coalesce(TRY_CAST({v} AS TIMESTAMP), try_strptime({v}, {_fmt_list(formats)}))"


class TypeInferrer:
    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        table: str,
        *,
        extra_null_tokens: list[str],
        date_order: DateOrder,
        prefer_decimal_comma: bool,
    ) -> None:
        self.con = con
        self.table = ident(table)
        self.extra = tuple(t.strip().lower() for t in extra_null_tokens)
        self.tokens = tuple(dict.fromkeys(DEFAULT_NULL_TOKENS + self.extra))
        self.date_order = date_order
        self.prefer_decimal_comma = prefer_decimal_comma
        self.dmy_formats, self.mdy_formats = _date_formats(date_order)

    # ------------------------------------------------------------------ public
    def text_plan(self, source: str) -> ColumnPlan:
        """Keep text, trimming whitespace; only '' and caller-listed tokens become null."""
        col = ident(source)
        tokens = ("", *self.extra)
        token_list = ", ".join(literal(t) for t in dict.fromkeys(tokens))
        expr = f"(CASE WHEN lower(trim({col})) IN ({token_list}) THEN NULL ELSE trim({col}) END)"
        return ColumnPlan(source=source, expr=expr, type_name="string")

    def plan(self, sources: list[str]) -> dict[str, ColumnPlan]:
        plans: dict[str, ColumnPlan] = {}
        for start in range(0, len(sources), _BATCH):
            batch = sources[start : start + _BATCH]
            plans.update(self._plan_numeric_batch(batch))
        undecided = [s for s in sources if plans[s].type_name == "string" and plans[s].inferred]
        for start in range(0, len(undecided), _BATCH):
            plans.update(self._plan_temporal_batch(undecided[start : start + _BATCH]))
        for plan in plans.values():
            plan.inferred = plan.type_name != "string"
            if plan.rejected_count:
                plan.rejected_examples = self._examples(plan)
        return plans

    # ------------------------------------------------------------------ rungs
    def _plan_numeric_batch(self, sources: list[str]) -> dict[str, ColumnPlan]:
        aggs: list[str] = []
        for s in sources:
            e = _Exprs.build(ident(s), self.tokens)
            m_int, m_plain = _match(e.n, _RE_INT), _match(e.n, _RE_PLAIN)
            aggs += [
                f"count({e.v})",
                f"count_if(lower({e.v}) IN ({', '.join(map(literal, TRUE_TOKENS + FALSE_TOKENS))}))",
                f"count_if({_match(e.n, _RE_LEADING_ZERO)})",
                f"count_if({m_int} AND length(regexp_replace({e.n}, '[^0-9]', '', 'g')) > 15)",
                f"count_if({m_int})",
                f"count_if({m_plain})",
                f"count_if({_match(e.n, _RE_US)} OR {m_plain})",
                f"count_if({_match(e.n, _RE_EU)} OR {m_int})",
                f"count_if({_match(e.n, _RE_SPACE)} OR {m_int})",
                f"count_if({e.v} != {e.n})",
                f"count_if({_match(f'lower({e.v})', _RE_DATE_LIKE)})",
            ]
        row = self.con.sql(f"SELECT {', '.join(aggs)} FROM {self.table}").fetchone()
        assert row is not None
        width = 11
        return {
            s: self._decide_numeric(s, row[i * width : (i + 1) * width])
            for i, s in enumerate(sources)
        }

    def _decide_numeric(self, source: str, stats: tuple[int, ...]) -> ColumnPlan:
        n, n_bool, n_lead0, n_long, n_int, n_plain, n_us, n_eu, n_space, n_currency, n_date = stats
        e = _Exprs.build(ident(source), self.tokens)
        text = self.text_plan(source)
        if n == 0:
            text.notes.append("all values empty")
            return text
        if n_bool == n:
            true_list = ", ".join(map(literal, TRUE_TOKENS))
            false_list = ", ".join(map(literal, FALSE_TOKENS))
            expr = (
                f"(CASE WHEN lower({e.v}) IN ({true_list}) THEN true "
                f"WHEN lower({e.v}) IN ({false_list}) THEN false END)"
            )
            return ColumnPlan(source, expr, "bool")
        if n_lead0 or n_long:
            text.notes.append("kept as text: leading zeros or 16+ digit identifiers")
            return text

        tolerant = n >= MIN_ROWS_FOR_TOLERANCE
        candidates: list[tuple[str, int]] = [
            ("plain", n_plain),
            ("us", n_us),
            ("eu", n_eu),
            ("space", n_space),
        ]
        full = [style for style, fit in candidates if fit == n]
        style: str | None = None
        if "plain" in full:
            style = "plain"
        elif full and "eu" in full and "us" in full:
            style = (
                "eu" if self.prefer_decimal_comma else "us"
            )  # '1,234' fits both: dialect decides
        elif full:
            style = full[0]
        elif tolerant:
            best_style, best_fit = max(candidates, key=lambda c: c[1])
            if best_fit >= MIN_FIT * n:
                style = best_style
        if style is None:
            # Only spend the (expensive) date parsing on columns that look like dates.
            text.inferred = n_date == n or (tolerant and n_date >= MIN_FIT * n)
            return text

        converted = _numeric_conversion(e.n, style)
        if style == "plain":
            # Integers only when no fitting value has a fractional part: nulling the 1% of
            # decimals in an otherwise-integer column would destroy real data.
            is_int = n_int == n_plain
        else:
            fit, fit_int = self._fit_counts(source, style, converted)
            is_int = fit_int == fit
        sql_type, type_name = ("BIGINT", "int64") if is_int else ("DOUBLE", "float64")
        expr = f"TRY_CAST({converted} AS {sql_type})"
        if is_int:
            # Regex-gate the cast so '12.7' becomes a counted reject, not a silent 13.
            expr = f"(CASE WHEN {_match(converted, _RE_INT)} THEN {expr} END)"
        plan = ColumnPlan(source, expr, type_name)
        if style in {"us", "eu", "space"}:
            plan.notes.append(f"parsed {style}-style number formatting")
        if n_currency:
            plan.notes.append("stripped currency symbols")
        plan.rejected_count = self._reject_count(source, expr)
        return plan

    def _fit_counts(self, source: str, style: str, converted: str) -> tuple[int, int]:
        """(values fitting the style, of which integral after conversion)."""
        e = _Exprs.build(ident(source), self.tokens)
        pattern = {"us": _RE_US, "eu": _RE_EU, "space": _RE_SPACE}[style]
        allowed = _RE_PLAIN if style == "us" else _RE_INT
        fit = f"({_match(e.n, pattern)} OR {_match(e.n, allowed)})"
        row = self.con.sql(
            f"SELECT count_if({fit}), count_if({fit} AND {_match(converted, _RE_INT)}) "
            f"FROM {self.table}"
        ).fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    def _plan_temporal_batch(self, sources: list[str]) -> dict[str, ColumnPlan]:
        aggs: list[str] = []
        for s in sources:
            v = _Exprs.build(ident(s), self.tokens).v
            dmy, mdy = _parse_ts(v, self.dmy_formats), _parse_ts(v, self.mdy_formats)
            aggs += [
                f"count({v})",
                f"count_if({_match(v, _RE_TZ_SUFFIX)} AND TRY_CAST({v} AS TIMESTAMPTZ) IS NOT NULL)",
                f"count({dmy})",
                f"count({mdy})",
                f"count_if({dmy} IS DISTINCT FROM {mdy} AND {dmy} IS NOT NULL AND {mdy} IS NOT NULL)",
                f"count_if(coalesce({dmy}, {mdy})::TIME != TIME '00:00:00')",
            ]
        row = self.con.sql(f"SELECT {', '.join(aggs)} FROM {self.table}").fetchone()
        assert row is not None
        width = 6
        return {
            s: self._decide_temporal(s, row[i * width : (i + 1) * width])
            for i, s in enumerate(sources)
        }

    def _decide_temporal(self, source: str, stats: tuple[int, ...]) -> ColumnPlan:
        n, n_tz, n_dmy, n_mdy, n_conflict, n_time = stats
        v = _Exprs.build(ident(source), self.tokens).v
        text = self.text_plan(source)
        tolerant = n >= MIN_ROWS_FOR_TOLERANCE
        if n and n_tz == n:
            expr = f"TRY_CAST({v} AS TIMESTAMPTZ)"
            return ColumnPlan(source, expr, "timestamptz")

        def fits(count: int) -> bool:
            return count == n or (tolerant and count >= MIN_FIT * n)

        notes: list[str] = []
        if self.date_order is DateOrder.MDY:
            order = "mdy" if fits(n_mdy) else None
        elif self.date_order in {DateOrder.DMY, DateOrder.YMD}:
            order = "dmy" if fits(n_dmy) else None
        elif fits(n_dmy) and fits(n_mdy):
            order = "dmy" if n_dmy >= n_mdy else "mdy"
            if n_conflict and n_dmy == n_mdy:
                notes.append(
                    "ambiguous day/month order; assumed day-first (set date_order='mdy' to flip)"
                )
        elif fits(n_dmy):
            order = "dmy"
        elif fits(n_mdy):
            order = "mdy"
        else:
            order = None
        if order is None:
            return text
        formats = self.dmy_formats if order == "dmy" else self.mdy_formats
        parsed = _parse_ts(v, formats)
        if n_time:
            expr, type_name = parsed, "timestamp"
        else:
            expr, type_name = f"CAST({parsed} AS DATE)", "date"
        plan = ColumnPlan(source, expr, type_name, notes=notes)
        plan.rejected_count = self._reject_count(source, expr)
        return plan

    def cast_text(self, source: str, type_name: str, sql_type: str) -> str:
        """Best-effort cast of a text column to a caller-mandated type (expected_schema)."""
        e = _Exprs.build(ident(source), self.tokens)
        style = "eu" if self.prefer_decimal_comma else "us"
        converted = _numeric_conversion(e.n, style)
        if type_name == "int64":
            return (
                f"(CASE WHEN {_match(e.n, _RE_INT)} THEN TRY_CAST({e.n} AS BIGINT) "
                f"WHEN {_match(converted, _RE_INT)} THEN TRY_CAST({converted} AS BIGINT) END)"
            )
        if type_name in {"float64", "decimal"}:
            return (
                f"coalesce(CASE WHEN {_match(e.n, _RE_PLAIN)} THEN TRY_CAST({e.n} AS {sql_type}) END, "
                f"TRY_CAST({converted} AS {sql_type}))"
            )
        if type_name == "bool":
            true_list = ", ".join(map(literal, (*TRUE_TOKENS, "1")))
            false_list = ", ".join(map(literal, (*FALSE_TOKENS, "0")))
            return (
                f"(CASE WHEN lower({e.v}) IN ({true_list}) THEN true "
                f"WHEN lower({e.v}) IN ({false_list}) THEN false END)"
            )
        if type_name in {"date", "timestamp"}:
            formats = self.mdy_formats if self.date_order is DateOrder.MDY else self.dmy_formats
            parsed = _parse_ts(e.v, formats)
            return f"CAST({parsed} AS DATE)" if type_name == "date" else parsed
        if type_name == "timestamptz":
            return f"TRY_CAST({e.v} AS TIMESTAMPTZ)"
        return f"TRY_CAST({e.v} AS {sql_type})"

    # ------------------------------------------------------------------ helpers
    def _reject_count(self, source: str, expr: str) -> int:
        v = _Exprs.build(ident(source), self.tokens).v
        row = self.con.sql(
            f"SELECT count_if({v} IS NOT NULL AND ({expr}) IS NULL) FROM {self.table}"
        ).fetchone()
        return int(row[0]) if row else 0

    def _examples(self, plan: ColumnPlan) -> list[str]:
        v = _Exprs.build(ident(plan.source), self.tokens).v
        rows = self.con.sql(
            f"SELECT DISTINCT {v} AS x FROM {self.table} "
            f"WHERE {v} IS NOT NULL AND ({plan.expr}) IS NULL ORDER BY x LIMIT 3"
        ).fetchall()
        return [str(r[0])[:80] for r in rows]
