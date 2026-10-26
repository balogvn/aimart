"""Resource limits for one conversion job, decoupled from the settings object."""

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
    """DuckDB's memory limit and spill cap for a job killed at ``job_memory_limit_mb`` of RSS.

    DuckDB spills a sort or an ordered write only once it reaches its own limit. Above the cap,
    the watchdog killed the job first: sort_by on a 450 MB CSV grew to 870 MB under the 1 GB
    default, and under 400 MB it spilled 420 MB and finished at 540 MB. Spill files are memory
    too on Cloud Run's RAM disk, so they get the job's cap, not 2 GB of a 2 GiB instance.
    """
    budget = max(_MIN_DUCKDB_MB, job_memory_limit_mb - JOB_OVERHEAD_MB)
    configured = size_bytes(memory_limit)
    if configured is None or configured > budget * _MIB:
        memory_limit = f"{budget}MiB"
    return memory_limit, f"{job_memory_limit_mb}MiB"


@dataclass(frozen=True)
class Limits:
    max_input_bytes: int = 50 * 1024 * 1024
    max_decompressed_bytes: int = 512 * 1024 * 1024
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
            max_json_repair_bytes=s.max_json_repair_bytes,
            duckdb_memory_limit=memory_limit,
            duckdb_threads=s.duckdb_threads,
            duckdb_max_temp_size=max_temp_size,
        )
