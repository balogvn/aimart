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

Since 1886f60 (merged after these tests) a gzip of UTF-8 CSV of one member is never unpacked:
DuckDB reads it, so the incident's file no longer writes its text anywhere and converts within
the files budget (``test_the_incidents_gzip_no_longer_lands_in_the_work_directory``). A gzip CSV that
is not UTF-8 still writes its UTF-8 version there, so the tests that need the job's files to
reach the cap send that one.
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


_ROW = b"alpha,beta,gamma,delta,epsilon,zeta,eta,theta\n"


def _incident_gzip(header: bytes = b"c1,c2,c3,c4,c5,c6,c7,c8\n") -> bytes:
    """The incident's shape, scaled down: 64 MB of CSV text in under 1 MB of gzip."""
    data = gzip.compress(header + _ROW * (64 * _MB // len(_ROW)), 9)
    assert len(data) < _MB
    return data


# A cp1252 header: the text is not UTF-8, so its UTF-8 version is written to the work directory
# (transcoded straight from the gzip since 1886f60), 64 MB of the job's files.
_CP1252_HEADER = "c1,c2,c3,c4,c5,c6,c7,café\n".encode("cp1252")


@pytest.mark.anyio
async def test_convert_counts_its_decompressed_input(isolated: Service, files_only: None) -> None:
    # Before 1886f60 this sent the incident's UTF-8 gzip, which unwrap inflated into the work
    # directory; that one is now read from the gzip (the test below). The same text in cp1252
    # still lands there, as its UTF-8 version, and counts.
    src = SourceRef(
        content_base64=base64.b64encode(_incident_gzip(_CP1252_HEADER)).decode(),
        filename="big.csv.gz",
    )
    with pytest.raises(errors.InputTooLarge, match="temporary files") as info:
        await isolated.convert(src, ConvertOptions(encoding="cp1252"))
    assert info.value.details["temp_file_bytes"] > 16 * _MB
    assert list((isolated.settings.artifact_dir / ".work").iterdir()) == []  # cleaned up


@pytest.mark.anyio
async def test_the_incidents_gzip_no_longer_lands_in_the_work_directory(
    isolated: Service, files_only: None
) -> None:
    # The incident's own file: 64 MB of UTF-8 CSV text, 16 MB of files allowed. DuckDB reads the
    # gzip after one in-memory pass, so the job's files are the upload, its link and the small
    # staged text and output: it converts where it was a 413 (and, before the watchdog counted
    # files, 501 MiB on the RAM disk).
    data = _incident_gzip()
    src = SourceRef(content_base64=base64.b64encode(data).decode(), filename="big.csv.gz")
    result = await isolated.convert(src, ConvertOptions())
    assert result.report.rows == 64 * _MB // len(_ROW)
    assert result.report.estimated_tokens_raw == len(gzip.decompress(data)) // 4


@pytest.mark.anyio
async def test_the_incident_on_the_wire_is_an_unpaid_413(
    make_settings: Callable[..., Settings], files_only: None
) -> None:
    # The cp1252 twin of the incident's file (see test_convert_counts_its_decompressed_input).
    settings = make_settings(isolate_jobs=True, job_memory_limit_mb=16)
    facilitator = kit.FakeFacilitator()
    body = {
        "content_base64": base64.b64encode(_incident_gzip(_CP1252_HEADER)).decode(),
        "filename": "big.csv.gz",
        "options": {"encoding": "cp1252"},
    }
    async with kit.running_app(settings, facilitator=facilitator) as h:
        r = await h.paying.post("/v1/convert", json=body)
    assert r.status_code == 413, r.text
    error = r.json()["error"]
    assert error["code"] == "input_too_large"
    assert "MB of temporary files" in error["message"]
    assert error["details"]["temp_file_bytes"] > 16 * _MB
    assert "not charged" in error["hint"]
    assert "payment-response" not in r.headers
    assert len(facilitator.verified) == 1
    assert facilitator.settled == []


@pytest.mark.anyio
async def test_convert_counts_an_upload(isolated: Service, files_only: None) -> None:
    # convert_upload writes the upload into the work directory the convert uses: it counts once
    # (integration/test_final_isolate_upload.py), and once is over this 16 MB cap.
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


def test_the_workbook_cap_is_not_the_stream_cap() -> None:
    # Round 1 lowered max_decompressed_bytes to 256 MB, sized to the 700 MB job cap from
    # single-sheet measurements. The excel reader summed every part of a workbook against it,
    # though one sheet is parsed and none is written to disk, so 256 MB refused a workbook whose
    # small sheet converted before (integration/test_final_isolate_workbook.py), and round 2
    # put it back at 512 MB for streams too. Workbooks now have max_workbook_xml_bytes (512 MB)
    # and streams 256 MB again (integration/test_final_integrate_stream_cap.py); a stream's
    # decompressed file also counts toward the job cap (test_convert_counts_its_decompressed_
    # input above). This test asserted the shared 512 MB until the split.
    from aimart_parquet.config import Settings
    from aimart_parquet.engine.limits import Limits

    settings = Settings(_env_file=None)
    assert settings.job_memory_limit_mb == 700
    assert settings.max_decompressed_bytes == 256 * _MB
    assert settings.max_workbook_xml_bytes == 512 * _MB
    limits = Limits.from_settings(settings)
    assert (limits.max_decompressed_bytes, limits.max_workbook_xml_bytes) == (256 * _MB, 512 * _MB)
    assert Limits().max_decompressed_bytes == settings.max_decompressed_bytes  # one default
    assert Limits().max_workbook_xml_bytes == settings.max_workbook_xml_bytes
