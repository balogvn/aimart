"""A paid REST call whose client hung up before its response is not settled.

The x402 middleware settles every response below 400 once the route returns. A caller that
disconnected while its call ran (its own timeout, or Cloud Run's front end answering 504 at its
request timeout, 120 s while a paid call may run for the 300 s payment window) never received
the response, but its payment was settled: charged for nothing. ``Payments.http_middleware``
now asks the request whether its client is still there before the SDK settles; when it is
gone, the response is a 499 ``client_disconnected``, which is not settled and whose outputs are
deleted (invariant 8).
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

import anyio
import pytest

import aimart_testkit as kit

if TYPE_CHECKING:
    from starlette.types import Message

pytestmark = pytest.mark.anyio

CSV = b"a,b\n1,2\n3,4\n"
BODY = {"content_base64": kit.b64(CSV), "filename": "a.csv"}


async def _payment_header(h: kit.AppHarness) -> bytes:
    from x402 import parse_payment_required

    quote = await h.http.post("/v1/convert", json=BODY)
    assert quote.status_code == 402
    required = parse_payment_required(base64.b64decode(quote.headers["payment-required"]))
    payload = await kit.paying_client().create_payment_payload(required)
    return base64.b64encode(
        json.dumps(payload.model_dump(by_alias=True, exclude_none=True)).encode()
    )


async def _call(h: kit.AppHarness, *, hang_up: bool) -> tuple[int, dict[str, Any]]:
    """POST /v1/convert, paid, straight through ASGI. With ``hang_up`` the client is gone once
    its body is sent, as a server's receive reports it (every later receive is a disconnect)."""
    body = json.dumps(BODY).encode()
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/convert",
        "raw_path": b"/v1/convert",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"payment-signature", await _payment_header(h)),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    sent_body = False
    finished = anyio.Event()

    async def receive() -> Message:
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        if not hang_up:
            await finished.wait()  # a connected client: nothing more until the response ends
        return {"type": "http.disconnect"}

    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            finished.set()

    await h.app(scope, receive, send)
    [start] = [m for m in messages if m["type"] == "http.response.start"]
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], json.loads(raw or b"{}")


async def test_a_call_whose_client_hung_up_is_not_settled(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    status, payload = await _call(paid_app, hang_up=True)
    assert status == 499, payload  # before: 200, and settled
    assert payload["error"]["code"] == "client_disconnected"
    assert len(facilitator.verified) == 1
    assert facilitator.settled == []
    # Its output was never paid for: it does not stay in the artifact budget (invariant 8).
    root = paid_app.settings.artifact_dir
    assert [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")] == []


async def test_a_connected_client_is_settled(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    status, payload = await _call(paid_app, hang_up=False)
    assert status == 200, payload
    assert payload["report"]["rows"] == 2
    assert len(facilitator.settled) == 1
