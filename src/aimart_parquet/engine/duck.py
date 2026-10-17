"""DuckDB connections that are safe to point at untrusted input and untrusted SQL.

Two rules hold everywhere a connection is made:

1. File access is locked to directories we created for this job (``allowed_directories``)
   with ``enable_external_access`` off, so neither a crafted file name nor a caller's SQL can
   read /etc/passwd, write elsewhere, or reach the network.
2. ``lock_configuration`` is set last, so SQL cannot undo rule 1 with ``SET``.

Identifiers and literals that we interpolate into SQL go through ``ident``/``literal``; nothing
user-supplied is ever concatenated raw.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import duckdb

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


def ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def connect(
    *,
    memory_limit: str,
    threads: int,
    allowed_dirs: Iterable[Path] = (),
    temp_dir: Path | None = None,
) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(
        database=":memory:",
        config={
            "threads": max(1, threads),
            "memory_limit": memory_limit,
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
        },
    )
    con.execute("SET TimeZone = 'UTC'")
    if temp_dir is not None:
        con.execute(f"SET temp_directory = {literal(str(temp_dir))}")
        con.execute("SET max_temp_directory_size = '2GB'")
    dirs = [str(d.resolve()) for d in allowed_dirs]
    if dirs:
        con.execute(f"SET allowed_directories = [{', '.join(literal(d) for d in dirs)}]")
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con
