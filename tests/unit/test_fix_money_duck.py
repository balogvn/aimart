"""The query sandbox reveals nothing about the host through DuckDB's settings (S5).

The cwd leak via the default spill directory is guarded in test_tabular.py; DuckDB also
derives ``secret_directory`` from $HOME (``SELECT current_setting('secret_directory')`` returned
``<home>/.duckdb/stored_secrets``), and a caller's SQL can read every setting.
"""

from __future__ import annotations

import getpass
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import aimart_testkit as kit
from aimart_parquet.engine.duck import connect
from aimart_parquet.engine.tabular import query

if TYPE_CHECKING:
    from collections.abc import Iterator


def host_strings(*, with_tempdir: bool) -> dict[str, str]:
    """What must never appear in a setting a caller can read (too-short values are skipped)."""
    found = {
        "home": str(Path.home()),
        "cwd": str(Path.cwd()),
        "user": getpass.getuser(),
    }
    if with_tempdir:
        found["tempdir"] = tempfile.gettempdir()
        found["tempdir (resolved)"] = os.path.realpath(tempfile.gettempdir())
    return {k: v for k, v in found.items() if len(v.strip("/")) >= 4}


def leaks(settings: list[tuple[str, object]], own: list[Path], *, with_tempdir: bool) -> list[str]:
    problems = []
    for name, value in settings:
        text = str(value)
        for path in own:  # directories we created for the job are expected to appear
            for form in {str(path), str(path.resolve())}:
                text = text.replace(form, "<own>")
        problems += [
            f"{name}={value!r} contains {what}"
            for what, secret in host_strings(with_tempdir=with_tempdir).items()
            if secret in text
        ]
    return problems


@pytest.fixture
def job_dirs() -> Iterator[list[Path]]:
    # Not pytest's tmp_path: on CI it is /tmp/pytest-of-<user>/..., which would put the
    # username into allowed_directories by construction rather than by a leak.
    dirs = [Path(tempfile.mkdtemp(prefix="aimart-job-")) for _ in range(2)]
    yield dirs
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


def test_no_setting_reveals_home_cwd_user_or_tempdir(job_dirs: list[Path]) -> None:
    con = connect(memory_limit="256MB", threads=1, allowed_dirs=[job_dirs[0]])
    try:
        settings = con.execute("SELECT name, value FROM duckdb_settings()").fetchall()
    finally:
        con.close()
    assert leaks(settings, job_dirs, with_tempdir=True) == []


def test_no_setting_reveals_home_cwd_or_user_with_a_spill_dir(job_dirs: list[Path]) -> None:
    con = connect(memory_limit="256MB", threads=1, allowed_dirs=[job_dirs[0]], temp_dir=job_dirs[1])
    try:
        settings = con.execute("SELECT name, value FROM duckdb_settings()").fetchall()
        secret_dir = con.execute("SELECT current_setting('secret_directory')").fetchone()
        persistent = con.execute("SELECT current_setting('allow_persistent_secrets')").fetchone()
    finally:
        con.close()
    assert leaks(settings, job_dirs, with_tempdir=False) == []
    assert secret_dir is not None
    assert str(Path.home()) not in str(secret_dir[0])
    assert persistent == (False,)


def test_caller_sql_sees_no_host_paths(job_dirs: list[Path]) -> None:
    data = kit.write_parquet(job_dirs[0] / "t.parquet", {"a": [1]})
    result = query(
        {"data": data},
        "SELECT name, value FROM duckdb_settings()",
        limit=1000,
        fmt="json",
        query_timeout=10,
        max_chars=10_000,
        memory_limit="256MB",
        threads=1,
    )
    assert result.rows
    rows = [(str(name), value) for name, value in result.rows]
    assert {name for name, _ in rows} >= {
        "secret_directory",
        "home_directory",
        "extension_directory",
    }
    # The query's own spill directory lives under the system temp dir by design; nothing else
    # about the host (home, cwd, user) may appear.
    assert leaks(rows, job_dirs, with_tempdir=False) == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM duckdb_extensions()",  # errored with <home>/.duckdb/extensions/...
        "SELECT * FROM duckdb_secrets()",  # errored with <home>/.duckdb/stored_secrets
        "SELECT * FROM read_parquet('~/x.parquet')",
        "SELECT * FROM glob('~/*')",
    ],
)
def test_error_messages_reveal_no_host_paths(sql: str, job_dirs: list[Path]) -> None:
    con = connect(memory_limit="256MB", threads=1, allowed_dirs=[job_dirs[0]], temp_dir=job_dirs[1])
    try:
        try:
            text = str(con.execute(sql).fetchall())
        except Exception as exc:  # noqa: BLE001 - the message is what is under test
            text = str(exc)
    finally:
        con.close()
    assert leaks([(sql, text)], job_dirs, with_tempdir=False) == []


def test_the_locked_down_connection_still_reads_and_writes_parquet(job_dirs: list[Path]) -> None:
    src = kit.write_parquet(job_dirs[0] / "in.parquet", {"a": [1, 2, 3], "b": ["x", "y", "z"]})
    out = job_dirs[1] / "out.parquet"
    con = connect(memory_limit="256MB", threads=1, allowed_dirs=job_dirs, temp_dir=job_dirs[1])
    try:
        con.execute(
            f"COPY (SELECT * FROM read_parquet('{src}')) TO '{out}' (FORMAT parquet, COMPRESSION zstd)"
        )
        assert con.execute(f"SELECT sum(a) FROM read_parquet('{out}')").fetchone() == (6,)
        with pytest.raises(Exception, match="lock"):
            con.execute("SET secret_directory = '/tmp/x'")
    finally:
        con.close()
