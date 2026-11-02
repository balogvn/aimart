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
  the report says so. Likewise ``1,234`` and ``1.234`` (thousands or decimals?): when no value
  in the column settles it, the file's other number columns decide, then its dialect (decimal
  comma in semicolon files), and the report says so. The expected_schema cast of a text
  column decides the same way, for the whole column, so a value never depends on whether
  inference left its column as text.
* A decimal cast never rounds: a digit beyond the requested scale makes the value a reject.
* A UTC offset is never dropped. DuckDB's ``TIMESTAMP`` cast silently discards it, so text
  with an offset or zone is only ever read as ``TIMESTAMPTZ``. And only such text: that cast
  reads a naive value in the zone named by an earlier row ('10:00' after '09:00 EST' is EST).
* A zone is an offset, UTC, an IANA name or one of a few abbreviations with one meaning
  (``ZONE_ABBREVIATIONS``), never what ICU makes of a name: it reads 'BST' as Asia/Dhaka.
  Any other name makes the value a counted reject, or keeps the column text, with a note.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

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
# A time of day as Danish, Norwegian and Finnish files write it ('10.15', '9.05'): only a
# decimal point reads it as a number, so it voted for one in the file's convention.
_RE_CLOCK = r"([01]?\d|2[0-3])\.[0-5]\d"
_STYLES = ("plain", "us", "eu", "space")
_NUMERIC_TYPES = frozenset(
    {"tinyint", "smallint", "integer", "bigint", "hugeint", "float", "double", "decimal"}
    | {"utinyint", "usmallint", "uinteger", "ubigint", "uhugeint"}
)
NAIVE_TIME_TYPES = frozenset({"date", "timestamp", "timestamp_s", "timestamp_ms", "timestamp_ns"})
INSTANT_TYPE = "timestamp with time zone"
_RE_DATE_LIKE = (
    r"\d{1,4}[-/. ]\d{1,2}[-/. ]\d{1,4}.*|.*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec).*"
)
# A time followed by an explicit offset. Zero offsets ('Z', '+00:00', 'UTC') are why this exists:
# _has_zone catches every other offset and zone name by comparing casts.
_RE_TZ_SUFFIX = r".*\d:\d{2}(:\d{2}([.,]\d+)?)?\s*(Z|[+-]\d{2}(:?\d{2}){0,2}|UTC)"
# Zone abbreviations read as times, each as the one offset it names, all year. ICU never reads
# an abbreviation: it resolves them through legacy Java IDs and the rules of the zone it picks.
# BST was Asia/Dhaka (+6, not British Summer Time), AST Alaska (not Atlantic), ART Cairo, NST
# Auckland, ECT Paris, SST Guadalcanal, PST Los Angeles (-7 in July), MST Phoenix (-6 in July
# 1967): a UK log came back with every summer instant five hours early, a 200 with no note.
# Listed are only names with one meaning in data that ICU also reads as that one offset all
# year (a test checks). CST is also China (Java prints it for Asia/Shanghai), IST is India,
# Ireland and Israel, and ICU reads PST and CET with summer time: 'PST' in July is -7 to ICU
# and -8 by its name, and data is written both ways. Any other name is not a zone: the value
# is a counted reject, or the column stays text, and the report names it.
ZONE_ABBREVIATIONS = {
    "GMT": "+00:00",
    "EST": "-05:00",
    "MST": "-07:00",
    "HST": "-10:00",
    "JST": "+09:00",
}
# The zones a value may name after its time: an attached offset or 'Z' (DuckDB's own parser),
# a listed abbreviation, UTC with whole hours (ICU cut 'UTC+05:30' to +05:00), or an IANA
# name ICU reads with that zone's rules, as the name means: Area/City, or one of the US zones
# in POSIX form (EST5EDT is America/New_York's rules, summer time included).
_RE_KNOWN_ZONE = (
    r".*\d(Z|[+-]\d{2}(:?\d{2}){0,2}|\s+(?i:UTC([+-]\d{1,2}(:?00)?)?|"
    + "|".join(ZONE_ABBREVIATIONS)
    + r"|EST5EDT|CST6CDT|MST7MDT|PST8PDT|[a-z_]+(/[a-z0-9_+-]+)+))"
)

