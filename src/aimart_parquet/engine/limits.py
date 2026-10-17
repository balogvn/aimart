"""Resource limits for one conversion job, decoupled from the settings object."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aimart_parquet.config import Settings


@dataclass(frozen=True)
class Limits:
    max_input_bytes: int = 50 * 1024 * 1024
    max_decompressed_bytes: int = 512 * 1024 * 1024
    max_json_repair_bytes: int = 5 * 1024 * 1024
    duckdb_memory_limit: str = "1GB"
    duckdb_threads: int = 2

    @classmethod
    def from_settings(cls, s: Settings) -> Limits:
        return cls(
            max_input_bytes=s.max_input_bytes,
            max_decompressed_bytes=s.max_decompressed_bytes,
            max_json_repair_bytes=s.max_json_repair_bytes,
            duckdb_memory_limit=s.duckdb_memory_limit,
            duckdb_threads=s.duckdb_threads,
        )
