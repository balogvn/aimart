"""The MCP server as agents see it: tool sets per mode, schemas, and x402 over MCP on the wire.

Money invariant under test (payments/mcp_gate.py): verify -> run tool -> settle only when the
tool result is not an error. Payment-required results are decoded from ``structuredContent``
exactly as a client would; expected amounts are literals.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

import mcp
import pytest

import aimart_testkit as kit
from aimart_parquet.artifacts import LocalArtifactStore
from aimart_parquet.mcp.server import build_mcp
from aimart_parquet.payments import Payments
from aimart_parquet.payments.pricing import active_operations
from aimart_parquet.provenance import Signer, build_manifest
from aimart_parquet.service import Service

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mcp_types import CallToolResult

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

CSV = b"region,units,price\nNorth,10,1.5\nSouth,20,2.5\nEast,30,3.25\n"
UNKNOWN_ARTIFACT = "Zz" * 12
PAID_TOOLS = list(dict.fromkeys(op.tool for op in active_operations()))
TOOL_ATOMIC = {
    "convert_to_parquet": "20000",
    "inspect_parquet": "2000",
    "preview_parquet": "2000",
    "query_parquet": "5000",
    "quantum_compress_features": "500000",
    "quantum_optimize_portfolio": "1000000",
}
TOOL_PRICE = {
    "convert_to_parquet": "$0.02",
    "inspect_parquet": "$0.002",
    "preview_parquet": "$0.002",
    "query_parquet": "$0.005",
    "quantum_compress_features": "$0.50",
    "quantum_optimize_portfolio": "$1.00",
}
LOCAL_ONLY_ARGS = {"path", "output_path"}


def remote_args(tool: str, artifact_id: str = UNKNOWN_ARTIFACT) -> dict[str, Any]:
    return {
        "convert_to_parquet": {"content_base64": kit.b64(CSV), "filename": "sales.csv"},
        "inspect_parquet": {"artifact_id": artifact_id},
        "preview_parquet": {"artifact_id": artifact_id, "limit": 2},
        "query_parquet": {"artifact_id": artifact_id, "sql": "SELECT sum(units) AS u FROM data"},
        "quantum_compress_features": {"artifact_id": artifact_id},
        "quantum_optimize_portfolio": {"artifact_id": artifact_id},
    }[tool]


def make_service(settings: Settings, *, local: bool) -> Service:
    store = LocalArtifactStore(
        settings.artifact_dir,
        ttl_s=settings.artifact_ttl_s,
        max_total_bytes=settings.artifact_max_total_bytes,
        secret=b"k",
    )
    return Service(settings, store, local_mode=local, signer=None if local else Signer(None))


def text_json(result: CallToolResult) -> Any:
    first = result.content[0]
    assert first.type == "text"
    return json.loads(first.text)


# ------------------------------------------------------------------------------ tool surface
async def test_remote_tool_set_and_schemas(settings: Settings) -> None:
    server = build_mcp(make_service(settings, local=False), settings, local_mode=False)
    async with mcp.Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {*PAID_TOOLS, "verify_provenance"}
    for name, tool in tools.items():
        props = set(tool.input_schema.get("properties", {}))
        assert not props & LOCAL_ONLY_ARGS, f"{name} exposes a server path to remote callers"
        assert tool.output_schema is not None, name
        assert tool.output_schema.get("type") == "object", name
        assert tool.description
    for name in PAID_TOOLS:
        assert f"Price: {TOOL_PRICE[name]} USDC per successful call" in tools[name].description
    assert "Free." in tools["verify_provenance"].description
    assert set(tools["convert_to_parquet"].input_schema["properties"]) >= {
        "url",
        "content_base64",
        "filename",
        "options",
    }
    assert tools["query_parquet"].input_schema["required"] == ["sql"]
    assert tools["inspect_parquet"].annotations.read_only_hint is True
    assert tools["convert_to_parquet"].annotations.idempotent_hint is False


async def test_local_tool_set_has_paths_and_no_verify(settings: Settings) -> None:
    server = build_mcp(make_service(settings, local=True), settings, local_mode=True)
    async with mcp.Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == set(PAID_TOOLS)
    assert "verify_provenance" not in tools
    for name, tool in tools.items():
        assert "path" in tool.input_schema["properties"], name
        assert tool.output_schema is not None, name
        assert "Free in local mode." in tool.description, name
        assert "USDC per successful call" not in tool.description, name
    assert "output_path" in tools["convert_to_parquet"].input_schema["properties"]


async def test_remote_caller_cannot_smuggle_a_server_path(
    settings: Settings, tmp_path: Path
) -> None:
    secret = kit.write(tmp_path / "secret.csv", "a,b\n1,2\n")
    server = build_mcp(make_service(settings, local=False), settings, local_mode=False)
    async with mcp.Client(server) as client:
        result = await client.call_tool("convert_to_parquet", {"path": str(secret)})
    assert result.is_error
    assert result.structured_content["error"]["code"] == "invalid_request"


# ------------------------------------------------------------------------------ local mode (free)
async def test_local_mode_converts_inspects_previews_and_queries_by_path(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    settings = make_settings(hosted_url="https://hosted.example")
    src = kit.write(tmp_path / "data" / "sales.csv", CSV)
    server = build_mcp(make_service(settings, local=True), settings, local_mode=True)
    async with mcp.Client(server) as client:
        first = await client.call_tool("convert_to_parquet", {"path": str(src)})
        second = await client.call_tool("convert_to_parquet", {"path": str(src)})
        explicit = tmp_path / "out" / "named.parquet"
        third = await client.call_tool(
            "convert_to_parquet", {"path": str(src), "output_path": str(explicit)}
        )
        out = first.structured_content["artifact"]["path"]
        inspected = await client.call_tool("inspect_parquet", {"path": out})
        preview = await client.call_tool(
            "preview_parquet", {"path": out, "limit": 2, "format": "markdown"}
        )
        query = await client.call_tool(
            "query_parquet", {"path": out, "sql": "SELECT sum(units) AS u FROM data"}
        )
        missing = await client.call_tool("inspect_parquet", {"path": str(tmp_path / "nope")})

    assert not first.is_error, first.structured_content
    assert first.structured_content["artifact"]["id"] == "local"
    assert out == str(tmp_path / "data" / "sales.parquet")
    # Never overwrite a file the caller did not name.
    assert second.structured_content["artifact"]["path"] == str(
        tmp_path / "data" / "sales.1.parquet"
    )
    assert third.structured_content["artifact"]["path"] == str(explicit)
    assert explicit.is_file()
    assert "https://hosted.example" in first.structured_content["hint"]
    assert "provenance" not in first.structured_content["artifact"]
    assert text_json(first) == first.structured_content

    assert inspected.structured_content["rows"] == 3
    assert preview.structured_content["row_count"] == 2
    assert "| region |" in preview.structured_content["text"]
    assert query.structured_content["rows"] == [[60]]
    assert missing.is_error
    assert missing.structured_content["error"]["code"] == "invalid_request"


# ------------------------------------------------------------------------------ x402 over MCP
async def test_tools_list_and_initialize_are_free(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    async with kit.mcp_over_http(paid_app.app) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == {*PAID_TOOLS, "verify_provenance"}
    assert facilitator.verified == []
    assert facilitator.settled == []


@pytest.mark.parametrize("tool", PAID_TOOLS)
async def test_unpaid_tool_call_returns_payment_required(
    tool: str, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    async with kit.mcp_over_http(paid_app.app) as client:
        result = await client.call_tool(tool, remote_args(tool))
    assert result.is_error
    pr = result.structured_content
    assert text_json(result) == pr  # spec: the same JSON as the first text block
    assert pr["x402Version"] == 2
    assert pr["error"] == "Payment required"
    assert pr["resource"]["url"] == f"http://test/mcp#{tool}"
    [accept] = pr["accepts"]
    assert accept["amount"] == TOOL_ATOMIC[tool]
    assert accept["payTo"] == kit.PAY_TO_EVM
    assert accept["network"] == kit.BASE_SEPOLIA
    assert accept["asset"] == kit.USDC_BASE_SEPOLIA
    assert accept["extra"]["name"] == "USDC"
    assert facilitator.verified == []
    assert facilitator.settled == []


async def test_paid_tool_call_returns_the_result_and_the_settlement(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    payer = kit.paying_client()
    async with kit.mcp_over_http(paid_app.app) as client:
        args = remote_args("convert_to_parquet")
        unpaid = await client.call_tool("convert_to_parquet", args)
        meta = await kit.mcp_payment_meta(payer, unpaid.structured_content)
        paid = await client.call_tool("convert_to_parquet", args, meta=meta)

        artifact_id = paid.structured_content["artifact"]["id"]
        unpaid_query = await client.call_tool(
            "query_parquet", remote_args("query_parquet", artifact_id)
        )
        query_meta = await kit.mcp_payment_meta(payer, unpaid_query.structured_content)
        query = await client.call_tool(
            "query_parquet", remote_args("query_parquet", artifact_id), meta=query_meta
        )

    assert not paid.is_error, paid.structured_content
    assert paid.structured_content["report"]["rows"] == 3
    assert paid.structured_content["artifact"]["provenance"]["algorithm"] == "ML-DSA-65"
    links = [c for c in paid.content if c.type == "resource_link"]
    assert links
    assert links[0].uri == paid.structured_content["artifact"]["download_url"]
    settlement = paid.meta["x402/payment-response"]
    assert settlement["success"] is True
    assert settlement["transaction"] == kit.TX_HASH
    assert settlement["network"] == kit.BASE_SEPOLIA

    assert not query.is_error
    assert query.structured_content["rows"] == [[60]]
    assert query.meta["x402/payment-response"]["success"] is True

    assert [req.amount for _, req in facilitator.settled] == ["20000", "5000"]
    assert [req.pay_to for _, req in facilitator.settled] == [kit.PAY_TO_EVM] * 2
    assert len(facilitator.verified) == 2


FAILING_CALLS = [
    ("convert_to_parquet", {"url": "http://127.0.0.1/a.csv"}, "url_not_allowed"),
    ("convert_to_parquet", {}, "invalid_request"),
    ("inspect_parquet", {"artifact_id": UNKNOWN_ARTIFACT}, "artifact_not_found"),
    ("preview_parquet", {"artifact_id": UNKNOWN_ARTIFACT}, "artifact_not_found"),
    ("query_parquet", {"artifact_id": "ARTIFACT", "sql": "DELETE FROM data"}, "query_rejected"),
    ("query_parquet", {"artifact_id": UNKNOWN_ARTIFACT, "sql": "SELECT 1"}, "artifact_not_found"),
    ("quantum_compress_features", {"artifact_id": UNKNOWN_ARTIFACT}, "artifact_not_found"),
    ("quantum_optimize_portfolio", {"artifact_id": UNKNOWN_ARTIFACT}, "artifact_not_found"),
]


@pytest.mark.parametrize(
    ("tool", "args", "code"),
    [c for c in FAILING_CALLS if c[0] in PAID_TOOLS],
    ids=[f"{c[0]}-{c[2]}" for c in FAILING_CALLS if c[0] in PAID_TOOLS],
)
async def test_paid_tool_call_that_fails_is_never_settled(
    tool: str,
    args: dict[str, Any],
    code: str,
    paid_app: kit.AppHarness,
    facilitator: kit.FakeFacilitator,
) -> None:
    if "ARTIFACT" in args.values():
        from aimart_parquet.engine.models import ConvertOptions
        from aimart_parquet.service import SourceRef

        converted = await paid_app.app.state.service.convert(
            SourceRef(content_base64=kit.b64(CSV), filename="s.csv"), ConvertOptions()
        )
        args = {k: converted.artifact.id if v == "ARTIFACT" else v for k, v in args.items()}
    async with kit.mcp_over_http(paid_app.app) as client:
        unpaid = await client.call_tool(tool, args)
        meta = await kit.mcp_payment_meta(kit.paying_client(), unpaid.structured_content)
        result = await client.call_tool(tool, args, meta=meta)
    assert result.is_error
    assert result.structured_content["error"]["code"] == code
    assert "x402/payment-response" not in (result.meta or {})
    assert len(facilitator.verified) == 1  # the payment was real and verified...
    assert facilitator.settled == []  # ...but failed work is never charged


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"x402Version": 2, "junk": True}, "Invalid payment payload"),
        ("not-an-object", "Invalid payment payload"),
        (
            {"x402Version": 1, "scheme": "exact", "network": "base-sepolia", "payload": {}},
            "x402 v1 payment payloads are not accepted",
        ),
    ],
    ids=["missing-fields", "not-an-object", "v1"],
)
async def test_malformed_payment_is_answered_with_payment_required(
    raw: Any, message: str, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    async with kit.mcp_over_http(paid_app.app) as client:
        result = await client.call_tool(
            "inspect_parquet", remote_args("inspect_parquet"), meta={"x402/payment": raw}
        )
    assert result.is_error
    assert result.structured_content["error"].startswith(message)
    assert result.structured_content["accepts"][0]["amount"] == "2000"
    assert facilitator.verified == []
    assert facilitator.settled == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("amount", "1"), ("payTo", kit.PAYER), ("network", "eip155:1"), ("asset", "0x" + "00" * 20)],
)
async def test_tampered_accepted_requirement_is_refused_before_verify(
    field: str, value: str, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    async with kit.mcp_over_http(paid_app.app) as client:
        args = remote_args("convert_to_parquet")
        unpaid = await client.call_tool("convert_to_parquet", args)
        meta = await kit.mcp_payment_meta(kit.paying_client(), unpaid.structured_content)
        meta["x402/payment"]["accepted"][field] = value
        result = await client.call_tool("convert_to_parquet", args, meta=meta)
    assert result.is_error
    assert result.structured_content["error"].startswith("Payment does not match")
    assert "artifact" not in result.structured_content
    assert facilitator.verified == []
    assert facilitator.settled == []


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ({"valid": False}, "Payment invalid: insufficient_funds"),
        ({"verify_error": RuntimeError("facilitator 500")}, "Payment verification failed"),
    ],
    ids=["invalid", "verify-raises"],
)
async def test_unverified_payment_runs_no_tool(
    setup: dict[str, Any], message: str, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    async with kit.mcp_over_http(paid_app.app) as client:
        args = remote_args("convert_to_parquet")
        unpaid = await client.call_tool("convert_to_parquet", args)
        meta = await kit.mcp_payment_meta(kit.paying_client(), unpaid.structured_content)
        for key, value in setup.items():
            setattr(facilitator, key, value)
        result = await client.call_tool("convert_to_parquet", args, meta=meta)
    assert result.is_error
    assert result.structured_content["error"].startswith(message)
    assert "artifact" not in result.structured_content
    assert len(facilitator.verified) == 1
    assert facilitator.settled == []
    # No conversion ran: the store holds no artifact folders.
    root = paid_app.settings.artifact_dir
    assert [p for p in root.iterdir() if not p.name.startswith(".")] == []


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ({"settle_ok": False}, "insufficient_funds"),
        ({"settle_error": RuntimeError("nonce reused")}, "nonce reused"),
    ],
    ids=["settle-false", "settle-raises"],
)
async def test_settlement_failure_withholds_the_tool_output(
    setup: dict[str, Any], reason: str, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    async with kit.mcp_over_http(paid_app.app) as client:
        args = remote_args("convert_to_parquet")
        unpaid = await client.call_tool("convert_to_parquet", args)
        meta = await kit.mcp_payment_meta(kit.paying_client(), unpaid.structured_content)
        for key, value in setup.items():
            setattr(facilitator, key, value)
        result = await client.call_tool("convert_to_parquet", args, meta=meta)
    assert result.is_error
    body = result.structured_content
    assert body["error"] == f"Payment settlement failed: {reason}"
    assert body["x402/payment-response"]["success"] is False
    assert body["x402/payment-response"]["network"] == kit.BASE_SEPOLIA
    assert body["accepts"][0]["amount"] == "20000"
    assert "artifact" not in body
    assert "report" not in body
    assert all(c.type == "text" for c in result.content)  # no download link leaks either
    assert len(facilitator.settled) == 1


async def test_verify_provenance_is_free_over_paid_http(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    signer = paid_app.app.state.service.signer
    manifest = build_manifest(
        artifact_id=UNKNOWN_ARTIFACT,
        sha256="0" * 64,
        size_bytes=1,
        filename="x",
        operation="convert",
    )
    signature = signer.sign(manifest)
    async with kit.mcp_over_http(paid_app.app) as client:
        good = await client.call_tool(
            "verify_provenance", {"manifest": manifest, "signature": signature}
        )
        tampered = await client.call_tool(
            "verify_provenance", {"manifest": manifest | {"size_bytes": 2}, "signature": signature}
        )
        missing = await client.call_tool(
            "verify_provenance",
            {"manifest": manifest, "signature": signature, "artifact_id": UNKNOWN_ARTIFACT},
        )
    assert not good.is_error
    assert good.structured_content["valid"] is True
    assert good.structured_content["algorithm"] == "ML-DSA-65"
    assert tampered.structured_content["valid"] is False
    assert tampered.structured_content["problems"]
    assert missing.is_error
    assert missing.structured_content["error"]["code"] == "artifact_not_found"
    assert facilitator.verified == []
    assert facilitator.settled == []
    assert base64.b64decode(signature)


def test_gate_settings_advertise_one_price_per_tool(settings: Settings) -> None:
    gate = Payments(settings, [kit.FakeFacilitator()]).mcp_extension()
    advertised = gate.settings()
    assert advertised["x402Version"] == 2
    assert advertised["networks"] == [kit.BASE_SEPOLIA]
    assert advertised["prices"] == {t: TOOL_PRICE[t] for t in PAID_TOOLS}


# ------------------------------------------------------------------------------ premium tools
quantum_only = pytest.mark.skipif(
    "quantum_compress_features" not in PAID_TOOLS, reason="quantum extra not installed"
)


def _features_parquet(path: Path, features: int) -> Path:
    import math

    rows = 40
    return kit.write_parquet(
        path,
        {
            f"asset_{j}": [100 + 5 * math.sin(d / (j + 2)) + d * 0.1 * j for d in range(rows)]
            for j in range(features)
        },
    )


@quantum_only
async def test_local_premium_tools_are_free_and_keep_the_honesty_note(
    settings: Settings, tmp_path: Path
) -> None:
    from aimart_parquet.quantum.models import SIMULATOR_NOTE

    src = _features_parquet(tmp_path / "prices.parquet", 10)  # > max_qubits: pre-reduced
    out = tmp_path / "out" / "q.parquet"
    server = build_mcp(make_service(settings, local=True), settings, local_mode=True)
    async with mcp.Client(server) as client:
        compressed = await client.call_tool(
            "quantum_compress_features",
            {
                "path": str(src),
                "options": {"components": 2, "landmarks": 16},
                "output_path": str(out),
            },
        )
        portfolio = await client.call_tool(
            "quantum_optimize_portfolio",
            {
                "path": str(src),
                "options": {"assets": ["asset_0", "asset_1", "asset_2"], "budget": 2, "steps": 10},
            },
        )
        bad = await client.call_tool(
            "quantum_optimize_portfolio", {"path": str(src), "options": {"assets": ["nope"]}}
        )
    assert not compressed.is_error, compressed.content
    assert compressed.structured_content["notes"][0] == SIMULATOR_NOTE
    assert compressed.structured_content["artifact"]["path"] == str(out)
    assert out.is_file()
    assert not portfolio.is_error, portfolio.content
    assert portfolio.structured_content["notes"][0] == SIMULATOR_NOTE
    assert len(portfolio.structured_content["classical_optimum"]["assets"]) == 2
    assert bad.is_error
    assert bad.structured_content["error"]["code"] == "invalid_request"


@quantum_only
async def test_remote_premium_tools_return_a_signed_artifact_and_a_link(
    settings: Settings, tmp_path: Path
) -> None:
    service = make_service(settings, local=False)
    art = service.store.put(_features_parquet(tmp_path / "f.parquet", 10), "features.parquet")
    server = build_mcp(service, settings, local_mode=False)
    async with mcp.Client(server) as client:
        compressed = await client.call_tool(
            "quantum_compress_features",
            {"artifact_id": art.id, "options": {"components": 2, "landmarks": 16}},
        )
        portfolio = await client.call_tool(
            "quantum_optimize_portfolio",
            {
                "artifact_id": art.id,
                "options": {"assets": ["asset_0", "asset_1", "asset_2"], "budget": 2, "steps": 10},
            },
        )
    assert not compressed.is_error, compressed.content
    artifact = compressed.structured_content["artifact"]
    assert artifact["provenance"]["manifest"]["operation"] == "quantum_compress"
    links = [c for c in compressed.content if c.type == "resource_link"]
    assert [link.uri for link in links] == [artifact["download_url"]]
    assert not portfolio.is_error, portfolio.content
    assert 0 < portfolio.structured_content["approximation_ratio"] <= 1.0 + 1e-9


@quantum_only
async def test_compress_over_mcp_without_pre_reduction(settings: Settings, tmp_path: Path) -> None:
    src = _features_parquet(tmp_path / "few.parquet", 4)  # <= max_qubits: no pre-reduction
    server = build_mcp(make_service(settings, local=True), settings, local_mode=True)
    async with mcp.Client(server) as client:
        result = await client.call_tool(
            "quantum_compress_features",
            {"path": str(src), "options": {"components": 2, "landmarks": 16}},
        )
    assert not result.is_error, result.content
    assert (
        result.structured_content["method"].get("pre_reduction") is None
    )  # null fields are omitted


def test_no_mcp_output_model_has_a_required_nullable_field() -> None:
    """_ok() drops None values, so a field that may be None must also have a default."""
    import types
    import typing

    from pydantic import BaseModel

    from aimart_parquet.engine import models as engine_models
    from aimart_parquet.mcp import server as mcp_server
    from aimart_parquet.quantum import models as quantum_models

    seen: set[type[BaseModel]] = set()
    offenders: list[str] = []

    def walk(model: type[BaseModel]) -> None:
        if model in seen:
            return
        seen.add(model)
        for name, field in model.model_fields.items():
            args = typing.get_args(field.annotation)
            if field.is_required() and type(None) in args:
                offenders.append(f"{model.__name__}.{name}")
            for arg in args or (field.annotation,):
                for inner in typing.get_args(arg) or (arg,):
                    if isinstance(inner, type) and issubclass(inner, BaseModel):
                        walk(inner)
                    elif isinstance(inner, types.GenericAlias):
                        continue

    for model in (
        engine_models.ConvertResult,
        engine_models.InspectResult,
        engine_models.TabularResult,
        quantum_models.QuantumCompressResult,
        quantum_models.PortfolioResult,
        mcp_server.VerifyResult,
    ):
        walk(model)
    assert len(seen) > 10
    assert offenders == []


async def test_paid_tool_that_crashes_inside_the_sdk_is_not_settled(
    paid_app: kit.AppHarness,
    facilitator: kit.FakeFacilitator,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A failure *outside* the tool's own error handling — here the SDK rejecting structured
    # output that does not match the declared outputSchema — must still never be charged.
    from pydantic import BaseModel

    class WrongShape(BaseModel):
        unexpected: int = 1

    async def bad_inspect(*_: object, **__: object) -> WrongShape:
        return WrongShape()

    monkeypatch.setattr(paid_app.app.state.service, "inspect", bad_inspect)
    args = {"artifact_id": "x" * 24}
    async with kit.mcp_over_http(paid_app.app) as client:
        unpaid = await client.call_tool("inspect_parquet", args)
        meta = await kit.mcp_payment_meta(kit.paying_client(), unpaid.structured_content)
        result = await client.call_tool("inspect_parquet", args, meta=meta)
    assert result.is_error
    assert len(facilitator.verified) == 1
    assert facilitator.settled == []
