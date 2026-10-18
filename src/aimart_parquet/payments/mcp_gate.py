"""x402 for MCP tool calls, implementing the x402 v2 MCP transport on mcp 2.x.

The SDK's own ``x402.mcp`` helpers target mcp 1.x and break on 2.x (they import the removed
``mcp.server.fastmcp``), so this is a thin bridge over the core resource-server API.

Wire format (x402 specs/transports-v2/mcp.md):

* Unpaid call to a paid tool -> result ``isError: true``, ``structuredContent`` = the
  PaymentRequired object, and the same JSON as the first text block.
* The client retries with the signed payload in ``params._meta["x402/payment"]``.
* Success -> the tool's normal result plus ``_meta["x402/payment-response"]`` (the settlement).
* Settlement failure after the tool ran -> only the payment error; the tool's output is
  withheld, because it was not paid for.

Order of operations is the money invariant: verify -> run tool -> settle only if the tool
returned a non-error result. Free tools and protocol messages never touch this code.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import Extension
from mcp_types import CallToolResult, TextContent
from x402 import parse_payment_payload
from x402.schemas import PaymentPayload

from aimart_parquet.payments.pricing import active_operations

if TYPE_CHECKING:
    from x402.schemas import PaymentRequirements, ResourceInfo

    from aimart_parquet.payments import Payments

log = logging.getLogger("aimart.payments.mcp")

PAYMENT_META_KEY = "x402/payment"
PAYMENT_RESPONSE_META_KEY = "x402/payment-response"


def _result(body: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body, separators=(",", ":")))],
        structured_content=body,
        is_error=True,
    )


class X402McpGate(Extension):
    identifier = "io.github.balogvn/x402"

    def __init__(self, payments: Payments) -> None:
        super().__init__()
        self.payments = payments

    def settings(self) -> dict[str, Any]:
        prices: dict[str, str] = {}
        for op in active_operations(self.payments.settings):
            prices.setdefault(op.tool, op.price(self.payments.settings))
        return {
            "x402Version": 2,
            "networks": [n for n, _ in self.payments.accepted],
            "prices": prices,
        }

    async def _payment_required(
        self, reqs: list[PaymentRequirements], resource: ResourceInfo, error: str
    ) -> CallToolResult:
        pr = await self.payments.server.create_payment_required_response(
            reqs, resource, error=error
        )
        return _result(pr.model_dump(by_alias=True, exclude_none=True))

    async def intercept_tool_call(self, params: Any, ctx: Any, call_next: Any) -> Any:
        op = self.payments.tool_operation(params.name)
        if op is None:
            return await call_next(ctx)
        reqs, resource = await self.payments.mcp_requirements(op)
        raw = (params.meta or {}).get(PAYMENT_META_KEY)
        if raw is None:
            return await self._payment_required(reqs, resource, "Payment required")
        try:
            payload = parse_payment_payload(raw)
        except Exception as exc:  # noqa: BLE001 - malformed client input of any shape
            return await self._payment_required(reqs, resource, f"Invalid payment payload: {exc}")
        if not isinstance(payload, PaymentPayload):
            return await self._payment_required(
                reqs,
                resource,
                "x402 v1 payment payloads are not accepted over MCP; send x402Version 2.",
            )
        server = self.payments.server
        matched = server.find_matching_requirements(reqs, payload)
        if matched is None:
            return await self._payment_required(
                reqs,
                resource,
                "Payment does not match any accepted requirement (price, payTo, network or asset).",
            )
        try:
            verified = await server.verify_payment(payload, matched)
        except Exception as exc:  # noqa: BLE001 - any facilitator failure means "not paid"
            log.warning("verify failed", extra={"tool": params.name, "error": str(exc)})
            return await self._payment_required(
                reqs, resource, f"Payment verification failed: {exc}"
            )
        if not verified.is_valid:
            return await self._payment_required(
                reqs, resource, f"Payment invalid: {verified.invalid_reason or 'unknown reason'}"
            )

        result = await call_next(ctx)
        if not isinstance(result, CallToolResult) or result.is_error:
            return result  # failed work is never settled

        try:
            settled = await server.settle_payment(payload, matched)
        except Exception as exc:
            log.exception("settle raised", extra={"tool": params.name})
            return self._settle_failed(reqs, resource, matched.network, str(exc))
        if not settled.success:
            return self._settle_failed(
                reqs, resource, matched.network, settled.error_reason or "unknown"
            )
        log.info(
            "payment settled",
            extra={
                "tool": params.name,
                "network": settled.network,
                "transaction": settled.transaction,
                "amount": matched.amount,
            },
        )
        result.meta = {
            **(result.meta or {}),
            PAYMENT_RESPONSE_META_KEY: settled.model_dump(by_alias=True, exclude_none=True),
        }
        return result

    @staticmethod
    def _settle_failed(
        reqs: list[PaymentRequirements], resource: ResourceInfo, network: str, reason: str
    ) -> CallToolResult:
        return _result(
            {
                "x402Version": 2,
                "error": f"Payment settlement failed: {reason}",
                "accepts": [r.model_dump(by_alias=True, exclude_none=True) for r in reqs],
                "resource": resource.model_dump(by_alias=True, exclude_none=True),
                PAYMENT_RESPONSE_META_KEY: {
                    "success": False,
                    "errorReason": reason,
                    "transaction": "",
                    "network": network,
                },
            }
        )
