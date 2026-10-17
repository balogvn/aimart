"""Pure-ASGI middleware: request IDs and access logs, body-size caps, per-client rate limits.

Pure ASGI (not BaseHTTPMiddleware) so request bodies stream through untouched — the multipart
upload route must spool to disk, not buffer 50 MB in memory.
"""

from __future__ import annotations

import json
import logging
import math
import secrets
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("aimart.access")


async def _send_json(
    send: Send,
    status: int,
    body: dict[str, object],
    headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                *(headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def client_ip(scope: Scope, *, trust_forwarded_for: bool) -> str:
    if trust_forwarded_for:
        for key, value in scope.get("headers", []):
            if key == b"x-forwarded-for":
                return str(value.decode("latin-1").split(",")[0].strip())
    client = scope.get("client")
    return str(client[0]) if client else "unknown"


class AccessLogMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = secrets.token_hex(8)
        started = time.monotonic()
        status = 500
        settled = False

        async def wrapped(message: Message) -> None:
            nonlocal status, settled
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = list(message.get("headers", []))
                settled = any(
                    k.lower() in {b"payment-response", b"x-payment-response"} for k, _ in headers
                )
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        finally:
            if scope.get("path") not in {"/health"}:
                log.info(
                    "request",
                    extra={
                        "request_id": request_id,
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "status": status,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "payment_settled": settled,
                    },
                )


class BodyLimitMiddleware:
    """413 any request body over the limit, whether or not it declares Content-Length."""

    def __init__(
        self, app: ASGIApp, *, default_limit: int, upload_limit: int, upload_paths: tuple[str, ...]
    ) -> None:
        self.app = app
        self.default_limit = default_limit
        self.upload_limit = upload_limit
        self.upload_paths = upload_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.upload_limit if scope.get("path") in self.upload_paths else self.default_limit
        for key, value in scope.get("headers", []):
            if key == b"content-length" and value.isdigit() and int(value) > limit:
                await _too_large(send, limit)
                return
        received = 0
        too_large = False

        async def limited() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        await self.app(scope, limited, tracking_send)
        if too_large and not response_started:
            await _too_large(send, limit)


async def _too_large(send: Send, limit: int) -> None:
    await _send_json(
        send,
        413,
        {
            "error": {
                "code": "input_too_large",
                "message": f"Request body exceeds {limit // (1024 * 1024)} MB.",
                "hint": "Pass a url instead of inline content, or use /v1/convert/upload.",
            }
        },
    )


class RateLimitMiddleware:
    """Token bucket per client IP, held in a bounded LRU so a flood of IPs cannot grow memory."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        per_minute: int,
        trust_forwarded_for: bool,
        exempt: tuple[str, ...] = ("/health",),
        max_clients: int = 50_000,
    ) -> None:
        self.app = app
        self.capacity = float(max(1, per_minute))
        self.rate = self.capacity / 60.0
        self.trust = trust_forwarded_for
        self.exempt = exempt
        self.max_clients = max_clients
        self.buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def allow(self, key: str, now: float) -> tuple[bool, float]:
        tokens, last = self.buckets.pop(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        self.buckets[key] = (tokens, now)
        if len(self.buckets) > self.max_clients:
            self.buckets.popitem(last=False)
        retry = 0.0 if allowed else (1.0 - tokens) / self.rate
        return allowed, retry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.exempt:
            await self.app(scope, receive, send)
            return
        allowed, retry = self.allow(
            client_ip(scope, trust_forwarded_for=self.trust), time.monotonic()
        )
        if not allowed:
            await _send_json(
                send,
                429,
                {
                    "error": {
                        "code": "rate_limited",
                        "message": "Too many requests.",
                        "hint": f"Retry after {math.ceil(retry)} s.",
                    }
                },
                headers=[(b"retry-after", str(math.ceil(retry)).encode())],
            )
            return
        await self.app(scope, receive, send)
