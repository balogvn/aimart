"""The limits README.md and deploy/README.md publish are the ones the code applies.

Operators size instances and callers size files from these tables; a default changed in
``config.Settings`` or ``engine/limits.py`` without them makes the documented capacity wrong.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from aimart_parquet.engine.limits import Limits

if TYPE_CHECKING:
    from collections.abc import Callable

    from aimart_parquet.config import Settings

ROOT = Path(__file__).resolve().parents[2]
_MIB = 1024 * 1024


def _limits_table() -> dict[str, str]:
    """`AIMART_*` setting -> its documented default, from README.md's Limits table."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = text.split("### Limits", 1)[1].split("\n\n", 2)[1]
    rows = [line.split("|")[1:-1] for line in section.splitlines() if line.startswith("| ")]
    return {cells[2].strip().strip("`"): cells[1].strip() for cells in rows[1:]}


def _shown(field: str, value: object) -> str:
    if field.endswith("_bytes"):
        return f"{int(value) // _MIB} MB"  # type: ignore[call-overload]
    if field.endswith("_s"):
        return f"{float(value):g} s"  # type: ignore[arg-type]
    if field.endswith("_mb"):
        return f"{value} MB"
    return str(value)


def test_readme_limits_table_shows_the_defaults(make_settings: Callable[..., Settings]) -> None:
    table = _limits_table()
    assert len(table) == 5
    settings = make_settings()
    for env, documented in table.items():
        field = env.removeprefix("AIMART_").lower()
        assert documented == _shown(field, getattr(settings, field)), env


def test_deploy_readme_states_the_derived_duckdb_limits(
    make_settings: Callable[..., Settings],
) -> None:
    limits = Limits.from_settings(make_settings(isolate_jobs=True))
    text = " ".join((ROOT / "deploy" / "README.md").read_text(encoding="utf-8").split())
    section = text.split("### Memory capacity", 1)[1].split("## 4.", 1)[0]
    memory, spill = (
        v.replace("MiB", " MiB") for v in (limits.duckdb_memory_limit, limits.duckdb_max_temp_size)
    )
    assert f"limit = the job cap minus 300 MB ({memory})" in section
    assert f"spill cap = the job cap ({spill})" in section
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    assert f"spills to disk past {memory}" in readme


def test_readme_links_to_a_deploy_heading_that_exists() -> None:
    # README links to the capacity section by its GitHub anchor; renaming the heading breaks it.
    deploy = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    headings = re.findall(r"^### (.+)$", deploy, flags=re.MULTILINE)
    anchors = {re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-") for h in headings}
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"deploy/README\.md#([a-z0-9-]+)", readme)
    assert links
    assert set(links) <= anchors
