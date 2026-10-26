"""The per-job memory cap counts the job's files, not just the child's resident memory.

Incident (final security review, at e8d228e): on Cloud Run ``/tmp`` is RAM inside the same 2 GiB
as the API, the forkserver and both jobs, and the cap polled only the child's RSS. A 1.5 MB gzip
CSV sent as content_base64 decompressed to 501 MiB in the call's work directory while the
child's RSS climbed to the 700 MB cap on its own; two such calls held about 2.5 GB, enough to
OOM-kill the instance and every other caller's request, and both ended in 413, unpaid. The
watchdog now adds the bytes under the job's directories (its own temp directory and the work
directories ``Service`` passes) to the RSS.

These tests pin the child's RSS at zero in the parent (the watchdog runs there), so only the
files can trip the cap: before the fix every one of them that expects ``InputTooLarge``
converted or queried successfully.
"""

from __future__ import annotations

import base64
import gzip
import importlib
import os
from typing import TYPE_CHECKING

import pytest

import aimart_testkit as kit
from aimart_parquet import errors
from aimart_parquet.artifacts import build_store
from aimart_parquet.engine import isolate
from aimart_parquet.engine.isolate import run_isolated
from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.service import Service, SourceRef

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from aimart_parquet.config import Settings

helpers = importlib.import_module("integration.isolate_helpers")
_MB = 1024 * 1024
# Long enough for the watchdog (every 100 ms) to see the query's inputs while it runs.
_SLOW_SQL = "SELECT (SELECT count(*) FROM data) AS n, sum(hash(i)) AS h FROM range(100000000) t(i)"


@pytest.fixture
def files_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The child's resident memory reads as zero: only its files can reach the cap."""
    monkeypatch.setattr(isolate, "_read_rss", lambda pid: 0)


def _blob(path: Path, mb: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(mb * _MB))
    return path


# ---------------------------------------------------------------------- what counts as the job's
def test_files_under_the_roots_count_and_links_do_not_double_count(tmp_path: Path) -> None:
    job = tmp_path / "job"
    _blob(job / "input", 2)
    _blob(job / "deep" / "deeper" / "spill", 1)
    os.link(job / "input", job / "deep" / "input-link")  # both links are the job's: once
    outside = _blob(tmp_path / "store" / "data.parquet", 5)
    os.link(outside, job / "artifact-link")  # the store's file, already in its budget
    (job / "symlink").symlink_to(outside)  # never followed
    (job / "dirlink").symlink_to(outside.parent, target_is_directory=True)
    got = isolate._disk_bytes([job, tmp_path / "gone"])  # a vanished root is not an error
    assert 3 * _MB <= got < 3 * _MB + 64 * 1024  # 2 MB + 1 MB, plus block rounding


def test_a_file_the_store_dropped_becomes_the_jobs_own(tmp_path: Path) -> None:
    # The link no longer shares its blocks with anything: they are the job's to pay for now.
    outside = _blob(tmp_path / "store" / "data.parquet", 2)
    job = tmp_path / "job"
    job.mkdir()
    os.link(outside, job / "link")
    assert isolate._disk_bytes([job]) == 0
    outside.unlink()
    assert isolate._disk_bytes([job]) >= 2 * _MB


def test_the_walk_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(20):
        (tmp_path / f"f{i}").write_bytes(b"x" * 100_000)
    assert isolate._disk_bytes([tmp_path]) >= 20 * 100_000
    monkeypatch.setattr(isolate, "_MAX_WALK_ENTRIES", 5)
    assert isolate._disk_bytes([tmp_path]) < 6 * 128 * 1024


# ---------------------------------------------------------------------- the watchdog, for real
def test_files_in_a_job_directory_count_toward_the_cap(tmp_path: Path, files_only: None) -> None:
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(errors.InputTooLarge, match=r"memory budget \(16 MB\)") as info:
        run_isolated(
            helpers.write_then_sleep,
            str(work),
            40,
            30,
            timeout_s=60,
            memory_limit_mb=16,
            job_dirs=[work],
        )
    exc = info.value
    assert exc.http_status == 413  # >= 400: not charged
    assert "temporary files" in exc.message
    assert "not charged" in (exc.hint or "")
    assert "compressed file counts at its decompressed size" in (exc.hint or "")
    assert exc.details["limit_bytes"] == 16 * _MB
    assert exc.details["memory_bytes"] == 0
    assert exc.details["temp_file_bytes"] > 16 * _MB


def test_the_jobs_own_temp_directory_counts_without_being_named(files_only: None) -> None:
    with pytest.raises(errors.InputTooLarge, match="temporary files"):
        run_isolated(helpers.write_then_sleep, None, 40, 30, timeout_s=60, memory_limit_mb=16)


