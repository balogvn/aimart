"""A convert's input is on the job's disk once, so it uses its size of the job cap, not twice that.

Incident (final-isolate review, round 2, at 230dabd): once the job cap counted the job's files,
``convert_upload``'s two copies of an upload both counted. It wrote the upload into a work
directory of its own and ``_convert`` made a second one; the engine copies an input that is
outside its work directory into it (DuckDB may read nothing else), so a 50 MB upload, the
advertised ``max_input_bytes``, used 100 MiB of the 700 MB cap before parsing began, measured
by the watchdog itself. A stored artifact given as a convert's input was copied the same way.

These tests read what the watchdog measured (it runs in the parent) during a real isolated job.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import aimart_testkit as kit
from aimart_parquet.artifacts import build_store
from aimart_parquet.engine import isolate
from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.service import Service, SourceRef

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

_MB = 1024 * 1024


@pytest.fixture
def peak(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """[most bytes of job files the watchdog saw in one poll, polls]."""
    seen = [0, 0]
    real = isolate._disk_bytes

    def measure(roots: Iterable[Path]) -> int:
        got = real(roots)
        seen[0] = max(seen[0], got)
        seen[1] += 1
        return got

    monkeypatch.setattr(isolate, "_disk_bytes", measure)
    return seen


@pytest.fixture
def isolated(make_settings: Callable[..., Settings]) -> Service:
    settings = make_settings(isolate_jobs=True)
    return Service(settings, build_store(settings))


async def test_an_upload_counts_once(isolated: Service, peak: list[int]) -> None:
    # 24 MB of short repetitive rows: parsing takes several polls, and the output is tiny.
    row = b"alpha,beta,gamma,delta,epsilon,zeta,eta,theta\n"
    body = b"c1,c2,c3,c4,c5,c6,c7,c8\n" + row * (24 * _MB // len(row))
    chunks = [body[i : i + _MB] for i in range(0, len(body), _MB)]

    async def read(_: int) -> bytes:
        return chunks.pop(0) if chunks else b""

    result = await isolated.convert_upload(read, "big.csv", ConvertOptions())
    assert result.report.rows == 24 * _MB // len(row)
    assert peak[0] >= len(body)  # the watchdog polled while the upload was there
    assert peak[0] < 1.5 * len(body), f"{peak[0] / _MB:.0f} MB of job files for a 24 MB upload"


async def test_a_stored_artifact_does_not_count(
    isolated: Service, peak: list[int], tmp_path: Path
) -> None:
    # An uncompressed Parquet of 40 MB whose zstd output is small: only a copy of the input
    # could put tens of MB on the job's disk.
    data = kit.write_parquet(
        tmp_path / "wide.parquet",
        {"s": [f"value-{i % 50}" * 4 for i in range(1_200_000)]},
        compression="none",
        use_dictionary=False,
    )
    size = data.stat().st_size
    assert size > 40 * _MB
    stored = isolated.store.put(data, "wide.parquet")
    result = await isolated.convert(SourceRef(artifact_id=stored.id), ConvertOptions())
    assert result.report.rows == 1_200_000
    assert peak[1] >= 3  # the watchdog polled while the job ran (a copy would have been seen)
    assert peak[0] < size / 4, f"{peak[0] / _MB:.0f} MB of job files for a stored input"
