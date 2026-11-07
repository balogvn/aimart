"""The file's other number columns override the dialect's decimal convention only on proof.

``TypeInferrer._decimal_comma`` decides how a value either convention reads ('43.445',
'"238,308"') is read when its column cannot decide. 740ddae let one-sided evidence from the
file's other number columns override the dialect: a '398.53' price, which only a decimal point
writes, made a semicolon file's '16.469' kg a decimal (07fa450, by the semicolon: 16469).
Two reviews of the fixes found the same rule reading counts 1000x off, in a 200 with only an
"assumed" note, where 07fa450 read them right:

- c5237d3 took the decimal-point vote of every H.MM-looking value ('0.25', '4.50', '$12.50')
  in every file. In a comma file, or JSON, those fractions, rates and prices had made a file
  with one comma-only column mixed: without them a survey multi-select '"2,4"' was one-sided
  evidence of a decimal comma, and an income '"238,308"' came back 238.308 (a weight '26.808'
  kg 26808). The same quoted pair alone did it too.
- What is written with a decimal point for reasons that have nothing to do with the file's
  thousands separator outvoted a semicolon file's dot thousands: GPS coordinates
  ('48.154041'), day.month dates ('21.02'), H.MM times beside GPS, versions ('2.23') and
  factors ('0.95'), each making downloads '43.445' 43.445 (07fa450: 43445).

No rule tells a '398.53' price from a '2.23' version, a '6.48' time or a '0.95' factor, nor a
'"12,50"' price from a '"2,4"' or '"10,15"' choice. So the other columns override the dialect
only when their evidence is unanimous (no column reads the dialect's way) and strong: values
written with a thousands group and a decimal separator ('1,234.56', '1.234,56'), which only one
convention writes and nothing else (a time, a date, a version, a list) looks like. Otherwise
the dialect decides, as it did for every file at 07fa450: the '398.53' rule is reverted, and
its file reads '16.469' as 16469 again, with the note that says so.
"""

from __future__ import annotations

import json
import random
from typing import TYPE_CHECKING, Any

import pyarrow.parquet as pq
import pytest

import aimart_testkit as kit
from aimart_parquet.engine.convert import convert_file
from aimart_parquet.engine.limits import Limits
from aimart_parquet.engine.models import ConvertOptions

if TYPE_CHECKING:
    from pathlib import Path

N = 60


def _convert(tmp_path: Path, name: str, text: str) -> dict[str, list[Any]]:
    src = tmp_path / name
    src.write_text(text, encoding="utf-8")
    out = tmp_path / f"{name}.parquet"
    convert_file(
        src, out, filename=name, options=ConvertOptions(), workdir=tmp_path, limits=Limits()
    )
    return pq.read_table(out).to_pydict()


def _us(n: int) -> str:
    return f"{n:,}"


