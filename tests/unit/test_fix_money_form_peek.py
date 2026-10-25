"""FormPeekMiddleware (RV4) on its own: what it reads, what it hands on, and what it never eats.

It sits in front of the payment middleware, so a bug here is either a quote for a request that
can only fail or, worse, an upload that reaches the route without some of its bytes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from starlette.responses import JSONResponse

from aimart_parquet.api.middleware import FormPeek, FormPeekMiddleware

if TYPE_CHECKING:
    from starlette.types import Message, Receive, Scope, Send

pytestmark = pytest.mark.anyio

BOUNDARY = "b0und4ry"
MULTIPART = f"multipart/form-data; boundary={BOUNDARY}"


def part(name: str, value: bytes, filename: str | None = None) -> bytes:
    disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
    return f"--{BOUNDARY}\r\nContent-Disposition: {disposition}\r\n\r\n".encode() + value + b"\r\n"


def multipart(*parts: bytes) -> bytes:
    return b"".join(parts) + f"--{BOUNDARY}--\r\n".encode()


class Harness:
    """Drives the middleware with a scripted body and records what reached the app."""

    def __init__(self, *, answer: bool = False, peek_bytes: int = 1024 * 1024) -> None:
        self.peeks: list[FormPeek] = []
        self.received: list[Message] = []
        self.app_called = False
        self.sent: list[Message] = []
        self.answer = answer

        def check(_scope: Scope, peek: FormPeek) -> JSONResponse | None:
            self.peeks.append(peek)
            return JSONResponse({"refused": True}, status_code=400) if self.answer else None

        async def app(_scope: Scope, receive: Receive, _send: Send) -> None:
            self.app_called = True
            while True:
                message = await receive()
                self.received.append(message)
                if message["type"] != "http.request" or not message.get("more_body", False):
                    return

        self.middleware = FormPeekMiddleware(
            app, paths=("/up",), check=check, peek_bytes=peek_bytes
        )

    async def run(
        self,
        chunks: list[bytes],
        *,
        content_type: str = MULTIPART,
        method: str = "POST",
        path: str = "/up",
        ended: bool = True,
    ) -> None:
        script: list[Message] = [
            {"type": "http.request", "body": c, "more_body": not ended or i < len(chunks) - 1}
            for i, c in enumerate(chunks)
        ]

        async def receive() -> Message:
            return script.pop(0) if script else {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            self.sent.append(message)

        scope: dict[str, Any] = {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [(b"content-type", content_type.encode())],
        }
        await self.middleware(scope, receive, send)

    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.received if m["type"] == "http.request")


def split(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


async def test_fields_and_files_are_seen_and_every_byte_is_replayed() -> None:
    body = multipart(
        part("options", b'{"sort_by": ["a"]}'),
        part("file", b"a,b\n" + b"1,2\n" * 50_000, filename="a.csv"),
        part("note", "café".encode()),
    )
    h = Harness()
    await h.run(split(body, 7_001))  # boundaries and headers split across messages
    [peek] = h.peeks
    assert peek.fields == {"options": '{"sort_by": ["a"]}', "note": "café"}
    assert peek.files == {"file"}
    assert peek.complete
    assert peek.error is None
    assert h.body() == body
    assert h.received[-1]["more_body"] is False


async def test_only_the_first_peek_bytes_are_read_before_the_app_runs() -> None:
    body = multipart(part("options", b"{}"), part("file", b"x" * 300_000, filename="a.csv"))
    chunks = split(body, 10_000)
    h = Harness(peek_bytes=50_000)
    await h.run(chunks)
    [peek] = h.peeks
    assert peek.fields == {"options": "{}"}  # fields before the file are read in time
    assert not peek.complete  # whatever follows is unknown: no "missing part" verdicts
    assert h.body() == body


async def test_a_refusal_answers_instead_of_the_app() -> None:
    h = Harness(answer=True)
    await h.run([multipart(part("options", b"nope"))])
    assert not h.app_called
    assert h.sent[0]["status"] == 400


async def test_a_disconnect_while_peeking_is_left_to_the_app() -> None:
    # e.g. BodyLimitMiddleware cut an over-limit body: the app (and BodyLimit) answer that.
    first = part("options", b"{}")
    h = Harness(answer=True)
    await h.run([first, first], ended=False)  # then the client is gone
    assert h.peeks == []  # no verdict on half a body
    assert h.app_called
    assert h.body() == first + first
    assert h.received[-1]["type"] == "http.disconnect"


@pytest.mark.parametrize(
    ("content_type", "body", "error"),
    [
        ("multipart/form-data", b"--x\r\n", "Missing boundary"),
        (MULTIPART, b"--wrong\r\n", "boundary"),
        (MULTIPART, f"--{BOUNDARY}\r\nContent-Type: text/plain\r\n\r\nx\r\n".encode(), '"name"'),
    ],
    ids=["no-boundary", "wrong-boundary", "no-name"],
)
async def test_malformed_forms_are_reported(content_type: str, body: bytes, error: str) -> None:
    h = Harness()
    await h.run([body], content_type=content_type)
    [peek] = h.peeks
    assert peek.error is not None
    assert error in peek.error
    assert h.body() == body  # handed on intact all the same (the check decides)


async def test_an_empty_form_is_not_malformed() -> None:
    # `curl -X POST -H 'content-type: multipart/form-data'`: a price probe, not an error.
    h = Harness()
    await h.run([b""], content_type="multipart/form-data")
    [peek] = h.peeks
    assert peek.error is None
    assert peek.complete
    assert peek.fields == {}
    assert peek.files == set()


async def test_a_file_part_cut_off_is_not_counted() -> None:
    # Starlette lists a part only once it ends; the route would say `file` is missing.
    body = (
        part("options", b"{}")
        + f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="a"\r\n\r\nabc'.encode()
    )
    h = Harness()
    await h.run([body])
    [peek] = h.peeks
    assert peek.files == set()
    assert peek.fields == {"options": "{}"}


async def test_urlencoded_fields_are_decoded_like_starlette() -> None:
    h = Harness()
    await h.run(
        [b"options=%7B%22a%22%3A+1%7D&flag&x=caf%C3%A9"],
        content_type="application/x-www-form-urlencoded",
    )
    [peek] = h.peeks
    assert peek.fields == {"options": '{"a": 1}', "flag": "", "x": "café"}
    assert peek.files == set()
    assert peek.complete


async def test_a_bad_charset_falls_back_to_latin_1() -> None:
    h = Harness()
    await h.run(
        [multipart(part("note", b"caf\xe9"))], content_type=MULTIPART + "; charset=no-such-codec"
    )
    assert h.peeks[0].fields == {"note": "café"}


@pytest.mark.parametrize(
    ("method", "path", "content_type"),
    [("GET", "/up", MULTIPART), ("POST", "/other", MULTIPART), ("POST", "/up", "application/json")],
    ids=["get", "other-path", "json"],
)
async def test_other_requests_pass_untouched(method: str, path: str, content_type: str) -> None:
    h = Harness(answer=True)
    await h.run([b"{}"], method=method, path=path, content_type=content_type)
    assert h.peeks == []
    assert h.app_called
    assert h.body() == b"{}"