def test_a_directory_nobody_named_is_not_the_jobs(tmp_path: Path, files_only: None) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    got = run_isolated(
        helpers.write_then_sleep, str(elsewhere), 20, 0.5, timeout_s=60, memory_limit_mb=16
    )
    assert got == "woke up"


def test_memory_and_files_add_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 10 MB resident plus 10 MB of files: neither alone passes 16 MB, together they do.
    monkeypatch.setattr(isolate, "_read_rss", lambda pid: 10 * _MB)
    work = tmp_path / "work"
    _blob(work / "input", 10)
    with pytest.raises(errors.InputTooLarge, match=r"10 MB in memory plus 10 MB of temporary"):
        run_isolated(
            helpers.record_pid_then_sleep,
            os.devnull,
            30,
            timeout_s=60,
            memory_limit_mb=16,
            job_dirs=[work],
        )


# ---------------------------------------------------------------------- through the Service
@pytest.fixture
def isolated(make_settings: Callable[..., Settings]) -> Service:
    settings = make_settings(isolate_jobs=True, job_memory_limit_mb=16)
    return Service(settings, build_store(settings))


@pytest.mark.anyio
async def test_convert_counts_its_decompressed_input(isolated: Service, files_only: None) -> None:
    # The incident's shape, scaled down: a small gzip that inflates in the work directory.
    row = b"alpha,beta,gamma,delta,epsilon,zeta,eta,theta\n"
    data = gzip.compress(b"c1,c2,c3,c4,c5,c6,c7,c8\n" + row * (64 * _MB // len(row)), 9)
    assert len(data) < _MB
    src = SourceRef(content_base64=base64.b64encode(data).decode(), filename="big.csv.gz")
    with pytest.raises(errors.InputTooLarge, match="temporary files") as info:
        await isolated.convert(src, ConvertOptions())
    assert info.value.details["temp_file_bytes"] > 16 * _MB
    assert list((isolated.settings.artifact_dir / ".work").iterdir()) == []  # cleaned up


@pytest.mark.anyio
async def test_convert_counts_an_upload(isolated: Service, files_only: None) -> None:
    # convert_upload copies the upload into its own work directory, outside _convert's.
    body = kit.csv_text(["n", "s"], [[i, "x" * 200] for i in range(120_000)]).encode()
    chunks = [body[i : i + _MB] for i in range(0, len(body), _MB)]

    async def read(_: int) -> bytes:
        return chunks.pop(0) if chunks else b""

    with pytest.raises(errors.InputTooLarge, match="temporary files"):
        await isolated.convert_upload(read, "big.csv", ConvertOptions())


@pytest.mark.anyio
async def test_query_counts_inputs_it_downloaded_but_not_stored_artifacts(
    isolated: Service, files_only: None, tmp_path: Path
) -> None:
    # 20 rows of 1 MB of random bytes: about 20 MB of Parquet, over the 16 MB cap.
    data = kit.write_parquet(tmp_path / "big.parquet", {"b": [os.urandom(_MB) for _ in range(20)]})
    assert data.stat().st_size > 16 * _MB
    stored = isolated.store.put(data, "big.parquet")
    # A stored artifact is linked into the query's directory: no new bytes, and it is already
    # in the artifact budget. Counting it would refuse joins of artifacts that convert fine.
    ok = await isolated.query(
        {"data": SourceRef(artifact_id=stored.id)}, _SLOW_SQL, limit=5, fmt="json"
    )
    assert ok.rows[0][0] == 20
    # The same bytes sent inline are written into the call's work directory: they count.
    _, path = isolated.store.get(stored.id)
    inline = SourceRef(content_base64=base64.b64encode(path.read_bytes()).decode())
    with pytest.raises(errors.InputTooLarge, match="temporary files"):
        await isolated.query({"data": inline}, _SLOW_SQL, limit=5, fmt="json")


def test_the_decompression_limit_is_sized_to_the_cap() -> None:
    # Measured with the 700 MB default (see max_decompressed_bytes and ADR 0005): the largest
    # inputs that converted were 120 MB of CSV and 144 MB of xlsx XML, and every CSV of 150 MB
    # failed at the cap. 512 MB let an input that can only fail write twice as much RAM first.
    from aimart_parquet.config import Settings
    from aimart_parquet.engine.limits import Limits

    settings = Settings(_env_file=None)
    assert settings.job_memory_limit_mb == 700
    assert settings.max_decompressed_bytes == 256 * _MB
    assert Limits.from_settings(settings).max_decompressed_bytes == 256 * _MB
