"""x402 payments: one resource server shared by the REST paywall and the MCP paywall.

Money flow per paid call: the caller signs a USDC transfer authorization for the quoted price
to our payTo address; we ask the facilitator to *verify* it before doing any work; we run the
operation; only if it succeeded do we ask the facilitator to *settle* (move the funds). A
failed operation is never settled, so a caller is never charged for an error.

The server holds no private key. A facilitator submits the transfer and pays the gas.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import anyio
import anyio.to_thread
from fastapi.responses import JSONResponse
from x402 import x402ResourceServer
from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware
from x402.http.types import PaymentOption, RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.mechanisms.svm import SOLANA_DEVNET_CAIP2, SOLANA_MAINNET_CAIP2
from x402.mechanisms.svm.exact import ExactSvmServerScheme
from x402.schemas import ResourceInfo
from x402.schemas.config import ResourceConfig

from aimart_parquet.config import NetworkMode, Settings
from aimart_parquet.payments.bazaar import EXAMPLES
from aimart_parquet.payments.mcp_gate import X402McpGate
from aimart_parquet.payments.pricing import BY_TOOL, Operation, active_operations, usd_to_atomic

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Request, Response
    from x402 import FacilitatorClient
    from x402.schemas import PaymentRequirements

log = logging.getLogger("aimart.payments")

BASE_MAINNET = "eip155:8453"
BASE_SEPOLIA = "eip155:84532"

NETWORKS: dict[NetworkMode, dict[str, str]] = {
    NetworkMode.TESTNET: {"evm": BASE_SEPOLIA, "svm": SOLANA_DEVNET_CAIP2},
    NetworkMode.MAINNET: {"evm": BASE_MAINNET, "svm": SOLANA_MAINNET_CAIP2},
}

# Free, keyless facilitators. Mainnet order matters: the first facilitator supporting a
# (network, scheme) serves it. Heurist settles Base with standard EIP-3009 transfers (no
# Permit2 approval needed by the payer); Dexter settles Solana. See docs/adr/0003.
DEFAULT_FACILITATORS: dict[NetworkMode, tuple[str, ...]] = {
    NetworkMode.TESTNET: ("https://x402.org/facilitator",),
    NetworkMode.MAINNET: ("https://facilitator.heurist.xyz", "https://x402.dexter.cash"),
}

SERVICE_NAME = "AImart Parquet"  # <= 32 ASCII chars, or facilitator catalogs drop it
TAGS = ["data", "parquet", "etl", "csv", "mcp"]  # <= 5 tags


def facilitator_urls(settings: Settings) -> list[str]:
    if settings.facilitator_urls:
        return [u.strip() for u in settings.facilitator_urls.split(",") if u.strip()]
    return list(DEFAULT_FACILITATORS[settings.network_mode])


class Payments:
    def __init__(self, settings: Settings, facilitators: list[FacilitatorClient]) -> None:
        self.settings = settings
        self._facilitators = facilitators
        self.server = x402ResourceServer(facilitators)
        nets = NETWORKS[settings.network_mode]
        self.accepted: list[tuple[str, str]] = []
        if settings.pay_to_evm:
            # The SDK's own Protocol annotations disagree with its scheme classes (payment_flows).
            self.server.register(nets["evm"], ExactEvmServerScheme())  # type: ignore[no-untyped-call,arg-type]
            self.accepted.append((nets["evm"], settings.pay_to_evm))
        if settings.pay_to_svm:
            self.server.register(nets["svm"], ExactSvmServerScheme())  # type: ignore[arg-type]
            self.accepted.append((nets["svm"], settings.pay_to_svm))
        if not self.accepted:
            # Paid routes with nobody to pay would either crash or, worse, serve work for free.
            msg = (
                "payments are enabled but no payee is configured: set AIMART_PAY_TO_EVM and/or "
                "AIMART_PAY_TO_SVM (or AIMART_PAYMENTS_ENABLED=false for a free deployment)"
            )
            raise ValueError(msg)
        self._initialized = False
        self._init_lock = anyio.Lock()
        self._mcp_requirements: dict[str, list[PaymentRequirements]] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> Payments:
        clients: list[FacilitatorClient] = [
            HTTPFacilitatorClient(FacilitatorConfig(url=url)) for url in facilitator_urls(settings)
        ]
        return cls(settings, clients)

    # ------------------------------------------------------------------ lifecycle
    async def ensure_initialized(self) -> None:
        """Fetch facilitator capabilities once, lazily.

        Not at boot: a scale-to-zero host cold-starts on the first request, and a facilitator
        blip at that moment must cost one failed (unpaid) request, not a crash-looping server.
        """
        if self.is_initialized():
            return
        async with self._init_lock:
            if self.is_initialized():  # double-checked: another task may have finished meanwhile
                return
            await anyio.to_thread.run_sync(self.server.initialize)
            self._check_price_floors()
            self._initialized = True

    def _check_price_floors(self) -> None:
        for network, _ in self.accepted:
            kind = self.server.get_supported_kind(2, network, "exact")
            floor = (kind.extra or {}).get("minPaymentAmountAtomic") if kind else None
            if not floor:
                continue
            for op in active_operations():
                atomic = usd_to_atomic(op.price(self.settings))
                if atomic < int(floor):
                    log.error(
                        "price below facilitator minimum: these payments will be rejected",
                        extra={
                            "operation": op.key,
                            "network": network,
                            "price_atomic": atomic,
                            "floor_atomic": int(floor),
                        },
                    )

    def is_initialized(self) -> bool:
        return self._initialized

    async def aclose(self) -> None:
        for client in self._facilitators:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()

    # ------------------------------------------------------------------ shared config
    def networks(self) -> list[dict[str, str]]:
        return [
            {"network": n, "asset": "USDC", "payTo": a, "scheme": "exact"} for n, a in self.accepted
        ]

    def _extra(self) -> dict[str, str] | None:
        return {"tag": self.settings.bazaar_tag} if self.settings.bazaar_tag else None

    def resource_url(self, op: Operation, *, mcp: bool = False) -> str:
        base = self.settings.public_base_url
        return f"{base}/mcp#{op.tool}" if mcp else f"{base}{op.path}"

    # ------------------------------------------------------------------ REST paywall
    def routes(self) -> dict[str, RouteConfig]:
        routes: dict[str, RouteConfig] = {}
        for op in active_operations():
            example = EXAMPLES[op.key]
            routes[op.route] = RouteConfig(
                accepts=[
                    PaymentOption(
                        scheme="exact",
                        price=op.price(self.settings),
                        network=network,
                        pay_to=pay_to,
                        max_timeout_seconds=300,
                        extra=self._extra(),
                    )
                    for network, pay_to in self.accepted
                ],
                resource=self.resource_url(op),
                description=op.summary,
                mime_type="application/json",
                service_name=SERVICE_NAME,
                tags=TAGS,
                extensions=declare_discovery_extension(
                    input=example["input"],
                    input_schema=example["input_schema"],
                    body_type="form-data" if op.key == "convert_upload" else "json",
                    output=OutputConfig(example=example["output"]),
                ),
            )
        return routes

    def http_middleware(
        self,
    ) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
        """The SDK middleware, built once, behind our lazy initialization.

        The SDK verifies before the route and settles after a < 400 response. With
        ``sync_facilitator_on_start=False`` it never initializes the resource server itself, so
        paid paths initialize here first; a facilitator outage becomes a 503 (nothing charged).
        """
        inner = payment_middleware(self.routes(), self.server, sync_facilitator_on_start=False)
        paid = {(op.method, op.path) for op in active_operations()}

        async def middleware(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            if (request.method, request.url.path) in paid:
                try:
                    await self.ensure_initialized()
                except Exception:
                    log.exception("facilitator initialization failed")
                    return JSONResponse(
                        {
                            "error": {
                                "code": "payments_unavailable",
                                "message": "The payment facilitator is unreachable.",
                                "hint": "Retry shortly; you were not charged.",
                            }
                        },
                        status_code=503,
                    )
            return await inner(request, call_next)

        return middleware

    # ------------------------------------------------------------------ MCP paywall
    async def mcp_requirements(
        self, op: Operation
    ) -> tuple[list[PaymentRequirements], ResourceInfo]:
        await self.ensure_initialized()
        reqs = self._mcp_requirements.get(op.tool)
        if reqs is None:
            reqs = []
            for network, pay_to in self.accepted:
                reqs += self.server.build_payment_requirements(
                    ResourceConfig(
                        scheme="exact",
                        network=network,
                        pay_to=pay_to,
                        price=op.price(self.settings),
                        max_timeout_seconds=300,
                        extra=self._extra(),
                    )
                )
            self._mcp_requirements[op.tool] = reqs
        resource = ResourceInfo(
            url=self.resource_url(op, mcp=True),
            description=op.summary,
            mime_type="application/json",
        )
        return reqs, resource

    def mcp_extension(self) -> X402McpGate:
        return X402McpGate(self)

    def tool_operation(self, tool: str) -> Operation | None:
        return BY_TOOL.get(tool)

    # ------------------------------------------------------------------ discovery
    def discovery_document(self) -> dict[str, Any]:
        """x402scan's /.well-known/x402 compatibility format (not part of the x402 spec)."""
        return {
            "version": 1,
            "resources": [self.resource_url(op) for op in active_operations()],
            "instructions": (
                "POST JSON (see /openapi.json). Unpaid requests get HTTP 402 with a base64 "
                "PAYMENT-REQUIRED header; retry with PAYMENT-SIGNATURE. MCP: /mcp, pay per tool "
                "call via _meta['x402/payment']."
            ),
        }
