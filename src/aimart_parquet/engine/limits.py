"""Resource limits for one conversion job, decoupled from the settings object.

The defaults are ``Settings``' defaults (``Service`` uses ``from_settings``; tests build
``Limits()`` directly), so a limit changed in one place is changed in both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aimart_parquet.engine.duck import DEFAULT_MAX_TEMP_SIZE

if TYPE_CHECKING:
    from aimart_parquet.config import Settings

_MIB = 1024 * 1024
# What an isolated job holds outside DuckDB's buffer manager: the interpreter and the preloaded
# engine (~160 MB on Linux, shared with the forkserver but counted in the child's RSS), Arrow,
# and allocator slack. Measured on macOS, RSS ran 120-170 MB above DuckDB's own accounting.
JOB_OVERHEAD_MB = 300
_MIN_DUCKDB_MB = 128
_UNITS = {
    "": 1,
    "b": 1,
    "kb": 10**3,
    "mb": 10**6,
    "gb": 10**9,
    "tb": 10**12,
    "kib": 2**10,
    "mib": 2**20,
    "gib": 2**30,
    "tib": 2**40,
}


def size_bytes(size: str) -> int | None:
    """Bytes in a DuckDB size such as '1GB' or '512MiB'; None for a form it does not read."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-z]*)\s*", size.lower())
    if match is None or match[2] not in _UNITS:
        return None
    return int(float(match[1]) * _UNITS[match[2]])


def within_job_cap(memory_limit: str, job_memory_limit_mb: int) -> tuple[str, str]:
    """DuckDB's memory limit and spill cap for a job stopped at ``job_memory_limit_mb``.

    DuckDB spills a sort or an ordered write only once it reaches its own limit. Above the cap,
    the watchdog killed the job first: sort_by on a 450 MB CSV grew to 870 MB under the 1 GB
    default; under 400 MB it spilled 420 MB and peaked at 540 MB resident (measured in
    process, files not counted). The limit leaves ``JOB_OVERHEAD_MB`` of the cap to the rest
    of the process.

    The watchdog counts the job's files with its resident memory, and spill files are among
    them (in the work directory, or the job's temp directory for a query: RAM on Cloud Run).
    So DuckDB's memory and its spill together get the cap, no more: the spill cap is the cap
    minus the memory limit. A job past it could not have finished, since DuckDB spills only
    with its memory full and that memory is resident; DuckDB stopping itself is a 413 that
    names sort_by (``convert_file``), where the watchdog, polling every 0.1 s, can let a spill
    run hundreds of MB past the cap first. The 2 GB spill cap used before sat outside the cap.
    """
    budget = max(_MIN_DUCKDB_MB, job_memory_limit_mb - JOB_OVERHEAD_MB)
    configured = size_bytes(memory_limit)
    if configured is None or configured > budget * _MIB:
        memory_limit, configured = f"{budget}MiB", budget * _MIB
    spill = max(0, job_memory_limit_mb * _MIB - configured) // _MIB
    return memory_limit, f"{spill}MiB"


@dataclass(frozen=True)
class Limits:
    max_input_bytes: int = 50 * 1024 * 1024
    max_decompressed_bytes: int = 256 * 1024 * 1024  # a gzip/bz2/zip stream
    max_workbook_xml_bytes: int = 512 * 1024 * 1024  # all the XML parts of an xlsx/ods
    max_json_repair_bytes: int = 5 * 1024 * 1024
    duckdb_memory_limit: str = "1GB"
    duckdb_threads: int = 2
    duckdb_max_temp_size: str = DEFAULT_MAX_TEMP_SIZE

    @classmethod
    def from_settings(cls, s: Settings) -> Limits:
        memory_limit, max_temp_size = s.duckdb_memory_limit, DEFAULT_MAX_TEMP_SIZE
        if s.isolate_jobs and s.job_memory_limit_mb:
            memory_limit, max_temp_size = within_job_cap(memory_limit, s.job_memory_limit_mb)
        return cls(
            max_input_bytes=s.max_input_bytes,
            max_decompressed_bytes=s.max_decompressed_bytes,
            max_workbook_xml_bytes=s.max_workbook_xml_bytes,
            max_json_repair_bytes=s.max_json_repair_bytes,
            duckdb_memory_limit=memory_limit,
            duckdb_threads=s.duckdb_threads,
            duckdb_max_temp_size=max_temp_size,
        )
