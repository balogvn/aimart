"""What x402 indexes and registries read about the service says what the service does.

Registering the live service with x402scan on 2026-11-16 listed /pricing as a failed endpoint:
it is free, but its OpenAPI operation declared no `security`, so the index probed it for a 402.
The discovery profile x402scan and x402gle follow wants every operation to declare its auth
(`[]` with x-payment-info means paid), a one-line `info.x-guidance` and a link to the longer
guide. And the registry entry (server.json) advertised testnet and Solana rails the hosted
remote does not take, under a placeholder remote URL.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import aimart_testkit as kit
from aimart_parquet.api.discovery import GUIDANCE

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[2]
HOSTED = "https://balogvn--aimart-parquet.us-east.modal.run"


async def test_every_free_operation_says_so_and_every_paid_one_is_priced(
    paid_app: kit.AppHarness,
) -> None:
    """An operation with neither `security: []` nor x-payment-info is one an index probes for a
    402 (x402scan's rule: agentcash discovery, inferAuthMode). Paid operations keep no
    `security` key, as they had when x402scan registered all of them (test_api.py pins that)."""
    spec = (await paid_app.http.get("/openapi.json")).json()
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            paid = path.startswith(
                ("/v1/convert", "/v1/inspect", "/v1/preview", "/v1/query", "/v1/quantum")
            )
            assert ("x-payment-info" in operation) == paid, (method, path)
            if paid:
                assert "402" in operation["responses"], (method, path)
            else:
                assert operation.get("security") == [], (method, path)  # before: /pricing


async def test_the_openapi_document_carries_the_guidance_indexes_show(
    paid_app: kit.AppHarness,
) -> None:
    spec = (await paid_app.http.get("/openapi.json")).json()
    assert spec["info"]["x-guidance"] == GUIDANCE
    llms = spec["x-agentcash-guidance"]["llmsTxtUrl"]
    assert llms == f"{paid_app.settings.public_base_url}/llms.txt"
    assert (
        await paid_app.http.get(llms.removeprefix(paid_app.settings.public_base_url))
    ).is_success
    assert spec == (await paid_app.http.get("/openapi.json")).json()  # built once, then served


def test_guidance_names_only_routes_the_service_has() -> None:
    assert "POST /v1/convert" in GUIDANCE
    assert "/llms.txt" in GUIDANCE
    assert len(GUIDANCE) < 300  # one line for an index, not a guide


def test_the_registry_entry_names_the_hosted_remote_and_only_the_rail_it_takes() -> None:
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert [r["url"] for r in manifest["remotes"]] == [f"{HOSTED}/mcp"]
    x402 = manifest["_meta"]["io.modelcontextprotocol.registry/publisher-provided"]["x402"]
    assert x402["networks"] == {"mainnet": [kit.BASE_MAINNET]}  # before: testnet and Solana too
    meta = json.dumps(manifest["_meta"]["io.modelcontextprotocol.registry/publisher-provided"])
    assert len(meta.encode()) < 4096  # the registry's limit for publisher-provided metadata