_ISO = ["%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"]
_DMY = ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%b-%Y", "%d %b %Y", "%d %B %Y"]
_MDY = ["%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%b %d %Y", "%b %d, %Y", "%B %d, %Y", "%B %d %Y"]
_TIMES = ["", " %H:%M", " %H:%M:%S", "T%H:%M:%S", " %I:%M %p", " %I:%M:%S %p"]


def _with_times(formats: list[str]) -> list[str]:
    return [f + t for f in formats for t in _TIMES]


def _fmt_list(formats: list[str]) -> str:
    return "[" + ", ".join(literal(f) for f in formats) + "]"


def _one_of(expr: str, values: tuple[str, ...]) -> str:
    """expr is one of the constants: never ``expr IN (...)`` in generated SQL.

    DuckDB 1.5.6 plans ``IN`` over five or more constants as a MARK hash join against the list.
    Each join held 6-12 MB however small the input, and the ladder repeats the null-token test
    several times per column, so a 100-row CSV with 30 date columns ran out of memory in the
    final COPY (a 500). The join also broke insertion order: once a file had more than one
    122,880-row group, its rows were written out of source order, and preview offset 0 showed
    row 122,881. A 40-column CSV's final SELECT held 90 of them, about 600 MB of hash tables
    and join buffers for a 13 MB file, over the 700 MB job cap whatever the row count (found
    profiling CSV memory, separately). ``list_contains`` is a plain function: no join, no
    reordering, and NULL for a NULL expr, as IN is.
    """
    return f"list_contains({_fmt_list(list(values))}, {expr})"


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
        v = f"(CASE WHEN {_one_of(f'lower(trim({col}))', tokens)} THEN NULL ELSE trim({col}) END)"
        n = (
            f"regexp_replace(regexp_replace({v}, '^([+-]?)\\s*{_CURRENCY}\\s*', '\\1'), "
            f"'\\s*{_CURRENCY}$', '')"
        )
        return cls(v, n)

    @classmethod
    def projected(
        cls, cols: list[str], tokens: tuple[str, ...], table: str
    ) -> tuple[str, list[_Exprs]]:
        """A FROM clause computing each column's v and n once, and the fragments naming them.

        For the statistics queries only (a plan's expression runs over the raw table). Inlined,
        v is planned again at every use, and the date rungs use it about 60 times a column:
        planning alone took seconds for 30 date columns of 100 rows.
        """
        exprs = [cls.build(col, tokens) for col in cols]
        select = ", ".join(
            f"{e.v} AS {ident(f'v{i}')}, {e.n} AS {ident(f'n{i}')}" for i, e in enumerate(exprs)
        )
        names = [cls(ident(f"v{i}"), ident(f"n{i}")) for i in range(len(cols))]
        return f"(SELECT {select} FROM {table})", names


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


def _unrounded_decimal(c: str, sql_type: str, scale: int) -> str:
    """Plain number text c as a decimal, or NULL where the cast would round it.

    DuckDB rounds to the scale without a word: '12.5' at scale 0 is 13, '12.345' at scale 2 is
    12.35. The int64 cast makes a fraction a counted reject, and with scale 0 honoured a
    decimal quietly did what an integer may not (strict passed and charged for 13). Text shows
    every digit, so one beyond the scale is visible; with an exponent, DOUBLE is the judge.
    """
    cast = f"TRY_CAST({c} AS {sql_type})"
    within = _match(c, rf"[+-]?(\d+\.?\d{{0,{scale}}}0*|\.\d{{0,{scale}}}0*)")
    exponent = f"(regexp_matches({c}, '[eE]') AND {cast} = TRY_CAST({c} AS DOUBLE))"
    return f"(CASE WHEN {within} OR {exponent} THEN {cast} END)"


