"""Outputs nobody paid for, asserted on the wire and on disk.

M1: an output bigger than the artifact budget is refused (413, not settled) instead of being
stored, evicted, and charged for as a dead download link.
M2: when the result is withheld (settlement failed, or the call failed after the file was
stored), the stored file is deleted: unpaid outputs must not sit in the budget and push out
paying customers' artifacts.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from x402 import SettleResponse, parse_payment_required
from x402.http.clients.httpx import PaymentError

import aimart_testkit as kit
from aimart_parquet import artifacts
from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.service import SourceRef

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

# 2,000 rows: about 6.6 KB of Parquet, so a budget of a few KB holds exactly one of these.
CSV = b"a,b\n" + b"".join(f"{i},{i * 7}\n".encode() for i in range(2000))
SMALL_CSV = b"a,b\n1,2\n3,4\n"


def convert_body(data: bytes = CSV, name: str = "x.csv") -> dict[str, Any]:
    return {"json": {"content_base64": kit.b64(data), "filename": name}}


def stored(settings: Settings) -> set[str]:
    root = settings.artifact_dir
    return {p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")}


async def download_status(h: kit.AppHarness, artifact: dict[str, Any]) -> int:
    url = artifact["download_url"].removeprefix(kit.BASE_URL)
    return (await h.http.get(url)).status_code


@dataclass
class NonceFacilitator(kit.FakeFacilitator):
    """Settles each signed authorization once, like the chain does (the nonce is spent)."""

    spent: set[str] = field(default_factory=set)

    async def settle(self, payload: Any, requirements: Any) -> SettleResponse:
        self.settled.append((payload, requirements))
        await anyio.sleep(0.01)  # let concurrent requests reach settlement together
        nonce = json.dumps(payload.payload, sort_keys=True)
        if nonce in self.spent:
            return SettleResponse(
                success=False,
                error_reason="nonce_used",
                transaction="",
                network=requirements.network,
            )
        self.spent.add(nonce)
        return SettleResponse(
            success=True,
            transaction=kit.TX_HASH,
            network=requirements.network,
            payer=kit.PAYER,
            amount=requirements.amount,
        )


# ------------------------------------------------------------------------------ M1
async def test_output_over_the_artifact_budget_is_413_and_never_settled(
    make_settings: Callable[..., Settings],
) -> None:
    # Before: 200, settled, and the download_url in the body was already a 404.
    settings = make_settings(artifact_max_total_bytes=4000)
    fake = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitator=fake) as h:
        r = await h.paying.post("/v1/convert", **convert_body())
    assert r.status_code == 413, r.text
    error = r.json()["error"]
    assert error["code"] == "input_too_large"
    assert "artifact budget" in error["message"]
    assert "not charged" in error["hint"]
    assert "payment-response" not in r.headers
    assert len(fake.verified) == 1
    assert fake.settled == []
    assert stored(settings) == set()


async def test_output_that_fits_is_stored_and_downloadable(
    make_settings: Callable[..., Settings],
) -> None:
    # The budget holds one. Round 1 let the second output evict the first right away, so the
    # first caller's paid link was dead: now a just-delivered output stays, and the second
    # caller is refused (503, not charged) until the first is old enough to make room.
    settings = make_settings(artifact_max_total_bytes=12_000)
    fake = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitator=fake) as h:
        first = (await h.paying.post("/v1/convert", **convert_body(name="1.csv"))).json()
        refused = await h.paying.post("/v1/convert", **convert_body(name="2.csv"))
        assert refused.status_code == 503, refused.text
        assert refused.json()["error"]["code"] == "busy"
        assert "not charged" in refused.json()["error"]["hint"]
        assert "payment-response" not in refused.headers
        assert len(fake.settled) == 1
        assert await download_status(h, first["artifact"]) == 200

        then = time.time() - artifacts.MIN_RESIDENCY_S - 60
        os.utime(settings.artifact_dir / first["artifact"]["id"], (then, then))
        third = (await h.paying.post("/v1/convert", **convert_body(name="3.csv"))).json()
        assert await download_status(h, third["artifact"]) == 200
        # The older paid artifact made room; the new one never does.
        assert stored(settings) == {third["artifact"]["id"]}
        assert await download_status(h, first["artifact"]) == 404


# ------------------------------------------------------------------------------ M2, REST
async def test_rest_settlement_failure_deletes_the_withheld_output(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(artifact_max_total_bytes=20_000)  # room for two outputs
    fake = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitator=fake) as h:
        paid = await h.paying.post("/v1/convert", **convert_body(name="paid.csv"))
        assert paid.status_code == 200
        art = paid.json()["artifact"]
        assert kit.payment_response(paid)["success"] is True

        fake.settle_ok = False  # the next caller's settlement fails: nothing is charged...
        unpaid = await h.paying.post("/v1/convert", **convert_body(name="unpaid.csv"))
        assert unpaid.status_code == 402
        assert "artifact" not in unpaid.text

        # ...so its output is gone, and the paying customer's artifact was not evicted for it.
        assert stored(settings) == {art["id"]}
        assert await download_status(h, art) == 200
        fake.settle_ok = True
        inspect = await h.paying.post("/v1/inspect", json={"artifact_id": art["id"]})
        assert inspect.status_code == 200, inspect.text


async def test_rest_settlement_exception_deletes_the_withheld_output(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    facilitator.settle_error = RuntimeError("chain congested")
    r = await paid_app.paying.post("/v1/convert", **convert_body())
    assert r.status_code == 402
    assert stored(paid_app.settings) == set()


async def test_rest_paid_call_keeps_its_output(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    for path, kwargs in (
        ("/v1/convert", convert_body()),
        ("/v1/convert/upload", {"files": {"file": ("u.csv", SMALL_CSV, "text/csv")}}),
    ):
        r = await paid_app.paying.post(path, **kwargs)
        assert r.status_code == 200, r.text
        art = r.json()["artifact"]
        assert art["id"] in stored(paid_app.settings)
        assert await download_status(paid_app, art) == 200
    assert len(facilitator.settled) == 2


async def test_rest_upload_settlement_failure_deletes_the_output(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    facilitator.settle_ok = False
    r = await paid_app.paying.post(
        "/v1/convert/upload", files={"file": ("u.csv", SMALL_CSV, "text/csv")}
    )
    assert r.status_code == 402
    assert stored(paid_app.settings) == set()


async def test_rest_failure_after_the_output_was_stored_deletes_it(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The file is stored, then building the response fails: the caller is not charged (>= 400)
    # and must not leave an orphan in the budget either.
    from aimart_parquet.errors import AimartError

    store = paid_app.app.state.service.store

    def broken_link(*_: Any) -> str:
        raise AimartError("Could not sign the download link.", hint="Retry.")

    monkeypatch.setattr(store, "download_url", broken_link)
    r = await paid_app.paying.post("/v1/convert", **convert_body())
    assert r.status_code == 500
    assert facilitator.settled == []
    assert stored(paid_app.settings) == set()


async def test_one_signed_payment_replayed_concurrently_leaves_one_output(
    make_settings: Callable[..., Settings],
) -> None:
    # Each copy of the same authorization verifies and runs; only one settles. Before, every
    # unpaid copy's artifact stayed stored and evicted paying customers' artifacts first.
    settings = make_settings()
    fake = NonceFacilitator()
    async with kit.running_app(settings, facilitator=fake) as h:
        quote = await h.http.post("/v1/convert", **convert_body(SMALL_CSV))
        required = parse_payment_required(base64.b64decode(quote.headers["payment-required"]))
        payload = await kit.paying_client().create_payment_payload(required)
        signature = base64.b64encode(
            json.dumps(payload.model_dump(by_alias=True, exclude_none=True)).encode()
        ).decode()
        statuses: list[int] = []
        bodies: list[dict[str, Any]] = []

        async def replay() -> None:
            r = await h.http.post(
                "/v1/convert",
                **convert_body(SMALL_CSV),
                headers={"payment-signature": signature},
            )
            statuses.append(r.status_code)
            if r.status_code == 200:
                bodies.append(r.json())

        async with anyio.create_task_group() as tg:
            for _ in range(4):
                tg.start_soon(replay)
    assert sorted(statuses) == [200, 402, 402, 402]
    assert len(fake.spent) == 1
    assert stored(settings) == {bodies[0]["artifact"]["id"]}


# ------------------------------------------------------------------------------ M2, MCP
async def _paid_mcp_call(
    h: kit.AppHarness, tool: str, args: dict[str, Any], setup: dict[str, Any]
) -> Any:
    async with kit.mcp_over_http(h.app) as client:
        unpaid = await client.call_tool(tool, args)
        meta = await kit.mcp_payment_meta(kit.paying_client(), unpaid.structured_content)
        assert h.facilitator is not None
        for name, value in setup.items():
            setattr(h.facilitator, name, value)
        return await client.call_tool(tool, args, meta=meta)


@pytest.mark.parametrize(
    "setup",
    [{"settle_ok": False}, {"settle_error": RuntimeError("nonce reused")}],
    ids=["settle-false", "settle-raises"],
)
async def test_mcp_settlement_failure_deletes_the_withheld_output(
    setup: dict[str, Any], paid_app: kit.AppHarness
) -> None:
    keep = await paid_app.app.state.service.convert(
        SourceRef(content_base64=kit.b64(SMALL_CSV), filename="keep.csv"), ConvertOptions()
    )
    args = {"content_base64": kit.b64(SMALL_CSV), "filename": "x.csv"}
    result = await _paid_mcp_call(paid_app, "convert_to_parquet", args, setup)
    assert result.is_error
    assert result.structured_content["error"].startswith("Payment settlement failed")
    assert stored(paid_app.settings) == {keep.artifact.id}


async def test_mcp_paid_call_keeps_its_output(paid_app: kit.AppHarness) -> None:
    args = {"content_base64": kit.b64(SMALL_CSV), "filename": "x.csv"}
    result = await _paid_mcp_call(paid_app, "convert_to_parquet", args, {})
    assert not result.is_error, result.structured_content
    art = result.structured_content["artifact"]
    assert stored(paid_app.settings) == {art["id"]}
    assert await download_status(paid_app, art) == 200


async def test_mcp_tool_error_after_the_output_was_stored_deletes_it(
    paid_app: kit.AppHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The SDK rejects the tool's output after the file was stored (the result becomes isError,
    # so nothing is settled); the stored file must go too.
    from pydantic import BaseModel

    service = paid_app.app.state.service
    real = service.convert

    class WrongShape(BaseModel):
        unexpected: int = 1

    async def convert_then_fail(*args: Any, **kwargs: Any) -> Any:
        await real(*args, **kwargs)
        return WrongShape()

    monkeypatch.setattr(service, "convert", convert_then_fail)
    args = {"content_base64": kit.b64(SMALL_CSV), "filename": "x.csv"}
    result = await _paid_mcp_call(paid_app, "convert_to_parquet", args, {})
    assert result.is_error
    assert paid_app.facilitator is not None
    assert paid_app.facilitator.settled == []
    assert stored(paid_app.settings) == set()


async def test_withheld_cleanup_failure_is_logged_not_raised(
    make_settings: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    settings = make_settings()
    fake = kit.FakeFacilitator(settle_ok=False)
    async with kit.running_app(settings, facilitator=fake) as h:
        store = h.app.state.service.store

        def broken_delete(_: str) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(store, "delete", broken_delete)
        r = await h.paying.post("/v1/convert", **convert_body(SMALL_CSV))
        assert r.status_code == 402  # the caller still gets the settlement error, not a 500
        args = {"content_base64": kit.b64(SMALL_CSV), "filename": "x.csv"}
        result = await _paid_mcp_call(h, "convert_to_parquet", args, {})
        assert result.structured_content["error"].startswith("Payment settlement failed")
    assert sum("withheld" in rec.message for rec in caplog.records) >= 2


async def test_a_gate_without_a_store_leaves_no_output_provisional(
    settings: Settings, tmp_path: Path
) -> None:
    # Outputs of a tracked call stay provisional (never evicted) until committed;
    # a gate with nothing to commit them to must not track them, or they would stay so for good.
    from types import SimpleNamespace

    from mcp_types import CallToolResult

    from aimart_parquet.artifacts import LocalArtifactStore
    from aimart_parquet.payments import Payments

    payments = Payments(settings, [kit.FakeFacilitator()])
    gate = payments.mcp_extension()
    op = payments.tool_operation("convert_to_parquet")
    assert op is not None
    reqs, resource = await payments.mcp_requirements(op)
    pr = await payments.server.create_payment_required_response(reqs, resource, error="x")
    meta = await kit.mcp_payment_meta(
        kit.paying_client(), pr.model_dump(by_alias=True, exclude_none=True)
    )
    store = LocalArtifactStore(tmp_path / "s", ttl_s=60, max_total_bytes=10**6, secret=b"k")
    out = tmp_path / "out.parquet"
    out.write_bytes(b"x" * 10)

    async def tool(_: Any) -> CallToolResult:
        store.put(out, "x.parquet")
        return CallToolResult(content=[], is_error=False)

    params = SimpleNamespace(name="convert_to_parquet", arguments={}, meta=meta)
    result = await gate.intercept_tool_call(params, None, tool)
    assert not result.is_error
    assert result.meta["x402/payment-response"]["success"] is True
    assert len(store._folders()) == 1
    assert store._provisional == set()


async def test_rest_handler_crash_after_the_output_was_stored_deletes_it(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Not an AimartError: the exception escapes the route (a 500, never settled); the file it
    # stored first must not stay behind.
    service = paid_app.app.state.service
    real = service.convert

    async def convert_then_crash(*args: Any, **kwargs: Any) -> Any:
        await real(*args, **kwargs)
        raise RuntimeError("bug after storing")

    monkeypatch.setattr(service, "convert", convert_then_crash)
    # The paying client wraps what the in-process transport re-raises.
    with pytest.raises(PaymentError, match="bug after storing"):
        await paid_app.paying.post("/v1/convert", **convert_body(SMALL_CSV))
    assert facilitator.settled == []
    assert stored(paid_app.settings) == set()


async def test_mcp_tool_crash_after_the_output_was_stored_deletes_it(
    settings: Settings, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    from aimart_parquet.artifacts import LocalArtifactStore
    from aimart_parquet.payments import Payments

    fake = kit.FakeFacilitator()
    payments = Payments(settings, [fake])
    store = LocalArtifactStore(tmp_path / "s", ttl_s=60, max_total_bytes=10**6, secret=b"k")
    gate = payments.mcp_extension(store=store)
    op = payments.tool_operation("convert_to_parquet")
    assert op is not None
    reqs, resource = await payments.mcp_requirements(op)
    pr = await payments.server.create_payment_required_response(reqs, resource, error="x")
    meta = await kit.mcp_payment_meta(
        kit.paying_client(), pr.model_dump(by_alias=True, exclude_none=True)
    )
    out = tmp_path / "out.parquet"
    out.write_bytes(b"x" * 10)

    async def tool(_: Any) -> Any:
        store.put(out, "x.parquet")
        raise anyio.get_cancelled_exc_class()  # e.g. the client went away mid-call

    params = SimpleNamespace(name="convert_to_parquet", arguments={}, meta=meta)
    with pytest.raises(anyio.get_cancelled_exc_class()):
        await gate.intercept_tool_call(params, None, tool)
    assert store._folders() == []
    assert store._provisional == set()
    assert fake.settled == []


@dataclass
class CancelledSettle(kit.FakeFacilitator):
    """The client goes away while the facilitator settles: the gate sees a cancellation."""

    async def settle(self, payload: Any, requirements: Any) -> SettleResponse:
        self.settled.append((payload, requirements))
        raise anyio.get_cancelled_exc_class()


async def test_mcp_settlement_cancelled_deletes_the_withheld_output(
    settings: Settings, tmp_path: Path
) -> None:
    # A cancellation is not an Exception, so the settle handler's `except Exception` missed it
    # and the output stayed provisional (uncounted, never evicted) until its TTL, although no
    # caller would ever receive its id.
    from types import SimpleNamespace

    from mcp_types import CallToolResult

    from aimart_parquet.artifacts import LocalArtifactStore
    from aimart_parquet.payments import Payments

    fake = CancelledSettle()
    payments = Payments(settings, [fake])
    store = LocalArtifactStore(tmp_path / "s", ttl_s=60, max_total_bytes=10**6, secret=b"k")
    gate = payments.mcp_extension(store=store)
    op = payments.tool_operation("convert_to_parquet")
    assert op is not None
    reqs, resource = await payments.mcp_requirements(op)
    pr = await payments.server.create_payment_required_response(reqs, resource, error="x")
    meta = await kit.mcp_payment_meta(
        kit.paying_client(), pr.model_dump(by_alias=True, exclude_none=True)
    )
    out = tmp_path / "out.parquet"
    out.write_bytes(b"x" * 10)

    async def tool(_: Any) -> CallToolResult:
        store.put(out, "x.parquet")
        return CallToolResult(content=[], is_error=False)

    params = SimpleNamespace(name="convert_to_parquet", arguments={}, meta=meta)
    with pytest.raises(anyio.get_cancelled_exc_class()):
        await gate.intercept_tool_call(params, None, tool)
    assert len(fake.settled) == 1
    assert store._folders() == []
    assert store._provisional == set()
