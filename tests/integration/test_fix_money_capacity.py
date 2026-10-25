"""The artifact budget on the wire, round 2 (see tests/unit/test_fix_money_capacity.py).

RV1: on GCS, a paid query joining artifacts whose cached copies together exceed the local
budget failed with a 500 that leaked the server's artifact path; it now returns its rows.
RV2: of two concurrent paid outputs that each fit the budget but not together, one was charged
for a download_url already 404ing; now one is delivered and the other refused unpaid.
RV3: while paid calls waited for settlement, the RAM-backed disk held the budget plus every
unsettled output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anyio
import pytest

import aimart_testkit as kit
from aimart_parquet.artifacts import GcsArtifactStore
from fixtures.money.fake_gcs import FakeClient, key

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from x402 import SettleResponse

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

CSV = b"a,b\n" + b"".join(f"{i},{i * 7}\n".encode() for i in range(2000))


def convert_body(name: str) -> dict[str, Any]:
    return {"json": {"content_base64": kit.b64(CSV), "filename": name}}


def disk_bytes(root: Path) -> int:
    return sum(
        (p / "data.parquet").stat().st_size
        for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and (p / "data.parquet").exists()
    )


async def output_size(h: kit.AppHarness) -> int:
    """One output's size, measured with the app's own converter; the measuring file is removed."""
    service = h.app.state.service
    from aimart_parquet.engine.models import ConvertOptions
    from aimart_parquet.service import SourceRef

    result = await service.convert(
        SourceRef(content_base64=kit.b64(CSV), filename="m.csv"), ConvertOptions()
    )
    service.store.delete(result.artifact.id)
    return result.artifact.size_bytes


@dataclass
class SlowSettle(kit.FakeFacilitator):
    """Settles after a delay (mainnet settles after the transaction), recording the disk then."""

    root: Path | None = None
    peaks: list[int] = field(default_factory=list)

    async def settle(self, payload: Any, requirements: Any) -> SettleResponse:
        assert self.root is not None
        self.peaks.append(disk_bytes(self.root))
        await anyio.sleep(0.2)
        self.peaks.append(disk_bytes(self.root))
        return await super().settle(payload, requirements)


async def concurrent_converts(h: kit.AppHarness, n: int) -> list[Any]:
    responses: list[Any] = []

    async def one(i: int) -> None:
        responses.append(await h.paying.post("/v1/convert", **convert_body(f"{i}.csv")))

    async with anyio.create_task_group() as tg:
        for i in range(n):
            tg.start_soon(one, i)
    return responses


# ------------------------------------------------------------------------------ RV2
async def test_two_concurrent_outputs_that_do_not_fit_together(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings()
    fake = SlowSettle(root=settings.artifact_dir)
    async with kit.running_app(settings, facilitator=fake) as h:
        size = await output_size(h)
        h.app.state.service.store.max_total_bytes = int(size * 1.7)  # one fits, two do not
        responses = await concurrent_converts(h, 2)
        assert sorted(r.status_code for r in responses) == [200, 503]
        [ok] = [r for r in responses if r.status_code == 200]
        [refused] = [r for r in responses if r.status_code == 503]
        assert kit.payment_response(ok)["success"] is True
        assert refused.json()["error"]["code"] == "busy"
        assert "not charged" in refused.json()["error"]["hint"]
        assert "payment-response" not in refused.headers
        assert len(fake.settled) == 1
        # Before: both were 200 and settled, and the first caller's link 404'd straight away.
        link = ok.json()["artifact"]["download_url"].removeprefix(kit.BASE_URL)
        assert (await h.http.get(link)).status_code == 200


# ------------------------------------------------------------------------------ RV3
async def test_the_disk_stays_within_the_budget_while_calls_settle(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings()
    fake = SlowSettle(root=settings.artifact_dir)
    async with kit.running_app(settings, facilitator=fake) as h:
        size = await output_size(h)
        budget = int(size * 2.5)  # two fit
        h.app.state.service.store.max_total_bytes = budget
        responses = await concurrent_converts(h, 4)
    # Before: all four were stored while settling (2.81x the budget in the reviewer's run).
    assert sorted(r.status_code for r in responses) == [200, 200, 503, 503]
    assert len(fake.settled) == 2
    assert fake.peaks
    assert max(fake.peaks) <= budget
    assert disk_bytes(settings.artifact_dir) <= budget


# ------------------------------------------------------------------------------ RV1
async def test_gcs_paid_join_of_artifacts_bigger_than_the_cache_together(
    make_settings: Callable[..., Settings], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    from google.cloud import storage

    monkeypatch.setattr(storage, "Client", lambda: client)
    # Another instance stored both (or this one's cache dropped them): ~60% of the budget each.
    writer = GcsArtifactStore(
        tmp_path / "other-instance",
        bucket="aimart-test",
        ttl_s=3600,
        max_total_bytes=10**9,
        secret=b"k",
        client=client,
    )
    ids = {}
    for name in ("a", "b"):
        path = kit.write_parquet(
            tmp_path / f"{name}.parquet", {"k": list(range(4000)), name: list(range(4000))}
        )
        ids[name] = writer.put(path, f"{name}.parquet")
    budget = int(max(a.size_bytes for a in ids.values()) * 1.6)
    # Isolated, as deployed: the child opens the files by path after both were fetched.
    settings = make_settings(
        gcs_bucket="aimart-test", artifact_max_total_bytes=budget, isolate_jobs=True
    )
    fake = kit.FakeFacilitator()
    body = {
        "artifact_id": ids["a"].id,
        "tables": {"b": {"artifact_id": ids["b"].id}},
        "sql": "SELECT count(*) FROM data JOIN b USING (k)",
    }
    async with kit.running_app(settings, facilitator=fake) as h:
        r = await h.paying.post("/v1/query", json=body)
        # Before: 500 internal_error "FileNotFoundError: <server path>", every retry the same.
        assert r.status_code == 200, r.text
        assert r.json()["rows"] == [[4000]]
        assert kit.payment_response(r)["success"] is True
        assert len(fake.settled) == 1
        again = await h.paying.post("/v1/query", json=body)
        assert again.status_code == 200, again.text
    assert disk_bytes(settings.artifact_dir) <= budget  # trimmed back once the calls ended
    assert {key(art.id) for art in ids.values()} <= set(client.the_bucket.blobs)  # blobs untouched
