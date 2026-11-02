"""The file's number convention: only one-sided evidence overrides the dialect.

Regression from 07fa450 (740ddae, the convention decided per file). ``_decimal_comma`` gave each
number column one vote, decimal point or decimal comma, and the majority decided how a value
either convention reads ('11.753') is read. In a German store list two GPS columns written as a
GPS export writes them ('54.565860', a decimal point) outvoted the turnover ('1.093.086,46': dot
thousands and a decimal comma in the same row), so every visitor count '11.753' came back
1000 times too small, 11.753, a 200 with only a note. 07fa450 read 11753, by the dialect.

A file with columns of both conventions is mixed: the dialect decides, as it did at 07fa450.
And a time of day written H.MM ('10.15', as Danish and Finnish files write it) is no decimal
point: the times of a 'dato;tid;antal' file were its only evidence, and its counts '1.000'
became 1.0 (1000 at 07fa450).
A semicolon file whose only evidence is '398.53' still reads dot decimals
(``test_final_data_decimals.py``).
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow.parquet as pq

from aimart_parquet.engine.convert import convert_file
from aimart_parquet.engine.limits import Limits
from aimart_parquet.engine.models import ConvertOptions

if TYPE_CHECKING:
    from collections.abc import Iterable

ROOT = Path(__file__).resolve().parents[2]


def _convert(tmp_path: Path, name: str, lines: list[str]) -> dict[str, list[object]]:
    src = tmp_path / name
    src.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = tmp_path / "o.parquet"
    convert_file(
        src, out, filename=name, options=ConvertOptions(), workdir=tmp_path, limits=Limits()
    )
    return pq.read_table(out).to_pydict()


def test_coordinates_do_not_outvote_the_files_thousands_separator(tmp_path: Path) -> None:
    rnd = random.Random(2)
    lines = ["Filiale;Breitengrad;Längengrad;Umsatz;Besucher"]
    visitors: list[int] = []
    for i in range(1, 41):
        v = rnd.randint(1, 40) * 1000 + rnd.randint(0, 999)
        visitors.append(v)
        lines.append(
            f"Filiale {i};{rnd.uniform(47.3, 54.9):.6f};{rnd.uniform(5.9, 15.0):.6f};"
            f"{rnd.randint(1, 9)}.{rnd.randint(0, 999):03d}.{rnd.randint(0, 999):03d},"
            f"{rnd.randint(0, 99):02d};{v // 1000}.{v % 1000:03d}"
        )
    columns = _convert(tmp_path, "filialen.csv", lines)
    assert columns["besucher"] == visitors  # before: each / 1000 (11.753 for 11753)
    assert all(isinstance(x, float) and 47 < x < 55 for x in columns["breitengrad"])
    assert all(isinstance(x, float) and x > 1_000_000 for x in columns["umsatz"])


def test_times_of_day_are_no_decimal_point(tmp_path: Path) -> None:
    lines = ["dato;tid;antal"]
    counts: list[int] = []
    for i in range(40):
        count = 1000 + i * 1007
        counts.append(count)
        lines.append(
            f"{1 + i % 28:02d}-02-2024;{10 + i % 12}.{(15 + i) % 60:02d};"
            f"{count // 1000}.{count % 1000:03d}"
        )
    columns = _convert(tmp_path, "salg.csv", lines)
    assert columns["antal"] == counts  # before: 1.0, 2.007, ...


def test_one_sided_evidence_still_decides(tmp_path: Path) -> None:
    # Only points (a price no comma convention writes): '1.234' is a decimal, as before.
    lines = ["sku;price;weight"] + [f"A{i};{398 + i}.53;{i + 1}.234" for i in range(30)]
    assert _convert(tmp_path, "p.csv", lines)["weight"] == [i + 1.234 for i in range(30)]
    # Only commas: '1.234' is a thousands group.
    lines = ["sku,price,weight"] + [f'A{i},"{i},5",{i + 1}.234' for i in range(30)]
    assert _convert(tmp_path, "q.csv", lines)["weight"] == [(i + 1) * 1000 + 234 for i in range(30)]


def _flat(texts: Iterable[str]) -> str:
    return " ".join(" ".join(texts).split())


def test_the_docs_state_the_rule_as_the_code_applies_it() -> None:
    guide = _flat((ROOT / "docs" / "agents-guide.md").read_text(encoding="utf-8").splitlines())
    assert (
        "The other columns decide only when they agree: a file with columns of both conventions "
        "(GPS coordinates `54.565860` beside amounts `1.093.086,46`) is read by its delimiter, "
        "and a time of day written `10.15` is no evidence."
    ) in guide
    readme = _flat((ROOT / "README.md").read_text(encoding="utf-8").splitlines())
    assert "then the file's other number columns when they agree, then the delimiter" in readme
