"""Service plumbing that every REST route and MCP tool shares: input selection, back-pressure,
deadlines, and the premium/provenance guards. Each failure here must be an AimartError (>= 400),
because that is what keeps the caller from being charged.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import anyio
import pytest

import aimart_testkit as kit
from aimart_parquet import quantum
from aimart_parquet import service as service_module
from aimart_parquet.artifacts import build_store
from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.errors import Busy, InvalidRequest, Timeout, UrlNotAllowed
from aimart_parquet.provenance import Signer
from aimart_parquet.service import QuantumUnavailable, Service, SourceRef
from aimart_parquet.sources import Fetched

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio


def make_service(settings: Settings, **kwargs: Any) -> Service:
    return Service(settings, build_store(settings), **kwargs)


@pytest.mark.parametrize(
    ("src", "count"),
    [
        (SourceRef(), 0),
        (SourceRef(url="https://e.example/a.csv", artifact_id="Zz" * 12), 2),
        (SourceRef(url="https://e.example/a", content_base64="YQ==", path="/etc/passwd"), 3),
    ],
)
async def test_exactly_one_input_is_required(
    src: SourceRef, count: int, settings: Settings
) -> None:
    with pytest.raises(InvalidRequest, match=f"exactly one input; got {count}") as info:
        await make_service(settings).inspect(src)
    assert info.value.hint == "Set one of: url, content_base64, artifact_id."


async def test_local_mode_hint_mentions_path(settings: Settings) -> None:
    with pytest.raises(InvalidRequest) as info:
        await make_service(settings, local_mode=True).inspect(SourceRef())
    assert info.value.hint == "Set one of: url, content_base64, artifact_id, path."


async def test_remote_service_refuses_paths(settings: Settings, tmp_path: Path) -> None:
    secret = kit.write(tmp_path / "secret.csv", "a\n1\n")
    with pytest.raises(InvalidRequest, match="only available in local mode"):
        await make_service(settings).convert(SourceRef(path=str(secret)), ConvertOptions())


async def test_private_urls_are_refused_by_default(settings: Settings) -> None:
    assert settings.allow_private_urls is False
    with pytest.raises(UrlNotAllowed):
        await make_service(settings).convert(
            SourceRef(url="http://127.0.0.1/a.csv"), ConvertOptions()
        )


async def test_url_input_uses_the_fetched_filename(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def fake_fetch(url: str, dest: Path, **kwargs: Any) -> Fetched:
        seen.update(kwargs, url=url)
        await anyio.Path(dest).write_bytes(b"a,b\n1,2\n")
        return Fetched(path=dest, filename="remote.csv", content_type="text/csv", size=8)

    monkeypatch.setattr(service_module, "fetch_url", fake_fetch)
    result = await make_service(settings).convert(
        SourceRef(url="https://e.example/x?y=1"), ConvertOptions()
    )
    assert result.artifact.filename == "remote.parquet"
    assert seen["max_bytes"] == settings.max_input_bytes
    assert seen["deadline_s"] == settings.fetch_timeout_s
    assert seen["allow_private"] is False


async def test_busy_when_the_queue_is_full(make_settings: Callable[..., Settings]) -> None:
    service = make_service(make_settings(max_concurrent_jobs=1))
    release = anyio.Event()
    holding = anyio.Event()

    async def hold() -> None:
        async with service._limiter:
            holding.set()
            await release.wait()

    async def wait_in_line() -> None:
        async with service._limiter:
            pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(hold)
        await holding.wait()
        for _ in range(4):
            tg.start_soon(wait_in_line)
        while service._limiter.statistics().tasks_waiting < 4:  # noqa: ASYNC110 - no event exists
            await anyio.sleep(0.001)
        with pytest.raises(Busy) as info:
            async with service._job():
                pytest.fail("a job slot was granted with the queue full")
        assert info.value.http_status == 503
        release.set()
    async with service._job():
        assert await service._execute(lambda: 7, timeout_s=1, isolate=False) == 7


async def test_in_process_deadline_is_a_timeout(settings: Settings) -> None:
    def slow() -> None:
        time.sleep(1.0)

    started = time.monotonic()
    with pytest.raises(Timeout, match=r"within 0\.05 seconds"):
        await make_service(settings)._execute(slow, timeout_s=0.05, isolate=False)
    assert time.monotonic() - started < 0.8  # abandoned at the deadline, not awaited


async def test_query_needs_a_table(settings: Settings) -> None:
    with pytest.raises(InvalidRequest, match="No tables"):
        await make_service(settings).query({}, "SELECT 1", limit=1, fmt="json")


async def test_quantum_tools_refuse_cleanly_when_the_extra_is_missing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aimart_parquet.quantum.models import PortfolioOptions, QuantumCompressOptions

    monkeypatch.setattr(quantum, "available", lambda: False)
    service = make_service(settings)
    src = SourceRef(artifact_id="Zz" * 12)
    with pytest.raises(QuantumUnavailable) as info:
        await service.quantum_compress(src, QuantumCompressOptions())
    assert info.value.http_status == 501
    assert info.value.code == "quantum_unavailable"
    with pytest.raises(QuantumUnavailable):
        await service.quantum_portfolio(src, PortfolioOptions())


async def test_verify_needs_a_signer(settings: Settings) -> None:
    with pytest.raises(InvalidRequest, match="does not sign"):
        await make_service(settings).verify_provenance({}, "AAAA")


async def test_hosted_artifacts_are_signed_and_verifiable(settings: Settings) -> None:
    service = make_service(settings, signer=Signer(None))
    result = await service.convert(
        SourceRef(content_base64=kit.b64(b"a\n1\n"), filename="../../evil name.csv"),
        ConvertOptions(),
    )
    art = result.artifact
    assert art.filename == "evil_name.parquet"  # caller filenames are sanitised
    assert art.provenance is not None
    manifest = art.provenance.manifest
    assert manifest["artifact_id"] == art.id
    assert manifest["sha256"] == art.sha256
    assert manifest["size_bytes"] == art.size_bytes
    assert manifest["operation"] == "convert"
    check = await service.verify_provenance(manifest, art.provenance.signature, artifact_id=art.id)
    assert check == {
        "valid": True,
        "signature_valid": True,
        "file_matches": True,
        "algorithm": "ML-DSA-65",
        "problems": [],
    }
    bad = await service.verify_provenance(manifest | {"operation": "x"}, art.provenance.signature)
    assert bad["valid"] is False
    assert bad["file_matches"] is None