def _date_formats(order: DateOrder) -> tuple[list[str], list[str]]:
    iso = _with_times(_ISO)
    return iso + _with_times(_DMY), iso + _with_times(_MDY)


def _tz(v: str) -> str:
    """Zoned text as an instant, with a listed abbreviation read as its offset, never by ICU."""
    for name, offset in ZONE_ABBREVIATIONS.items():
        v = f"regexp_replace({v}, {literal(rf'(\d)\s+(?i:{name})$')}, {literal(rf'\1{offset}')})"
    return f"TRY_CAST({v} AS TIMESTAMPTZ)"


def _has_zone(v: str) -> str:
    """True for text carrying a UTC offset or a zone name this module reads (_RE_KNOWN_ZONE).

    DuckDB's TIMESTAMP cast drops an offset without applying it ('10:00+05:00' and
    '10:00-08:00' both become 10:00, 13 hours apart made equal), and returns NULL for zone
    names other than UTC; the TIMESTAMPTZ cast applies both. A zone name it reads but this
    module does not ('09:30 BST', see ZONE_ABBREVIATIONS) has no zone here, and no naive
    reading either: the value is NULL in every rung.

    The TIMESTAMPTZ cast of text must never see a value without a zone in the same call as
    one with a zone name: a name such as 'Asia/Tokyo' becomes the zone of every later naive
    value in its 2048-row vector (DuckDB 1.5.6), so '10:00' after it reads as 01:00 UTC.
    Comparing it with the TIMESTAMP cast over the whole column took those naive values for
    zoned ones and shifted them, silently. So each CASE branch below casts a set that cannot
    leak: values the TIMESTAMP cast rejects (zone names, and junk that both casts reject; a
    leak changes which zone a value is read in, never whether it parses), or values it reads,
    whose only possible zone name is UTC, the session zone that duck.connect pins anyway.
    DuckDB runs a CASE branch only on the rows that take it.
    """
    naive = f"TRY_CAST({v} AS TIMESTAMP)"
    return (
        f"(CASE WHEN {naive} IS NULL THEN {_match(v, _RE_KNOWN_ZONE)} AND {_tz(v)} IS NOT NULL "
        f"ELSE {_match(v, _RE_TZ_SUFFIX)} OR TRY_CAST({v} AS TIMESTAMPTZ)::TIMESTAMP != {naive} END)"
    )


def _unread_zone(v: str) -> str:
    """The zone name ending text that ICU would read as an instant and this module does not."""
    return (
        f"(CASE WHEN TRY_CAST({v} AS TIMESTAMP) IS NULL AND NOT {_match(v, _RE_KNOWN_ZONE)} "
        f"AND TRY_CAST({v} AS TIMESTAMPTZ) IS NOT NULL THEN regexp_extract({v}, '\\S+$') END)"
    )


def _zoned_ts(v: str) -> str:
    """Text with a zone as an instant; NULL for anything else, which the cast would misread."""
    return f"(CASE WHEN {_has_zone(v)} THEN {_tz(v)} END)"


def unread_zone_note(name: str, count: int, total: int) -> str:
    """Said whenever values name a zone that has another meaning (ZONE_ABBREVIATIONS)."""
    return (
        f"zone '{name[:32]}' was not read as a zone ({count} of {total}): abbreviations such as "
        "BST, IST or CST name several zones, and DuckDB reads BST as Asia/Dhaka; write a UTC "
        "offset (+01:00) or a zone name (Europe/London)"
    )


def naive_as_utc_note(naive: int, total: int) -> str:
    """Said whenever a timestamptz column holds values that carried no offset of their own."""
    return f"values without a UTC offset were read as UTC ({naive} of {total})"


def offset_to_utc_note(converted: int, total: int) -> str:
    """Said whenever a naive date or timestamp column holds values that carried an offset."""
    return f"values with a UTC offset were converted to UTC ({converted} of {total})"


