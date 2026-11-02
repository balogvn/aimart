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


def _limits_table() -> list[tuple[str, list[str]]]:
    """(documented default, its `AIMART_*` settings) for each row of README.md's one limits table.

    The merge of perf/csv-memory into integrate/final left README with two limits tables that
    disagreed (512 MB against 256 MB for a gzip stream); they are one table now, under "Limits
    and errors", with a setting column, and this reads that one.
    """
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "### Limits" not in text
    section = text.split("## Limits and errors", 1)[1].split("\n## ", 1)[0]
    lines = [line for line in section.splitlines() if line.startswith("| ")]
    assert lines[0] == "| limit | default | setting | over it |"
    rows = [line.split(" | ") for line in lines[1:]]  # the |---| line is not "| "
    return [(cells[1], re.findall(r"`(AIMART_[A-Z_]+)`", cells[2])) for cells in rows]


def _shown(field: str, value: object) -> tuple[str, ...]:
    if field.endswith("_bytes"):
        mib = int(value) // _MIB  # type: ignore[call-overload]
        return (f"{mib} MB", f"{mib} MiB")
    if field.endswith("_s"):
        return (f"{float(value):g} s",)  # type: ignore[arg-type]
    if field.endswith("_mb"):
        return (f"{value} MB",)
    if field == "max_concurrent_jobs":
        return (f"{value} jobs",)
    return (f"{int(value):,}",)  # type: ignore[call-overload]


def test_readme_limits_table_shows_the_defaults(make_settings: Callable[..., Settings]) -> None:
    table = _limits_table()
    settings = make_settings()
    documented = {env for _, envs in table for env in envs}
    assert {
        "AIMART_MAX_INPUT_BYTES",
        "AIMART_MAX_DECOMPRESSED_BYTES",
        "AIMART_MAX_WORKBOOK_XML_BYTES",
        "AIMART_CONVERT_TIMEOUT_S",
        "AIMART_JOB_MEMORY_LIMIT_MB",
        "AIMART_MAX_CONCURRENT_JOBS",
    } <= documented
    for default, envs in table:
        assert envs, default
        for env in envs:
            field = env.removeprefix("AIMART_").lower()
            if field == "queue_wait_s":
                # A fixed 10 s before; unset, it is what the payment window leaves each
                # operation (Settings.queue_wait_for), and the row shows the convert's wait.
                assert settings.queue_wait_s is None
                shown = (f"{settings.queue_wait_for(settings.convert_timeout_s):g} s to convert",)
            else:
                shown = _shown(field, getattr(settings, field))
            assert any(form in default for form in shown), (env, default)


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
    assert f"spill cap = the rest of the job cap ({spill})" in section
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
