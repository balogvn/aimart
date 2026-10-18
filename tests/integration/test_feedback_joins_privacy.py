"""Panel-driven features over the wire: demand signals, multi-file joins, published retention."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit

if TYPE_CHECKING:
    from collections.abc import Callable

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

GL = b"txn_id,amount\nT1,100.00\nT2,250.50\nT3,75.25\n"
BANK = b"txn_id,amount\nT1,100.00\nT3,75.25\nT9,10.00\n"


async def _convert(h: kit.AppHarness, data: bytes, name: str) -> str:
    r = await h.paying.post("/v1/convert", json={"content_base64": kit.b64(data), "filename": name})
    assert r.status_code == 200, r.text
    artifact_id: str = r.json()["artifact"]["id"]
    return artifact_id


async def test_unsupported_upload_is_recorded_as_demand(free_app: kit.AppHarness) -> None:
    r = await free_app.http.post(
        "/v1/convert",
        json={"content_base64": kit.b64(b"\x00\x01binary\x00"), "filename": "slides.pptx"},
    )
    assert r.status_code == 415
    service = free_app.app.state.service
    assert service.demand.snapshot()["unsupported_formats"] == {".pptx": 1}


async def test_feedback_is_free_and_acknowledged(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    r = await paid_app.http.post(
        "/v1/feedback",
        json={
            "task": "Turn scanned receipts into a table",
            "input_formats": ["jpg"],
            "max_price_usd": 0.05,
        },
    )
    assert r.status_code == 200
    assert r.json()["received"] is True
    assert facilitator.verified == []
    assert facilitator.settled == []


async def test_feedback_validation(free_app: kit.AppHarness) -> None:
    r = await free_app.http.post("/v1/feedback", json={"task": "x", "email": "a@b.c"})
    assert r.status_code == 400


async def test_demand_report_requires_the_admin_token(
    make_settings: Callable[..., Settings],
) -> None:
    async with kit.running_app(make_settings(admin_token="s3cret-token"), paid=False) as h:
        await h.http.post(
            "/v1/feedback", json={"task": "need xml support", "input_formats": ["xml"]}
        )
        assert (await h.http.get("/v1/demand")).status_code == 404
        assert (
            await h.http.get("/v1/demand", headers={"authorization": "Bearer wrong"})
        ).status_code == 404
        ok = await h.http.get("/v1/demand", headers={"authorization": "Bearer s3cret-token"})
    assert ok.status_code == 200
    assert ok.json()["capability_requests_total"] == 1


async def test_demand_report_is_off_without_a_token(free_app: kit.AppHarness) -> None:
    assert (
        await free_app.http.get("/v1/demand", headers={"authorization": "Bearer "})
    ).status_code == 404


async def test_query_joins_several_artifacts_for_one_query_price(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    gl, bank = await _convert(paid_app, GL, "gl.csv"), await _convert(paid_app, BANK, "bank.csv")
    before = len(facilitator.settled)
    r = await paid_app.paying.post(
        "/v1/query",
        json={
            "artifact_id": gl,
            "tables": {"bank": {"artifact_id": bank}},
            "sql": "SELECT d.txn_id, d.amount FROM data d ANTI JOIN bank b USING (txn_id) ORDER BY 1",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == [["T2", 250.5]]  # in the GL, missing from the bank
    assert [req.amount for _, req in facilitator.settled[before:]] == ["5000"]  # one query price


@pytest.mark.parametrize(
    "tables",
    [
        {"Bad-Name": {"artifact_id": "x" * 24}},
        {f"t{i}": {"artifact_id": "x" * 24} for i in range(9)},
    ],
)
async def test_query_table_names_and_count_are_validated(
    free_app: kit.AppHarness, tables: dict[str, Any]
) -> None:
    r = await free_app.http.post(
        "/v1/query", json={"artifact_id": "x" * 24, "tables": tables, "sql": "SELECT 1"}
    )
    assert r.status_code == 400


async def test_join_over_mcp(paid_app: kit.AppHarness) -> None:
    gl, bank = await _convert(paid_app, GL, "gl.csv"), await _convert(paid_app, BANK, "bank.csv")
    args = {
        "artifact_id": gl,
        "tables": {"bank": bank},
        "sql": "SELECT count(*) FROM data JOIN bank USING (txn_id)",
    }
    async with kit.mcp_over_http(paid_app.app) as client:
        unpaid = await client.call_tool("query_parquet", args)
        meta = await kit.mcp_payment_meta(kit.paying_client(), unpaid.structured_content)
        result = await client.call_tool("query_parquet", args, meta=meta)
        ack = await client.call_tool(
            "request_capability", {"request": {"task": "XML to Parquet please"}}
        )
    assert not result.is_error
    assert result.structured_content["rows"] == [[2]]
    assert not ack.is_error
    assert ack.structured_content["received"] is True


async def test_retention_policy_is_published_consistently(free_app: kit.AppHarness) -> None:
    index = (await free_app.http.get("/")).json()["data_retention"]
    pricing = (await free_app.http.get("/pricing")).json()["data_retention"]
    llms = (await free_app.http.get("/llms.txt")).text
    assert index == pricing
    assert "never used to train" in index["training"]
    assert f"{free_app.settings.artifact_ttl_s // 60} minutes" in index["outputs"]
    assert "request_capability" in llms
    assert "deleted when the call ends" in llms