def _parse_ts(v: str, formats: list[str]) -> str:
    """Naive timestamp text only: text with an offset parses to NULL here, never to a shifted time."""
    return (
        f"(CASE WHEN NOT {_has_zone(v)} THEN "
        f"coalesce(TRY_CAST({v} AS TIMESTAMP), try_strptime({v}, {_fmt_list(formats)})) END)"
    )


def _instant(v: str, formats: list[str]) -> str:
    """Text as an instant: a zone where it has one, UTC where it has none (say so in a note)."""
    return (
        f"(CASE WHEN {_has_zone(v)} THEN {_tz(v)} "
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
        self._file_decimal_comma: bool | None = None
        self.dmy_formats, self.mdy_formats = _date_formats(date_order)

    # ------------------------------------------------------------------ public
    def text_plan(self, source: str) -> ColumnPlan:
        """Keep text, trimming whitespace; only '' and caller-listed tokens become null."""
        col = ident(source)
        tokens = tuple(dict.fromkeys(("", *self.extra)))
        expr = (
            f"(CASE WHEN {_one_of(f'lower(trim({col}))', tokens)} THEN NULL ELSE trim({col}) END)"
        )
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
        table, exprs = _Exprs.projected([ident(s) for s in sources], self.tokens, self.table)
        for e in exprs:
            m_int = _match(e.n, _RE_INT)
            aggs += [
                f"count({e.v})",
                f"count_if({_one_of(f'lower({e.v})', TRUE_TOKENS + FALSE_TOKENS)})",
                f"count_if({_match(e.n, _RE_LEADING_ZERO)})",
                f"count_if({m_int} AND length(regexp_replace({e.n}, '[^0-9]', '', 'g')) > 15)",
                f"count_if({m_int})",
                *(f"count_if({_style_fit(e.n, style)})" for style in _STYLES),
                f"count_if({_ambiguous_separator(e.n)})",
                f"count_if({e.v} != {e.n})",
                f"count_if({_match(f'lower({e.v})', _RE_DATE_LIKE)})",
            ]
        row = self.con.sql(f"SELECT {', '.join(aggs)} FROM {table}").fetchone()
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
            expr = (
                f"(CASE WHEN {_one_of(f'lower({e.v})', TRUE_TOKENS)} THEN true "
                f"WHEN {_one_of(f'lower({e.v})', FALSE_TOKENS)} THEN false END)"
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
        if n_ambiguous and "eu" in winners and self._decimal_comma():
            # '1,234' and '1.234' fit both conventions; the file's other numbers decide.
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

        '1,234' and '1.234' read either way: the column's other values or the dialect decided
        which; a naive target holds an offset only by converting to UTC, and a timestamptz
        target holds a naive value only by assuming UTC. Inference says so in a note; the
        expected_schema cast must too.
        """
        if type_name in {"int64", "float64", "decimal"}:
            return self._separator_note(
                source,
                self._text_style(source),
                expr,
                "declare it as string in expected_schema to keep the text",
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
                note = naive_as_utc_note if type_name == "timestamptz" else offset_to_utc_note
                return note(int(row[0]), int(row[1]))
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
        table, exprs = _Exprs.projected([ident(s) for s in sources], self.tokens, self.table)
        for v in (e.v for e in exprs):
            dmy, mdy = _parse_ts(v, self.dmy_formats), _parse_ts(v, self.mdy_formats)
            aggs += [
                f"count({v})",
                f"count_if({_has_zone(v)})",
                f"count({dmy})",
                f"count({mdy})",
                f"count_if({dmy} IS DISTINCT FROM {mdy} AND {dmy} IS NOT NULL AND {mdy} IS NOT NULL)",
                f"count_if(coalesce({dmy}, {mdy})::TIME != TIME '00:00:00')",
                f"count({_unread_zone(v)})",
                f"min({_unread_zone(v)})",
            ]
        row = self.con.sql(f"SELECT {', '.join(aggs)} FROM {table}").fetchone()
        assert row is not None
        width = 8
        return {
            s: self._decide_temporal(s, row[i * width : (i + 1) * width])
            for i, s in enumerate(sources)
        }

    def _decide_temporal(self, source: str, stats: tuple[Any, ...]) -> ColumnPlan:
        n, n_tz, n_dmy, n_mdy, n_conflict, n_time, n_unread = (int(x or 0) for x in stats[:7])
        unread = str(stats[7])
        v = _Exprs.build(ident(source), self.tokens).v
        text = self.text_plan(source)
        if n_unread:
            # Kept as text, or counted rejects: either way the report names the zone.
            text.notes.append(unread_zone_note(unread, n_unread, n))
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

        notes: list[str] = list(text.notes)
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

    def _text_style(self, source: str) -> str:
        """The number convention of a text column ('us' or 'eu'), decided for the whole column.

        Values only one convention reads are the evidence ('1.5' and '1,234.5' have a decimal
        point, '1,5' and '1.234,5' a decimal comma); the file's convention (``_decimal_comma``)
        breaks a tie, including the tie of no evidence at all, as it does in inference.
        Reading each value on its own, dialect first, made '1.234' 1234 in a semicolon file
        whose '1.5' and '2.75' prove dot decimals, while inference read the same column as
        1.234: the value depended on whether inference left it as text.
        """
        e = _Exprs.build(ident(source), self.tokens)
        point, comma = _style_fit(e.n, "us"), _style_fit(e.n, "eu")
        row = self.con.sql(
            f"SELECT count_if({point} AND NOT {comma}), count_if({comma} AND NOT {point}) "
            f"FROM {self.table}"
        ).fetchone()
        point_only, comma_only = (int(row[0]), int(row[1])) if row else (0, 0)
        if point_only != comma_only:
            return "us" if point_only > comma_only else "eu"
        return "eu" if self._decimal_comma() else "us"

    def _decimal_comma(self) -> bool:
        """The file's convention for values that either one reads ('1,234', '16.469').

        Decided once per file, before the delimiter: each number column (every value, or 98%,
        fits a convention) votes for the convention of its values that only one convention
        reads ('398.53', '0.250' and '1,234.5' have a decimal point, '12,5' and '1.234,5' a
        decimal comma; a time of day such as '10.15' is no evidence). The delimiter alone read
        every '16.469' kg in a semicolon file as 16469, beside a price of '398.53' in the same
        row that only a decimal point writes. Only one-sided evidence overrides the dialect:
        with votes for both, or none, the dialect decides, as it did for every file at
        07fa450. A majority vote let two GPS columns ('54.565860') outvote a turnover written
        '1.093.086,46' in the same row, and every visitor count '11.753' was read as 11.753.
        """
        if self._file_decimal_comma is None:
            texts = self.con.sql(f"SELECT * FROM {self.table} LIMIT 0")
            sources = [c for c, t in zip(texts.columns, texts.types, strict=True) if t == "VARCHAR"]
            point_votes = comma_votes = 0
            for start in range(0, len(sources), _BATCH):
                aggs: list[str] = []
                batch = [ident(s) for s in sources[start : start + _BATCH]]
                table, exprs = _Exprs.projected(batch, self.tokens, self.table)
                for e in exprs:
                    point, comma = _style_fit(e.n, "us"), _style_fit(e.n, "eu")
                    aggs += [
                        f"count({e.v})",
                        f"count_if({point} OR {comma})",
                        f"count_if({point} AND NOT {comma})",
                        f"count_if({comma} AND NOT {point})",
                        f"count_if({_match(e.n, _RE_CLOCK)})",
                    ]
                row = self.con.sql(f"SELECT {', '.join(aggs)} FROM {table}").fetchone()
                assert row is not None
                for i in range(0, len(aggs), 5):
                    n, fit, point_only, comma_only, clock = (int(x or 0) for x in row[i : i + 5])
                    # '10.15' is no evidence of a decimal point: a 'dato;tid;antal' file's times
                    # made every count '1.000' 1.0. Every clock value is point-only.
                    point_only -= clock
                    tolerant = n >= MIN_ROWS_FOR_TOLERANCE
                    if n and (fit == n or (tolerant and fit >= MIN_FIT * n)):
                        comma_votes += comma_only > point_only
                        point_votes += point_only > comma_only
            # Only one-sided evidence overrides the dialect. A file holding columns of both
            # conventions is mixed: GPS coordinates from another system ('54.565860') beside
            # amounts written '1.093.086,46' outvoted them, and visitors '11.753' became 11.753.
            if comma_votes and point_votes:
                self._file_decimal_comma = self.prefer_decimal_comma
            else:
                self._file_decimal_comma = bool(comma_votes) or (
                    self.prefer_decimal_comma and not point_votes
                )
        return self._file_decimal_comma

    def cast_text(self, source: str, type_name: str, sql_type: str) -> str:
        """Best-effort cast of a text column to a caller-mandated type (expected_schema)."""
        e = _Exprs.build(ident(source), self.tokens)
        if type_name in {"int64", "float64", "decimal"}:
            style = self._text_style(source)
            # Separators are rewritten only in values written in the column's convention, or in
            # space groups (which read the same in both). Anything else is a reject. Rewriting
            # every value made '12,5' in a comma file 125, and '1,234.5' in a semicolon file
            # 1.2345.
            readings = [
                (_style_fit(e.n, style), _numeric_conversion(e.n, style)),
                (_match(e.n, _RE_SPACE), _numeric_conversion(e.n, "space")),
            ]
            if type_name == "int64":
                whens = " ".join(
                    f"WHEN {fit} THEN "
                    f"(CASE WHEN {_match(c, _RE_INT)} THEN TRY_CAST({c} AS BIGINT) END)"
                    for fit, c in readings
                )
            elif type_name == "decimal":
                scale = self._decimal_scale(sql_type)
                whens = " ".join(
                    f"WHEN {fit} THEN {_unrounded_decimal(c, sql_type, scale)}"
                    for fit, c in readings
                )
            else:
                whens = " ".join(
                    f"WHEN {fit} THEN TRY_CAST({c} AS {sql_type})" for fit, c in readings
                )
            return f"(CASE {whens} END)"
        if type_name == "bool":
            return (
                f"(CASE WHEN {_one_of(f'lower({e.v})', (*TRUE_TOKENS, '1'))} THEN true "
                f"WHEN {_one_of(f'lower({e.v})', (*FALSE_TOKENS, '0'))} THEN false END)"
            )
        formats = self.mdy_formats if self.date_order is DateOrder.MDY else self.dmy_formats
        if type_name in {"date", "timestamp"}:
            # An offset is applied (UTC, as a timestamptz column cast to timestamp is), never
            # dropped by the TIMESTAMP cast.
            parsed = (
                f"(CASE WHEN {_has_zone(e.v)} THEN timezone('UTC', {_tz(e.v)}) "
                f"ELSE {_parse_ts(e.v, formats)} END)"
            )
            return f"CAST({parsed} AS DATE)" if type_name == "date" else parsed
        if type_name == "timestamptz":
            # Not TRY_CAST over the column: naive values after '09:00 EST' read as EST.
            return _instant(e.v, formats)
        return f"TRY_CAST({e.v} AS {sql_type})"

    # ------------------------------------------------------------------ helpers
    def type_id(self, expr: str) -> str:
        """The DuckDB type id of expr over this table (natives may be anything)."""
        return str(self.con.sql(f"SELECT {expr} FROM {self.table} LIMIT 0").types[0].id)

    def is_number(self, expr: str) -> bool:
        return self.type_id(expr) in _NUMERIC_TYPES

    def _decimal_scale(self, sql_type: str) -> int:
        children = dict(self.con.sql(f"SELECT NULL::{sql_type}").types[0].children)
        return cast("int", children["scale"])  # DECIMAL's children: precision and scale

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
