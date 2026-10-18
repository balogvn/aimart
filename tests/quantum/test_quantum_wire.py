"""Wire-level tests for the premium tier: what a paying agent actually receives."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

import anyio
import httpx
from eth_account import Account
from x402 import x402Client
from x402.http.clients.httpx import x402AsyncTransport
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact import ExactEvmClientScheme
from x402.schemas import SettleResponse, SupportedKind, SupportedResponse, VerifyResponse

from aimart_parquet.api.app import create_app
from aimart_parquet.config import Settings
from aimart_parquet.payments import Payments
from aimart_parquet.payments.pricing import active_operations, usd_to_atomic
from aimart_parquet.quantum.models import SIMULATOR_NOTE

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from fastapi import FastAPI

NETWORK = "eip155:84532"
PAY_TO = "0x209693Bc6afc0C5328bA36FaF03C514EF312287C"


class FakeFacilitator:
    def __init__(self) -> None:
        self.verified: list[str] = []
        self.settled: list[str] = []

    def get_supported(self) -> SupportedResponse:
        return SupportedResponse(
            kinds=[SupportedKind(x402_version=2, scheme="exact", network=NETWORK)],
            extensions=[],
            signers={},
        )

    async def verify(self, payload: Any, requirements: Any) -> VerifyResponse:
        self.verified.append(requirements.amount)
        return VerifyResponse(is_valid=True, payer="0xabc")

    async def settle(self, payload: Any, requirements: Any) -> SettleResponse:
        self.settled.append(requirements.amount)
        return SettleResponse(
            success=True,
            transaction="0x" + "ab" * 32,
            network=requirements.network,
            payer="0xabc",
            amount=requirements.amount,
        )


def _app(tmp_path: Path, seed: str) -> tuple[FastAPI, FakeFacilitator, Settings]:
    settings = Settings(
        pay_to_evm=PAY_TO,
        artifact_dir=tmp_path / "art",
        public_base_url="http://test",
        log_json=False,
        log_level="WARNING",
        signing_key_mldsa=seed,
        isolate_jobs=False,
        enable_quantum=True,
    )
    fake = FakeFacilitator()
    return create_app(settings, payments=Payments(settings, [fake])), fake, settings


def _paying_client() -> x402Client:
    client = x402Client()
    client.register(NETWORK, ExactEvmClientScheme(EthAccountSigner(Account.create())))
    return client


def _decode(header: str) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads(base64.b64decode(header))
    return decoded


def test_quantum_operations_are_advertised_when_installed() -> None:
    keys = {op.key for op in active_operations()}
    assert {"quantum_compress", "quantum_portfolio"} <= keys


def test_quantum_operations_hidden_when_extra_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from aimart_parquet import quantum

    monkeypatch.setattr(quantum, "available", lambda: False)
    keys = {op.key for op in active_operations()}
    assert "quantum_compress" not in keys
    assert "quantum_portfolio" not in keys


def test_unpaid_quantum_calls_quote_the_configured_price_on_the_wire(
    tmp_path: Path, signing_seed: str
) -> None:
    app, fake, settings = _app(tmp_path, signing_seed)

    async def run() -> None:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as h,
        ):
            for path, price in (
                ("/v1/quantum/compress", settings.price_quantum_compress),
                ("/v1/quantum/portfolio", settings.price_quantum_portfolio),
            ):
                r = await h.post(path, json={"artifact_id": "x" * 24})
                assert r.status_code == 402
                accepts = _decode(r.headers["payment-required"])["accepts"]
                assert [a["amount"] for a in accepts] == [str(usd_to_atomic(price))]
                assert accepts[0]["payTo"] == PAY_TO
            spec = (await h.get("/openapi.json")).json()
            assert (
                spec["paths"]["/v1/quantum/portfolio"]["post"]["x-payment-info"]["price"]["amount"]
                == "1.00"
            )
        assert fake.settled == []

    anyio.run(run)


def test_paid_premium_flow_settles_once_and_keeps_the_honesty_note(
    tmp_path: Path, signing_seed: str
) -> None:
    app, fake, settings = _app(tmp_path, signing_seed)
    csv = "date,a,b,c,d\n" + "\n".join(
        f"2025-01-{d + 1:02d},{100 + d * 0.3:.2f},{50 + (d % 5):.2f},{80 - d * 0.1:.2f},{20 + (d % 3) * 0.5:.2f}"
        for d in range(28)
    )

    async def run() -> None:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=x402AsyncTransport(_paying_client(), httpx.ASGITransport(app=app)),
                base_url="http://test",
                timeout=120,
            ) as h,
        ):
            conv = (
                await h.post(
                    "/v1/convert",
                    json={
                        "content_base64": base64.b64encode(csv.encode()).decode(),
                        "filename": "p.csv",
                    },
                )
            ).json()
            aid = conv["artifact"]["id"]
            assert conv["artifact"]["provenance"]["algorithm"] == "ML-DSA-65"

            r = await h.post(
                "/v1/quantum/portfolio",
                json={"artifact_id": aid, "options": {"budget": 2, "steps": 15}},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["notes"][0] == SIMULATOR_NOTE
            assert _decode(r.headers["payment-response"])["amount"] == str(
                usd_to_atomic(settings.price_quantum_portfolio)
            )

            r = await h.post(
                "/v1/quantum/compress",
                json={"artifact_id": aid, "options": {"components": 2, "landmarks": 16}},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["notes"][0] == SIMULATOR_NOTE
            assert body["artifact"]["provenance"]["manifest"]["operation"] == "quantum_compress"

            # A failing paid call: verified, never settled.
            before = len(fake.settled)
            r = await h.post(
                "/v1/quantum/portfolio", json={"artifact_id": aid, "options": {"assets": ["nope"]}}
            )
            assert r.status_code == 400
            assert len(fake.settled) == before

        assert fake.settled == [
            str(usd_to_atomic(settings.price_convert)),
            str(usd_to_atomic(settings.price_quantum_portfolio)),
            str(usd_to_atomic(settings.price_quantum_compress)),
        ]

    anyio.run(run)


def test_verify_endpoint_is_free_and_detects_tampering(tmp_path: Path, signing_seed: str) -> None:
    app, fake, _ = _app(tmp_path, signing_seed)

    async def run() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=x402AsyncTransport(_paying_client(), httpx.ASGITransport(app=app)),
                base_url="http://test",
            ) as paid:
                conv = (
                    await paid.post(
                        "/v1/convert",
                        json={
                            "content_base64": base64.b64encode(b"a,b\n1,2\n3,4\n").decode(),
                            "filename": "t.csv",
                        },
                    )
                ).json()
            settled_after_convert = len(fake.settled)
            prov, aid = conv["artifact"]["provenance"], conv["artifact"]["id"]
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as h:
                key = (await h.get("/.well-known/aimart-signing-key")).json()
                assert key["algorithm"] == "ML-DSA-65"
                assert key["ephemeral"] is False
                ok = (
                    await h.post(
                        "/v1/verify",
                        json={
                            "manifest": prov["manifest"],
                            "signature": prov["signature"],
                            "artifact_id": aid,
                        },
                    )
                ).json()
                assert ok["valid"]
                assert ok["file_matches"]
                bad = (
                    await h.post(
                        "/v1/verify",
                        json={
                            "manifest": prov["manifest"] | {"sha256": "f" * 64},
                            "signature": prov["signature"],
                        },
                    )
                ).json()
                assert not bad["valid"]
            assert len(fake.settled) == settled_after_convert  # verification never charges

    anyio.run(run)