def _eu(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _survey(rnd: random.Random, *, fractions: bool) -> tuple[str, list[int]]:
    """A US survey export: quoted multi-select choices, a share 0.10-0.50, quoted incomes."""
    lines = [
        "response_id,q1_channels" + (",share_online" if fractions else "") + ",household_income"
    ]
    incomes = []
    for i in range(N):
        income = rnd.randrange(20_000, 250_000)
        incomes.append(income)
        choice = rnd.choice(["1", "2", "3", '"1,3"', '"2,4"', '"1,2"'])
        share = rnd.choice(["0.25", "0.33", "0.50", "0.20", "0.40", "0.10"])
        lines.append(f"R{i},{choice}" + (f",{share}" if fractions else "") + f',"{_us(income)}"')
    return "\n".join(lines) + "\n", incomes


def test_fractions_in_a_comma_file_make_the_pairs_beside_them_no_decimal_comma(
    tmp_path: Path,
) -> None:
    text, incomes = _survey(random.Random(3), fractions=True)
    columns = _convert(tmp_path, "survey.csv", text)
    assert columns["household_income"] == incomes  # before: 219.641 for 219641


def test_quoted_pairs_alone_are_no_decimal_comma_in_a_comma_file(tmp_path: Path) -> None:
    text, incomes = _survey(random.Random(3), fractions=False)
    columns = _convert(tmp_path, "survey.csv", text)
    assert columns["household_income"] == incomes  # before: 219.641 (07fa450: 219641)


def test_rates_scores_and_ratings_leave_a_comma_files_thousands_alone(tmp_path: Path) -> None:
    rnd = random.Random(7)
    lines = ["team,score,rate,rating,attendance"]
    attendance = []
    for i in range(40):
        a = rnd.randrange(1000, 99_999)
        attendance.append(a)
        lines.append(
            f'T{i},"{rnd.randrange(5)},{rnd.randrange(5)}",'
            f"{rnd.choice(['0.05', '0.10', '0.15', '0.20'])},"
            f'"{rnd.choice(["4,5", "3,5", "5", "4"])}","{_us(a)}"'
        )
    columns = _convert(tmp_path, "teams.csv", "\n".join(lines) + "\n")
    assert columns["attendance"] == attendance  # before: 43.445 for 43445


def test_real_decimals_in_a_comma_file_stay_decimals(tmp_path: Path) -> None:
    rnd = random.Random(42)
    lines = ["item,discount,options,weight_kg"]
    weights = []
    for i in range(N):
        w = f"{rnd.randrange(1, 60)}.{rnd.randrange(100, 999)}"
        weights.append(float(w))
        lines.append(
            f"I{i},{rnd.choice(['0.10', '0.15', '0.25', '0.50'])},"
            f"{rnd.choice(['1', '"1,3"', '"2,4"'])},{w}"
        )
    columns = _convert(tmp_path, "items.csv", "\n".join(lines) + "\n")
    assert columns["weight_kg"] == pytest.approx(weights)  # before: 26808 for 26.808


def test_ndjson_string_fields_follow_the_same_rule(tmp_path: Path) -> None:
    rnd = random.Random(42)
    incomes, lines = [], []
    for i in range(N):
        income = rnd.randrange(20_000, 250_000)
        incomes.append(income)
        share = rnd.choice(["0.10", "0.25", "0.33", "0.50"])
        choices = rnd.choice(["1", "2,4", "1,3"])
        lines.append(
            json.dumps({"id": i, "share": share, "choices": choices, "income": _us(income)})
        )
    columns = _convert(tmp_path, "rows.ndjson", "\n".join(lines) + "\n")
    assert columns["income"] == incomes  # before: 126.952 for 126952


def _gps(rnd: random.Random) -> str:
    return f"{rnd.uniform(47, 55):.6f};{rnd.uniform(6, 15):.6f}"


def test_coordinates_alone_do_not_override_a_semicolon_files_thousands(tmp_path: Path) -> None:
    rnd = random.Random(7)
    lines = ["Filiale;Breitengrad;Laengengrad;Besucher"]
    counts = [rnd.randrange(1000, 99_999) for _ in range(40)]
    lines += [f"Filiale {i};{_gps(rnd)};{_eu(c)}" for i, c in enumerate(counts)]
    columns = _convert(tmp_path, "filialen.csv", "\n".join(lines) + "\n")
    assert columns["besucher"] == counts  # before: 43.445 for 43445 (07fa450: 43445)
    assert all(47 < x < 55 for x in columns["breitengrad"])


def test_times_beside_coordinates_do_not_override_either(tmp_path: Path) -> None:
    rnd = random.Random(7)
    lines = ["dato;tid;lat;lon;antal"]
    counts = [rnd.randrange(1000, 99_999) for _ in range(40)]
    lines += [
        f"2025-01-{i % 28 + 1:02d};{rnd.randrange(6, 23)}.{rnd.randrange(60):02d};"
        f"{_gps(rnd)};{_eu(c)}"
        for i, c in enumerate(counts)
    ]
    columns = _convert(tmp_path, "salg.csv", "\n".join(lines) + "\n")
    assert columns["antal"] == counts  # before: 43.445 (07fa450: 43445)


def test_day_month_dates_are_no_decimal_point(tmp_path: Path) -> None:
    rnd = random.Random(7)
    lines = ["Tag;Anzahl"]
    counts = [rnd.randrange(1000, 99_999) for _ in range(40)]
    lines += [f"{rnd.randrange(1, 29):02d}.{rnd.randrange(1, 13):02d};{_eu(c)}" for c in counts]
    columns = _convert(tmp_path, "tage.csv", "\n".join(lines) + "\n")
    assert columns["anzahl"] == counts  # before: 43.445 (07fa450: 43445)


def _counts_beside(rnd: random.Random, header: str, values: list[str]) -> tuple[str, list[int]]:
    counts = [rnd.randrange(1000, 99_999) for _ in values]
    lines = [header] + [
        f"P{i};{v};{_eu(c)}" for i, (v, c) in enumerate(zip(values, counts, strict=True))
    ]
    return "\n".join(lines) + "\n", counts


@pytest.mark.parametrize(
    ("header", "pool"),
    [
        ("Paket;Version;Downloads", ["2.23", "3.5", "3.0", "3.18", "1.12", "2.7"]),
        ("Artikel;Faktor;Menge", ["1.25", "0.95", "0.75", "1.5", "2.80"]),
        ("Artikel;Preis;Menge", ["720.43", "368.33", "16.68", "402.00", "99.95"]),
    ],
    ids=["versions", "factors", "prices"],
)
def test_one_sided_points_are_no_proof_in_a_semicolon_file(
    header: str, pool: list[str], tmp_path: Path
) -> None:
    """Before: each made the counts '43.445' 43.445 (07fa450: 43445). A price writes the same
    as a version or a factor, so prices no longer override the semicolon either."""
    rnd = random.Random(7)
    text, counts = _counts_beside(rnd, header, [rnd.choice(pool) for _ in range(40)])
    columns = _convert(tmp_path, "pakete.csv", text)
    assert list(columns.values())[2] == counts


def test_the_reverted_price_rule_reads_as_07fa450_did(tmp_path: Path) -> None:
    """This asserted the 740ddae rule: '720.43' prices made the '32.616' kg 32.616. The same
    evidence made versions' downloads 1000x too small (above), and nothing tells the two apart,
    so the semicolon decides, as at 07fa450: 32616, with the note that says it assumed a
    decimal comma (infer_types=false keeps the text)."""
    rnd = random.Random(7)
    lines = ["Artikel;Preis;Gewicht"]
    weights = [f"{rnd.randrange(1, 99)}.{rnd.randrange(100, 999)}" for _ in range(40)]
    lines += [f"A{i};{rnd.uniform(10, 999):.2f};{w}" for i, w in enumerate(weights)]
    result = kit.convert_bytes(tmp_path, "artikel.csv", "\n".join(lines) + "\n")
    assert result.rows["gewicht"] == [int(w.replace(".", "")) for w in weights]
    note = next(n for n in result.column("gewicht").notes if n.startswith("ambiguous"))
    assert "assumed a decimal comma" in note


def test_strong_unanimous_evidence_still_overrides(tmp_path: Path) -> None:
    """'1,234.56' in a semicolon file, or '"1.234,56"' in a comma file: only one convention
    writes it, and no other column reads the dialect's way."""
    rnd = random.Random(5)
    amounts = [rnd.randrange(100_000, 9_999_999) / 100 for _ in range(30)]
    weights = [f"{rnd.randrange(1, 99)}.{rnd.randrange(100, 999)}" for _ in range(30)]
    lines = ["sku;amount;weight"] + [
        f"A{i};{a:,.2f};{w}" for i, (a, w) in enumerate(zip(amounts, weights, strict=True))
    ]
    columns = _convert(tmp_path, "us.csv", "\n".join(lines) + "\n")
    assert columns["amount"] == pytest.approx(amounts)
    assert columns["weight"] == pytest.approx([float(w) for w in weights])
    lines = ["sku,amount,weight"] + [
        f'A{i},"{_eu(int(a))},{round(a * 100) % 100:02d}",{w}'
        for i, (a, w) in enumerate(zip(amounts, weights, strict=True))
    ]
    columns = _convert(tmp_path, "eu.csv", "\n".join(lines) + "\n")
    assert columns["weight"] == [int(w.replace(".", "")) for w in weights]


def test_strong_evidence_against_a_dialect_column_is_mixed(tmp_path: Path) -> None:
    """A '12,5' column reads the semicolon's way: with it the file is mixed, and the
    semicolon decides even beside '1,234.56' amounts."""
    rnd = random.Random(5)
    lines = ["sku;amount;rate;weight"] + [
        f"A{i};{rnd.randrange(100_000, 9_999_999) / 100:,.2f};{rnd.randrange(1, 9)},5;"
        f"{rnd.randrange(1, 99)}.{rnd.randrange(100, 999)}"
        for i in range(30)
    ]
    columns = _convert(tmp_path, "mixed.csv", "\n".join(lines) + "\n")
    assert all(isinstance(w, int) and w >= 1000 for w in columns["weight"])


@pytest.mark.anyio
@pytest.mark.parametrize("shape", ["survey", "weights", "versions"])
async def test_a_paid_convert_returns_the_values_07fa450_returned(
    shape: str, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    """On the wire: the survey's incomes as integers, the items' weights as decimals, the
    packages' downloads as integers."""
    if shape == "survey":
        text, expected = _survey(random.Random(3), fractions=True)
        column, name = "household_income", "survey.csv"
    elif shape == "versions":
        rnd = random.Random(7)
        versions = [rnd.choice(["2.23", "3.5", "3.18", "1.12"]) for _ in range(N)]
        text, expected = _counts_beside(rnd, "Paket;Version;Downloads", versions)
        column, name = "downloads", "pakete.csv"
    else:
        rnd = random.Random(42)
        lines = ["item,discount,options,weight_kg"]
        expected = []
        for i in range(N):
            w = f"{rnd.randrange(1, 60)}.{rnd.randrange(100, 999)}"
            expected.append(float(w))
            lines.append(f"I{i},{rnd.choice(['0.10', '0.25'])},{rnd.choice(['1', '"1,3"'])},{w}")
        text, column, name = "\n".join(lines) + "\n", "weight_kg", "items.csv"
    body = {"content_base64": kit.b64(text.encode()), "filename": name}
    r = await paid_app.paying.post("/v1/convert", json=body)
    assert r.status_code == 200, r.text
    report = r.json()["report"]
    [col] = [c for c in report["columns"] if c["name"] == column]
    assert col["type"] == ("double" if shape == "weights" else "int64")
    preview = await paid_app.paying.post(
        "/v1/preview", json={"artifact_id": r.json()["artifact"]["id"], "limit": N}
    )
    assert preview.status_code == 200, preview.text
    payload = preview.json()
    index = payload["columns"].index(column)
    values = [row[index] for row in payload["rows"]]
    assert values == (pytest.approx(expected) if shape == "weights" else expected)
    assert len(facilitator.settled) == 2
