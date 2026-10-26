"""Every isolated job is given the call's work directories, so its files count toward its cap.

Incident (final security review, at e8d228e): the cap counted only the child's RSS, and the
decompressed input of a convert lives in the work directory ``Service`` makes, not in the
child's temp directory; a 1.5 MB gzip CSV put 501 MiB there unseen. ``run_isolated`` can only
count what it is told about, so each operation must pass its work directory (and an upload's).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit
from aimart_parquet import service as service_module
from aimart_parquet.artifacts import build_store
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


async def test_an_upload_is_watched_in_its_own_directory(
    service: Service, recorder: Recorder
) -> None:
    chunks = [CSV]

    async def read(_: int) -> bytes:
        return chunks.pop() if chunks else b""

    await service.convert_upload(read, "s.csv", ConvertOptions())
    [(name, dirs, inputs)] = recorder.calls
    assert name == "convert_file"
    assert len(dirs) == 2  # convert's work directory, and convert_upload's holding the upload
    assert inputs[0].name == "upload"
    assert _inside(inputs[0], dirs)
