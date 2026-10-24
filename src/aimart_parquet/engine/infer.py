"""Infer real types for text columns, entirely inside DuckDB.

Each text column climbs a ladder — bool, int, float (plain, US ``1,234.5``, EU ``1.234,5``,
spaced ``1 234,5``, with an optional currency symbol), timestamptz, timestamp/date (ISO,
day-first, month-first) — and takes the first rung that every non-null value fits. With 20+
values, a rung that fits at least 98% wins anyway: the misfits become nulls, and the report
counts them with examples, so nothing is lost silently.

Correctness beats cleverness on purpose:

* Every cast is gated on the rung's own pattern. A misfit is a counted null, never a value
  read with the winning rung's separators: '1.5' in an EU column is not 15.
* Integers are matched by regex, never ``TRY_CAST`` — DuckDB happily casts '12.7' to BIGINT 13.
* Leading zeros (``00123``) and 16+ digit numbers stay text: they are IDs, zip codes and
  account numbers, and a float would corrupt them.
* Null tokens such as ``NA`` are only applied to columns that become typed. In a text column
  ``NA`` may be Namibia, so text keeps it unless the caller lists it in ``null_values``.
* When ``03/04/2025`` parses both ways and no value disambiguates, ``date_order`` decides and
  the report says so. Likewise ``1,234`` and ``1.234`` (thousands or decimals?): the file's
  dialect decides (decimal comma in semicolon files) and the report says so.
* A UTC offset is never dropped. DuckDB's ``TIMESTAMP`` cast silently discards it, so text
  with an offset or zone is only ever read as ``TIMESTAMPTZ``. And only such text: that cast
  reads a naive value in the zone named by an earlier row ('10:00' after '09:00 EST' is EST).
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
# A thousands group never leads with 0: nobody writes 125 as '0,125' or '0.125', which are
# decimals in either convention.
_RE_US = r"[+-]?[1-9]\d{0,2}(,\d{3})+(\.\d+)?"
_RE_EU = r"[+-]?[1-9]\d{0,2}(\.\d{3})+(,\d+)?|[+-]?\d+,\d+"
_RE_SPACE = r"[+-]?[1-9]\d{0,2}([ \x{00A0}\x{202F}]\d{3})+([.,]\d+)?"
_RE_LEADING_ZERO = r"[+-]?0\d+"
_STYLES = ("plain", "us", "eu", "space")
_RE_DATE_LIKE = (
    r"\d{1,4}[-/. ]\d{1,2}[-/. ]\d{1,4}.*|.*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec).*"
)
# A time followed by an explicit offset. Zero offsets ('Z', '+00:00', 'UTC') are why this exists:
# _has_zone catches every other offset and zone name by comparing casts.
_RE_TZ_SUFFIX = r".*\d:\d{2}(:\d{2}([.,]\d+)?)?\s*(Z|[+-]\d{2}(:?\d{2}){0,2}|UTC)"

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


def _style_fit(n: str, style: str) -> str:
    """The values a number style reads. In a column of that style everything else is a reject."""
    if style == "plain":
        return _match(n, _RE_PLAIN)
    pattern, allowed = {
        "us": (_RE_US, _RE_PLAIN),
        "eu": (_RE_EU, _RE_INT),
        "space": (_RE_SPACE, _RE_INT),
    }[style]
    return f"({_match(n, pattern)} OR {_match(n, allowed)})"


def _ambiguous_separator(n: str) -> str:
    """'1,234' and '1.234': a thousands group under one convention, a decimal under the other."""
    return f"({_style_fit(n, 'us')} AND {_match(n, _RE_EU)})"


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


def _has_zone(v: str) -> str:
    """True for text carrying a UTC offset or zone name.

    DuckDB's TIMESTAMP cast drops an offset without applying it ('10:00+05:00' and
    '10:00-08:00' both become 10:00, 13 hours apart made equal), and returns NULL for zone
    names other than UTC; the TIMESTAMPTZ cast applies both.

    The TIMESTAMPTZ cast of text must never see a value without a zone in the same call as
    one with a zone name: a name such as 'EST' becomes the zone of every later naive value in
    its 2048-row vector (DuckDB 1.5.6), so '10:00' after '09:00 EST' reads as 15:00 UTC.
    Comparing it with the TIMESTAMP cast over the whole column took those naive values for
    zoned ones and shifted them, silently. So each CASE branch below casts a set that cannot
    leak: values the TIMESTAMP cast rejects (zone names, and junk that both casts reject; a
    leak changes which zone a value is read in, never whether it parses), or values it reads,
    whose only possible zone name is UTC, the session zone that duck.connect pins anyway.
    DuckDB runs a CASE branch only on the rows that take it.
    """
    naive = f"TRY_CAST({v} AS TIMESTAMP)"
    tz = f"TRY_CAST({v} AS TIMESTAMPTZ)"
    return (
        f"(CASE WHEN {naive} IS NULL THEN {tz} IS NOT NULL "
        f"ELSE {_match(v, _RE_TZ_SUFFIX)} OR {tz}::TIMESTAMP != {naive} END)"
    )


def _zoned_ts(v: str) -> str:
    """Text with a zone as an instant; NULL for anything else, which the cast would misread."""
    return f"(CASE WHEN {_has_zone(v)} THEN TRY_CAST({v} AS TIMESTAMPTZ) END)"


def naive_as_utc_note(naive: int, total: int) -> str:
    """Said whenever a timestamptz column holds values that carried no offset of their own."""
    return f"values without a UTC offset were read as UTC ({naive} of {total})"


def _parse_ts(v: str, formats: list[str]) -> str:
    """Naive timestamp text only: text with an offset parses to NULL here, never to a shifted time."""
    return (
        f"(CASE WHEN NOT {_has_zone(v)} THEN "
        f"coalesce(TRY_CAST({v} AS TIMESTAMP), try_strptime({v}, {_fmt_list(formats)})) END)"
    )


def _instant(v: str, formats: list[str]) -> str:
    """Text as an instant: a zone where it has one, UTC where it has none (say so in a note)."""
    return (
        f"(CASE WHEN {_has_zone(v)} THEN TRY_CAST({v} AS TIMESTAMPTZ) "
        f"ELSE timezone('UTC', {_parse_ts(v, formats)}) END)"
    )


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
            m_int = _match(e.n, _RE_INT)
            aggs += [
                f"count({e.v})",
                f"count_if(lower({e.v}) IN ({', '.join(map(literal, TRUE_TOKENS + FALSE_TOKENS))}))",
                f"count_if({_match(e.n, _RE_LEADING_ZERO)})",
                f"count_if({m_int} AND length(regexp_replace({e.n}, '[^0-9]', '', 'g')) > 15)",
                f"count_if({m_int})",
                *(f"count_if({_style_fit(e.n, style)})" for style in _STYLES),
                f"count_if({_ambiguous_separator(e.n)})",
                f"count_if({e.v} != {e.n})",
                f"count_if({_match(f'lower({e.v})', _RE_DATE_LIKE)})",
            ]
        row = self.con.sql(f"SELECT {', '.join(aggs)} FROM {self.table}").fetchone()
        assert row is not None
        width = 12
        return {
            s: self._decide_numeric(s, row[i * width : (i + 1) * width])
            for i, s in enumerate(sources)
        }

    def _decide_numeric(self, source: str, stats: tuple[int, ...]) -> ColumnPlan:
        n, n_bool, n_lead0, n_long, n_int, *style_fits, n_ambiguous, n_currency, n_date = stats
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

        def qualifies(count: int) -> bool:
            return count == n or (tolerant and count >= MIN_FIT * n)

        fits = dict(zip(_STYLES, style_fits, strict=True))
        top = max(fits.values())
        if not qualifies(top):
            # Only spend the (expensive) date parsing on columns that look like dates.
            text.inferred = qualifies(n_date)
            return text
        winners = [style for style, fit in fits.items() if fit == top]
        style = winners[0]  # plain, then us: a decimal point unless the dialect says otherwise
        if n_ambiguous and "eu" in winners and self.prefer_decimal_comma:
            # '1,234' and '1.234' fit both conventions; a semicolon file writes decimal commas.
            # Plain used to win outright, reading thousands '1.234' as 1.234 (1000x too small).
            style = "eu"
        rival = fits["us"] if style == "eu" else fits["eu"]
        ambiguous = bool(n_ambiguous) and style != "space" and qualifies(rival)

        fit = _style_fit(e.n, style)
        converted = _numeric_conversion(e.n, style)
        if style == "plain":
            # Integers only when no fitting value has a fractional part: nulling the 1% of
            # decimals in an otherwise-integer column would destroy real data.
            is_int = n_int == fits["plain"]
        else:
            fit_count, fit_int = self._fit_counts(fit, converted)
            is_int = fit_int == fit_count
        # Gate every cast on the style's own pattern. Under the 98% tolerance the misfits used
        # to be read with the winner's separators ('1.5' -> 15 in an EU column, '2,5' -> 25 in a
        # US one): wrong numbers, uncounted. They must be counted rejects instead.
        if is_int:
            # And regex-gate the integer so '12.7' becomes a counted reject, not a silent 13.
            cast, type_name = f"TRY_CAST({converted} AS BIGINT)", "int64"
            expr = f"(CASE WHEN {fit} AND {_match(converted, _RE_INT)} THEN {cast} END)"
        else:
            cast, type_name = f"TRY_CAST({converted} AS DOUBLE)", "float64"
            expr = f"(CASE WHEN {fit} THEN {cast} END)"
        plan = ColumnPlan(source, expr, type_name)
        if style in {"us", "eu", "space"}:
            plan.notes.append(f"parsed {style}-style number formatting")
        if ambiguous and (note := self._separator_note(source, style, expr)):
            plan.notes.append(note)
        if n_currency:
            plan.notes.append("stripped currency symbols")
        plan.rejected_count = self._reject_count(source, expr)
        return plan

    def _separator_note(
        self,
        source: str,
        style: str,
        expr: str,
        remedy: str = "infer_types=false keeps the original text",
    ) -> str | None:
        """Say which convention was assumed, with a value it read, so the reader can check.

        None when no value both conventions read made it into the column: nothing was assumed.
        """
        e = _Exprs.build(ident(source), self.tokens)
        row = self.con.sql(
            f"SELECT {e.v}, {expr} FROM {self.table} "
            f"WHERE {_ambiguous_separator(e.n)} AND ({expr}) IS NOT NULL ORDER BY 1 LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        mark = "comma" if style == "eu" else "point"
        return (
            f"ambiguous thousands/decimal separator; assumed a decimal {mark}, so "
            f"'{str(row[0])[:40]}' is {row[1]} ({remedy})"
        )

    def cast_note(self, source: str, type_name: str, expr: str) -> str | None:
        """What a cast_text result assumed that its values do not say, for the report.

        A per-value cast cannot see the column, so the file's dialect alone decided '1,234' and
        '1.234'; a naive target holds an offset only by converting to UTC, and a timestamptz
        target holds a naive value only by assuming UTC. Inference says so in a note; the
        expected_schema cast must too.
        """
        if type_name in {"int64", "float64", "decimal"}:
            style = "eu" if self.prefer_decimal_comma else "us"
            return self._separator_note(
                source, style, expr, "declare it as string in expected_schema to keep the text"
            )
        if type_name in {"date", "timestamp", "timestamptz"}:
            v = _Exprs.build(ident(source), self.tokens).v
            # The values the target type could not hold as written: offsets in a naive target,
            # naive values in a timestamptz one.
            assumed = _has_zone(v) if type_name != "timestamptz" else f"NOT {_has_zone(v)}"
            row = self.con.sql(
                f"SELECT count_if({assumed} AND ({expr}) IS NOT NULL), count({v}) FROM {self.table}"
            ).fetchone()
            if row and row[0]:
                if type_name == "timestamptz":
                    return naive_as_utc_note(int(row[0]), int(row[1]))
                return f"values with a UTC offset were converted to UTC ({row[0]} of {row[1]})"
        return None

    def _fit_counts(self, fit: str, converted: str) -> tuple[int, int]:
        """(values fitting the style, of which integral after conversion)."""
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
                f"count_if({_has_zone(v)})",
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
            return ColumnPlan(source, _zoned_ts(v), "timestamptz")
        # Offsets mixed with naive values (the naive parse below never sees an offset). Mostly
        # offsets: the column holds instants and the naive values are read as UTC, said in a
        # note. Mostly naive: an offset has no honest naive reading, so it is a counted reject.
        # It used to be dropped: '10:00+05:00' and '10:00-08:00' both became 10:00, rejected=0.
        instants = n_tz * 2 > n
        base = n_tz if instants else 0

        def fits(count: int) -> bool:
            total = base + count
            return total == n or (tolerant and total >= MIN_FIT * n)

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
        if instants:
            expr, type_name = _instant(v, formats), "timestamptz"
            naive = n_dmy if order == "dmy" else n_mdy
            if naive:
                notes.append(naive_as_utc_note(naive, n))
        elif n_time:
            expr, type_name = parsed, "timestamp"
        else:
            expr, type_name = f"CAST({parsed} AS DATE)", "date"
        if n_tz and not instants:
            notes.append(
                f"values with a UTC offset were rejected ({n_tz} of {n}): most values have none, "
                "and dropping the offset would shift the time"
            )
        plan = ColumnPlan(source, expr, type_name, notes=notes)
        plan.rejected_count = self._reject_count(source, expr)
        return plan

    def cast_text(self, source: str, type_name: str, sql_type: str) -> str:
        """Best-effort cast of a text column to a caller-mandated type (expected_schema)."""
        e = _Exprs.build(ident(source), self.tokens)
        style = "eu" if self.prefer_decimal_comma else "us"
        # Separators are rewritten only in values written in a convention: the dialect's
        # grouping (first, so '1.234' is 1234 in a decimal-comma file, as inference reads it),
        # plain numbers, space groups. Anything else is a reject. Rewriting every value made
        # '12,5' in a comma file 125, and '1,234.5' in a semicolon file 1.2345.
        readings = [
            (_match(e.n, _RE_EU if style == "eu" else _RE_US), _numeric_conversion(e.n, style)),
            (_match(e.n, _RE_PLAIN), e.n),
            (_match(e.n, _RE_SPACE), _numeric_conversion(e.n, "space")),
        ]
        if type_name == "int64":
            whens = " ".join(
                f"WHEN {fit} THEN (CASE WHEN {_match(c, _RE_INT)} THEN TRY_CAST({c} AS BIGINT) END)"
                for fit, c in readings
            )
            return f"(CASE {whens} END)"
        if type_name in {"float64", "decimal"}:
            whens = " ".join(f"WHEN {fit} THEN TRY_CAST({c} AS {sql_type})" for fit, c in readings)
            return f"(CASE {whens} END)"
        if type_name == "bool":
            true_list = ", ".join(map(literal, (*TRUE_TOKENS, "1")))
            false_list = ", ".join(map(literal, (*FALSE_TOKENS, "0")))
            return (
                f"(CASE WHEN lower({e.v}) IN ({true_list}) THEN true "
                f"WHEN lower({e.v}) IN ({false_list}) THEN false END)"
            )
        formats = self.mdy_formats if self.date_order is DateOrder.MDY else self.dmy_formats
        if type_name in {"date", "timestamp"}:
            # An offset is applied (UTC, as a timestamptz column cast to timestamp is), never
            # dropped by the TIMESTAMP cast.
            parsed = (
                f"(CASE WHEN {_has_zone(e.v)} THEN timezone('UTC', TRY_CAST({e.v} AS TIMESTAMPTZ)) "
                f"ELSE {_parse_ts(e.v, formats)} END)"
            )
            return f"CAST({parsed} AS DATE)" if type_name == "date" else parsed
        if type_name == "timestamptz":
            # Not TRY_CAST over the column: naive values after '09:00 EST' read as EST.
            return _instant(e.v, formats)
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
