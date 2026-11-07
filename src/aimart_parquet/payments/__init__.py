"""x402 payments: one resource server shared by the REST paywall and the MCP paywall.

Money flow per paid call: the caller signs a USDC transfer authorization for the quoted price
to our payTo address; we ask the facilitator to *verify* it before doing any work; we run the
operation; only if it succeeded do we ask the facilitator to *settle* (move the funds). A
failed operation is never settled, so a caller is never charged for an error.

Two rules on either side of that: a request that cannot succeed (an invalid body or tool
arguments, a tool this deployment does not serve) is refused before any price is quoted, and
an output whose result is withheld (settlement failed, or the call failed after storing it)
is deleted, because nobody paid for it and it would otherwise evict paying customers' files.

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

from aimart_parquet.artifacts import discard_withheld, outputs_of
from aimart_parquet.config import PAYMENT_WINDOW_S, SETTLE_MARGIN_S, NetworkMode, Settings
from aimart_parquet.errors import AimartError
from aimart_parquet.payments.bazaar import EXAMPLES
from aimart_parquet.payments.mcp_gate import X402McpGate, payments_unavailable
from aimart_parquet.payments.pricing import Operation, active_operations, usd_to_atomic

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Request, Response
    from x402 import FacilitatorClient
    from x402.schemas import PaymentRequirements

    from aimart_parquet.artifacts import ArtifactStore

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

# Where a paying client puts its signed payload (v2, then v1): what the SDK middleware reads.
PAYMENT_HEADERS = ("payment-signature", "x-payment")

SERVICE_NAME = "AImart Parquet"  # <= 32 ASCII chars, or facilitator catalogs drop it
TAGS = ["data", "parquet", "etl", "csv", "mcp"]  # <= 5 tags


def carries_payment(request: Request) -> bool:
    return any(request.headers.get(name) for name in PAYMENT_HEADERS)


def facilitator_urls(settings: Settings) -> list[str]:
    if settings.facilitator_urls:
        return [u.strip() for u in settings.facilitator_urls.split(",") if u.strip()]
    return list(DEFAULT_FACILITATORS[settings.network_mode])


class ClientDisconnected(AimartError):
    """The caller hung up before its response was ready: nobody receives it, so it is unpaid.

    499 is nginx's "client closed request"; no standard status says it, and nobody reads it
    but the access log.
    """

    code = "client_disconnected"
    http_status = 499


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
        self._check_payment_window()
        self._initialized = False
        self._init_lock = anyio.Lock()
        self._mcp_requirements: dict[str, list[PaymentRequirements]] = {}
        # Only what this deployment serves: the quantum tools (off by default) used to be
        # quoted at $0.50/$1.00 and verified, and only then answered "Unknown tool".
        self._by_tool: dict[str, Operation] = {}
        for op in active_operations(settings):
            self._by_tool.setdefault(op.tool, op)

    def _check_payment_window(self) -> None:
        """Refuse to start when a paid call could outlast its own payment authorization.

        ``queue_wait_s`` was never read, and a call queued behind 4 x 180 s of jobs ran its work
        after the authorization expired: unsettleable, so served for free. ``fetch_timeout_s``
        counts once because it bounds all of a call's downloads together
        (``sources.downloads_deadline``, opened by ``Service._job``): a query over `data` and 7
        URL tables used to get one each, 480 s at the defaults. Unset, the queue wait is what
        the window leaves each operation (``Settings.queue_wait_for``), so only the job
        timeouts and the download allowance can fail this; set, it must fit the longest job.
        """
        s = self.settings
        longest = max(
            s.convert_timeout_s,
            s.query_timeout_s + 5,  # Service.query's outer deadline
            s.quantum_timeout_s if s.enable_quantum else 0.0,
        )
        needed = s.queue_wait_for(longest) + s.fetch_timeout_s + longest
        if needed + SETTLE_MARGIN_S > PAYMENT_WINDOW_S:
            msg = (
                f"queue_wait_s + fetch_timeout_s + the longest operation timeout is {needed:g} s; "
                f"with {SETTLE_MARGIN_S} s to verify and settle it exceeds the {PAYMENT_WINDOW_S} s "
                "a payment authorization is valid for, so paid calls could not settle. Lower "
                "AIMART_QUEUE_WAIT_S, AIMART_FETCH_TIMEOUT_S or the operation timeouts."
            )
            raise ValueError(msg)

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
            for op in active_operations(self.settings):
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
        for op in active_operations(self.settings):
            example = EXAMPLES[op.key]
            routes[op.route] = RouteConfig(
                accepts=[
                    PaymentOption(
                        scheme="exact",
                        price=op.price(self.settings),
                        network=network,
                        pay_to=pay_to,
                        max_timeout_seconds=PAYMENT_WINDOW_S,
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
        *,
        prevalidate: Callable[[Request, bool], Awaitable[Response | None]] | None = None,
        store: ArtifactStore | None = None,
    ) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
        """The SDK middleware, built once, behind our lazy initialization.

        The SDK verifies before the route and settles after a < 400 response. With
        ``sync_facilitator_on_start=False`` it never initializes the resource server itself, so
        paid paths initialize here first; a facilitator outage becomes a 503 (nothing charged).

        ``prevalidate(request, paying)`` answers a request that can never succeed (an invalid
        body) before the SDK quotes a price for it or verifies its payment. Artifacts a paid
        request stores are committed to ``store`` when its final status is < 400, and deleted
        otherwise: a failed settlement turns the route's 200 into a 402 and withholds the body,
        so that output was never paid for and must not stay, or have evicted anyone, in the
        artifact budget.

        A paid response whose client has disconnected becomes a 499 before the SDK settles it:
        the SDK settled every response below 400, so a caller that hung up while its call ran
        (its own timeout, or the front end's 504 at Cloud Run's request timeout) was charged
        for a response it never received. A disconnect during the settlement itself still
        settles; the request timeout is the payment window, past which nothing can.
        """
        inner = payment_middleware(self.routes(), self.server, sync_facilitator_on_start=False)
        paid = {(op.method, op.path) for op in active_operations(self.settings)}

        def withhold(produced: list[str], reason: str) -> None:
            if store is not None and produced:
                discard_withheld(store, list(produced), reason=reason)

        async def middleware(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            if (request.method, request.url.path) not in paid:
                return await inner(request, call_next)
            if prevalidate is not None:
                rejected = await prevalidate(request, carries_payment(request))
                if rejected is not None:
                    return rejected
            try:
                await self.ensure_initialized()
            except Exception:
                log.exception("facilitator initialization failed")
                unavailable = payments_unavailable()
                return JSONResponse(unavailable.to_dict(), status_code=unavailable.http_status)

            async def served(req: Request) -> Response:
                response = await call_next(req)
                if response.status_code < 400 and await req.is_disconnected():
                    gone = ClientDisconnected(
                        "The client disconnected before the response was ready.",
                        hint="Retry the call; you were not charged.",
                    )
                    log.warning("paid response withheld: the client disconnected")
                    return JSONResponse(gone.to_dict(), status_code=gone.http_status)
                return response

            with outputs_of(store) as produced:
                try:
                    response = await inner(request, served)
                except BaseException:
                    withhold(produced, "handler_raised")
                    raise
            if response.status_code >= 400:
                withhold(produced, f"http_{response.status_code}")
            elif store is not None and produced:
                store.commit(produced)
            return response

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
                        max_timeout_seconds=PAYMENT_WINDOW_S,
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

    def mcp_extension(self, *, store: ArtifactStore | None = None) -> X402McpGate:
        return X402McpGate(self, store=store)

    def tool_operation(self, tool: str) -> Operation | None:
        """The paid operation behind an MCP tool, if this deployment serves it."""
        return self._by_tool.get(tool)

    # ------------------------------------------------------------------ discovery
    def discovery_document(self) -> dict[str, Any]:
        """x402scan's /.well-known/x402 compatibility format (not part of the x402 spec)."""
        return {
            "version": 1,
            "resources": [self.resource_url(op) for op in active_operations(self.settings)],
            "instructions": (
                "POST JSON (see /openapi.json). Unpaid requests get HTTP 402 with a base64 "
                "PAYMENT-REQUIRED header; retry with PAYMENT-SIGNATURE. MCP: /mcp, pay per tool "
                "call via _meta['x402/payment']."
            ),
        }
