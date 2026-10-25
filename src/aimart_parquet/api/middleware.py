"""Pure-ASGI middleware: request IDs and access logs, body-size caps, per-client rate limits,
and a look at an upload form's small fields before the payment middleware quotes it.

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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import unquote_plus

from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, QuerystringParser, parse_options_header
from starlette.datastructures import Headers

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.responses import Response
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


def client_ip(scope: Scope, *, trust_forwarded_for: bool, trusted_hops: int = 1) -> str:
    """The caller's address for rate limiting.

    Behind a proxy, only the entries the proxies themselves APPENDED to X-Forwarded-For are
    trustworthy; everything to their left is whatever the client chose to send. So we count
    ``trusted_hops`` from the right. Taking the first entry (as uvicorn does with
    forwarded_allow_ips="*") let any caller dodge the limiter with a fresh fake IP per request.
    """
    if trust_forwarded_for:
        values = [v for k, v in scope.get("headers", []) if k == b"x-forwarded-for"]
        hops = [h.strip() for h in b",".join(values).decode("latin-1").split(",") if h.strip()]
        if hops:
            return hops[-min(max(1, trusted_hops), len(hops))]
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

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            # Once the body is over the limit, the app's reply to our disconnect (FastAPI's 400
            # "error parsing the body") is dropped and the caller gets 413 instead — unless the
            # app had already started its response, which can no longer be replaced.
            if too_large and not response_started:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        await self.app(scope, limited, guarded_send)
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
        trusted_hops: int = 1,
        exempt: tuple[str, ...] = ("/health",),
        max_clients: int = 50_000,
    ) -> None:
        self.app = app
        self.capacity = float(max(1, per_minute))
        self.rate = self.capacity / 60.0
        self.trust = trust_forwarded_for
        self.trusted_hops = trusted_hops
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
            client_ip(scope, trust_forwarded_for=self.trust, trusted_hops=self.trusted_hops),
            time.monotonic(),
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


# ---------------------------------------------------------------------------------- form peek
FORM_TYPES = ("multipart/form-data", "application/x-www-form-urlencoded")


@dataclass
class FormPeek:
    """What the start of a form body says, read before any route or payment middleware."""

    fields: dict[str, str] = field(default_factory=dict)  # non-file parts; the last one wins
    files: set[str] = field(default_factory=set)  # names of finished parts with a filename
    complete: bool = False  # the whole body was read: no other part follows
    error: str | None = None  # not a well-formed form: the route could only answer 400


def _decode(raw: bytes, charset: str) -> str:
    """As Starlette decodes multipart names and values."""
    try:
        return raw.decode(charset)
    except (UnicodeDecodeError, LookupError):
        return raw.decode("latin-1")


class _FormReader:
    """Feeds a form body to python-multipart (Starlette's own parser) and records what it sees.

    File parts are noted by name and their data skipped; field values are kept, as Starlette
    keeps them. Only ever sees the first ``FormPeekMiddleware.peek_bytes`` of a body.
    """

    def __init__(self, content_type: str) -> None:
        self.peek = FormPeek()
        kind, params = parse_options_header(content_type)
        self._charset = params.get(b"charset", b"utf-8").decode("latin-1")
        self._header_name = b""
        self._header_text = b""
        self._disposition = b""
        self._name = b""
        self._value = bytearray()
        self._is_file = False
        self._parser: MultipartParser | QuerystringParser | None = None
        if kind == b"multipart/form-data":
            boundary = params.get(b"boundary")
            if not boundary:
                return  # an error once there is a body; an empty one is still a price probe
            self._parser = MultipartParser(
                boundary,
                {
                    "on_part_begin": self._part_begin,
                    "on_header_field": self._header_field,
                    "on_header_value": self._header_value,
                    "on_header_end": self._header_end,
                    "on_headers_finished": self._headers_finished,
                    "on_part_data": self._data,
                    "on_part_end": self._part_end,
                },
            )
        else:
            self._parser = QuerystringParser(
                {
                    "on_field_start": self._part_begin,
                    "on_field_name": self._field_name,
                    "on_field_data": self._data,
                    "on_field_end": self._field_end,
                }
            )

    def feed(self, chunk: bytes) -> None:
        if not chunk or self.peek.error is not None:
            return
        if self._parser is None:
            self.peek.error = "Missing boundary in multipart."
            return
        try:
            self._parser.write(chunk)
        except FormParserError as exc:
            self.peek.error = str(exc) or "The form body is malformed."

    def finish(self) -> None:
        if self._parser is not None and self.peek.error is None:
            try:
                self._parser.finalize()
            except FormParserError as exc:
                self.peek.error = str(exc) or "The form body is malformed."
        self.peek.complete = self.peek.error is None

    # ---------------------------------------------------------------- parser callbacks
    def _part_begin(self) -> None:
        self._header_name = b""
        self._header_text = b""
        self._disposition = b""
        self._name = b""
        self._value = bytearray()
        self._is_file = False

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_text += data[start:end]

    def _header_end(self) -> None:
        if self._header_name.lower() == b"content-disposition":
            self._disposition = self._header_text
        self._header_name = b""
        self._header_text = b""

    def _headers_finished(self) -> None:
        _, options = parse_options_header(self._disposition)
        if b"name" not in options:
            # Starlette refuses such a part ("name" must be provided): a 400 either way.
            self.peek.error = 'The Content-Disposition header field "name" must be provided.'
            return
        self._name = options[b"name"]
        self._is_file = b"filename" in options

    def _data(self, data: bytes, start: int, end: int) -> None:
        if not self._is_file:
            self._value += data[start:end]

    def _part_end(self) -> None:
        if self.peek.error is not None:
            return
        name = _decode(self._name, self._charset)
        if self._is_file:
            self.peek.files.add(name)
        else:
            self.peek.fields[name] = _decode(bytes(self._value), self._charset)

    def _field_name(self, data: bytes, start: int, end: int) -> None:
        self._name += data[start:end]

    def _field_end(self) -> None:
        name = unquote_plus(self._name.decode("latin-1"))
        self.peek.fields[name] = unquote_plus(bytes(self._value).decode("latin-1"))


class FormPeekMiddleware:
    """Show ``check`` the small fields of a form POST before the app (and its payment
    middleware) sees the request; ``check`` may answer it instead.

    The payment middleware quotes and verifies the upload route before FastAPI parses its form,
    so an upload that could only fail (invalid ``options``, no ``file`` part) got a 402, and its
    paid retry a facilitator verify, before the route's 400. This reads the body until it ends
    or ``peek_bytes`` have arrived (clients send fields before the file, so ``options`` comes
    early), then replays every message it read, unchanged, to the app: the file still streams.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        paths: tuple[str, ...],
        check: Callable[[Scope, FormPeek], Response | None],
        peek_bytes: int = 1024 * 1024,
    ) -> None:
        self.app = app
        self.paths = paths
        self.check = check
        self.peek_bytes = peek_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope["path"] not in self.paths
        ):
            await self.app(scope, receive, send)
            return
        content_type = Headers(scope=scope).get("content-type", "")
        if not content_type.startswith(FORM_TYPES):
            await self.app(scope, receive, send)
            return
        reader = _FormReader(content_type)
        buffered: list[Message] = []
        read = 0
        disconnected = False
        while reader.peek.error is None and read < self.peek_bytes:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                disconnected = True  # gone, or over the body limit: the app answers that
                break
            chunk = message.get("body", b"")
            read += len(chunk)
            reader.feed(chunk)
            if not message.get("more_body", False):
                reader.finish()
                break
        answer = None if disconnected else self.check(scope, reader.peek)
        if answer is not None:
            await answer(scope, receive, send)
            return

        async def replay() -> Message:
            return buffered.pop(0) if buffered else await receive()

        await self.app(scope, replay, send)
