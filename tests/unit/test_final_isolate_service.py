"""Every isolated job is given the call's work directories, so its files count toward its cap.

Incident (final security review, at e8d228e): the cap counted only the child's RSS, and the
decompressed input of a convert lives in the work directory ``Service`` makes, not in the
child's temp directory; a 1.5 MB gzip CSV put 501 MiB there unseen. ``run_isolated`` can only
count what it is told about, so each operation must pass its work directory, and an input must
be in it once: an upload is written there, a stored artifact is linked there.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit
from aimart_parquet import service as service_module
from aimart_parquet.artifacts import build_store
from aimart_parquet.engine import safety
from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.errors import InvalidInput
from aimart_parquet.service import Service, SourceRef

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

CSV = b"region,units\nNorth,10\nSouth,20\n"


class Recorder:
    """Stands in for run_isolated: records what the job would watch, then runs it in-process."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Path, ...], list[Path]]] = []

    def __call__(
        self,
        fn: Callable[..., Any],
        *args: Any,
        timeout_s: float,
        memory_limit_mb: int | None = None,
        job_dirs: tuple[Path, ...] = (),
        **kw: Any,
    ) -> Any:
        inputs = [a for a in args if hasattr(a, "is_file")]
        inputs += [p for a in args if isinstance(a, dict) for p in a.values()]
        # Checked now: the directories are deleted when the call ends.
        assert all(d.is_dir() for d in job_dirs), job_dirs
        self.calls.append((fn.__name__, tuple(job_dirs), inputs))
        if fn.__module__.startswith("aimart_parquet.quantum"):
            raise InvalidInput(
                "stopped by the test", hint="-"
            )  # the PennyLane work is not the point
        return fn(*args, **kw)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(service_module, "run_isolated", rec)
    return rec


@pytest.fixture
def service(make_settings: Callable[..., Settings]) -> Service:
    settings = make_settings(isolate_jobs=True)
    return Service(settings, build_store(settings))


def _inside(path: Path, dirs: tuple[Path, ...]) -> bool:
    return any(path.resolve().is_relative_to(d.resolve()) for d in dirs)


async def test_every_operation_passes_its_work_directory(
    service: Service, recorder: Recorder, tmp_path: Path
) -> None:
    pytest.importorskip("pennylane", reason="quantum extra not installed")
    from aimart_parquet.quantum.models import PortfolioOptions, QuantumCompressOptions

    work = service.settings.artifact_dir / ".work"
    inline = SourceRef(content_base64=kit.b64(CSV), filename="s.csv")
    art = (await service.convert(inline, ConvertOptions())).artifact
    parquet = SourceRef(
        content_base64=kit.b64(kit.write_parquet(tmp_path / "p.parquet", {"a": [1]}).read_bytes())
    )
    await service.inspect(parquet)
    await service.preview(parquet, columns=None, offset=0, limit=1, fmt="json")
    await service.query(
        {"data": parquet, "s": SourceRef(artifact_id=art.id)}, "SELECT 1", limit=1, fmt="json"
    )
    with pytest.raises(InvalidInput):
        await service.quantum_compress(parquet, QuantumCompressOptions())
    with pytest.raises(InvalidInput):
        await service.quantum_portfolio(parquet, PortfolioOptions())

    names = [name for name, _, _ in recorder.calls]
    assert names == ["convert_file", "inspect_parquet", "preview", "query", "compress", "optimize"]
    for name, dirs, inputs in recorder.calls:
        assert dirs, f"{name} watched no work directory"
        assert all(d.parent == work for d in dirs), (name, dirs)
        stored = service.settings.artifact_dir.resolve() / art.id
        downloaded = [p for p in inputs if not p.resolve().is_relative_to(stored)]
        assert downloaded, name
        assert all(_inside(p, dirs) for p in downloaded), (name, inputs, dirs)


@pytest.fixture
def copies(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every input the engine copies into its work directory (unwrap's DuckDB-sandbox copy)."""
    made: list[Path] = []
    real = safety.copy_file

    def record(src: Path, dst: Path) -> None:
        made.append(src)
        real(src, dst)

    monkeypatch.setattr(safety, "copy_file", record)
    return made


async def test_an_upload_is_converted_in_the_directory_it_was_written_to(
    service: Service, recorder: Recorder, copies: list[Path]
) -> None:
    # Review round 2: the upload sat in convert_upload's own directory, outside _convert's, so
    # the engine copied it in (DuckDB may read only its work directory), and both copies counted:
    # a 50 MB upload took 100 MB of the 700 MB job cap before parsing began.
    chunks = [CSV]

    async def read(_: int) -> bytes:
        return chunks.pop() if chunks else b""

    await service.convert_upload(read, "s.csv", ConvertOptions())
    [(name, dirs, inputs)] = recorder.calls
    assert name == "convert_file"
    assert len(dirs) == 1  # one work directory, holding the upload
    assert inputs[0].name == "upload"
    assert _inside(inputs[0], dirs)
    assert copies == []
    assert list((service.settings.artifact_dir / ".work").iterdir()) == []  # cleaned up


async def test_a_stored_artifact_is_linked_into_the_work_directory_not_copied(
    service: Service, recorder: Recorder, copies: list[Path], tmp_path: Path
) -> None:
    # A stored artifact is on the same disk and in the artifact budget already: a hard link puts
    # it where DuckDB may read it at no cost, and the watchdog does not count it a second time.
    stored = service.store.put(
        kit.write_parquet(tmp_path / "p.parquet", {"a": [1, 2]}), "p.parquet"
    )
    _, path = service.store.get(stored.id)
    before = path.read_bytes()
    result = await service.convert(SourceRef(artifact_id=stored.id), ConvertOptions())
    assert result.report.rows == 2
    [(name, dirs, inputs)] = recorder.calls
    assert name == "convert_file"
    assert _inside(inputs[0], dirs)
    assert inputs[0].name == "input.parquet"
    assert copies == []
    assert path.stat().st_nlink == 1  # the link went with the work directory
    assert path.read_bytes() == before  # and the stored file is intact


async def test_an_input_that_cannot_be_linked_is_still_copied_in(
    service: Service,
    recorder: Recorder,
    copies: list[Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Another filesystem (EXDEV) or a refused link: the engine's copy is the fallback.
    def refuse(src: object, dst: object) -> None:
        raise OSError(18, "Invalid cross-device link")

    stored = service.store.put(kit.write_parquet(tmp_path / "p.parquet", {"a": [1]}), "p.parquet")
    _, path = service.store.get(stored.id)
    monkeypatch.setattr(os, "link", refuse)
    result = await service.convert(SourceRef(artifact_id=stored.id), ConvertOptions())
    assert result.report.rows == 1
    assert copies == [path]


async def test_a_local_callers_own_file_is_copied_never_linked(
    make_settings: Callable[..., Settings],
    recorder: Recorder,
    copies: list[Path],
    tmp_path: Path,
) -> None:
    # Only the store's files are linked: a link to a local caller's file would put the
    # converter one bug away from writing to it, and changes the file's link count.
    settings = make_settings(isolate_jobs=True)
    service = Service(settings, build_store(settings), local_mode=True)
    mine = tmp_path / "mine.csv"
    mine.write_bytes(CSV)
    result = await service.convert(
        SourceRef(path=str(mine)), ConvertOptions(), output_path=str(tmp_path / "o.parquet")
    )
    assert result.report.rows == 2
    assert copies == [mine]
    assert mine.stat().st_nlink == 1
    assert mine.read_bytes() == CSV
