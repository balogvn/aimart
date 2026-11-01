"""FormPeek applies Starlette's form limits, and reads a field open at its limit to the end.

It used to stop at ``peek_bytes`` (1 MiB) wherever that fell, so a field running past it was
never seen whole, and Starlette's 1 MiB part limit was met only in the route, after the quote.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

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


async def peek_at(
    body: bytes, *, chunk: int, peek_bytes: int, content_type: str = MULTIPART
) -> tuple[FormPeek, int, bytes]:
    """The peek, how many messages were read before the app ran, and every byte the app got."""
    peeks: list[FormPeek] = []
    script: list[Message] = [
        {"type": "http.request", "body": body[i : i + chunk], "more_body": i + chunk < len(body)}
        for i in range(0, len(body), chunk)
    ]
    total = len(script)
    left_at_check: list[int] = []
    received = bytearray()

    def check(_scope: Scope, peek: FormPeek) -> None:
        peeks.append(peek)
        left_at_check.append(len(script))

    async def app(_scope: Scope, receive: Receive, _send: Send) -> None:
        while True:
            message = await receive()
            received.extend(message.get("body", b""))
            if not message.get("more_body", False):
                return

    async def receive() -> Message:
        return script.pop(0)

    async def send(_message: Message) -> None:
        raise AssertionError("the check never answers here")

    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/up",
        "headers": [(b"content-type", content_type.encode())],
    }
    middleware = FormPeekMiddleware(app, paths=("/up",), check=check, peek_bytes=peek_bytes)
    await middleware(scope, receive, send)
    [peek] = peeks
    return peek, total - left_at_check[0], bytes(received)


async def test_a_field_open_at_the_limit_is_read_to_its_end_and_no_further() -> None:
    value = b"v" * 300_000
    body = multipart(
        part("pad", b"p" * 40_000), part("options", value), part("file", b"x" * 500_000, "a.csv")
    )
    peek, read, received = await peek_at(body, chunk=10_000, peek_bytes=50_000)
    assert peek.fields["options"] == value.decode()  # seen whole, past the 50 KB limit
    assert peek.files == set()  # the file part's end was never waited for
    assert 34 <= read <= 36  # 340 KB of fields, not the 840 KB body
    assert received == body  # and every byte still reached the app


async def test_a_file_open_at_the_limit_is_not_waited_for() -> None:
    body = multipart(part("options", b"{}"), part("file", b"x" * 500_000, "a.csv"))
    peek, read, received = await peek_at(body, chunk=10_000, peek_bytes=50_000)
    assert peek.fields == {"options": "{}"}
    assert read == 5
    assert not peek.complete
    assert received == body


@pytest.mark.parametrize(
    ("size", "error"),
    [
        (1024 * 1024, None),  # Starlette's limit is "more than 1 MiB"
        (
            1024 * 1024 + 1,
            "Form field 'options' is larger than 1024 KB, the most one field may hold.",
        ),
    ],
)
async def test_the_field_limit_is_starlettes_to_the_byte(size: int, error: str | None) -> None:
    body = multipart(part("options", b"o" * size), part("file", b"a\n", "a.csv"))
    peek, _, _ = await peek_at(body, chunk=64 * 1024, peek_bytes=1024 * 1024)
    assert peek.error == error


async def test_urlencoded_counts_the_name_and_value_as_starlette_does() -> None:
    name = b"o" * 1000
    fits = name + b"=" + b"v" * (1024 * 1024 - len(name))
    peek, _, _ = await peek_at(
        fits, chunk=64 * 1024, peek_bytes=1024, content_type="application/x-www-form-urlencoded"
    )
    assert peek.error is None
    peek, _, _ = await peek_at(
        fits + b"v",
        chunk=64 * 1024,
        peek_bytes=1024,
        content_type="application/x-www-form-urlencoded",
    )
    assert (
        peek.error
        == f"Form field {'o' * 64!r} is larger than 1024 KB, the most one field may hold."
    )


async def test_urlencoded_field_count_is_starlettes() -> None:
    form = "application/x-www-form-urlencoded"
    peek, _, _ = await peek_at(
        b"&".join([b"a=1"] * 1000), chunk=4096, peek_bytes=10**6, content_type=form
    )
    assert peek.error is None
    peek, _, _ = await peek_at(
        b"&".join([b"a=1"] * 1001), chunk=4096, peek_bytes=10**6, content_type=form
    )
    assert peek.error == "The form has more than 1000 fields."


async def test_a_finished_file_part_records_whether_it_held_bytes() -> None:
    # Integration review: an empty file part was quoted, then refused after the payment.
    body = multipart(part("empty", b"", "e.csv"), part("full", b"a\n1\n", "f.csv"))
    peek, _, received = await peek_at(body, chunk=7, peek_bytes=1024)
    assert peek.files == {"empty", "full"}
    assert peek.empty_files == {"empty"}
    assert received == body


async def test_the_last_file_part_of_a_name_decides_whether_it_is_empty() -> None:
    full_then_empty = multipart(part("file", b"a", "a.csv"), part("file", b"", "b.csv"))
    peek, _, _ = await peek_at(full_then_empty, chunk=1024, peek_bytes=1024)
    assert peek.empty_files == {"file"}
    empty_then_full = multipart(part("file", b"", "a.csv"), part("file", b"a", "b.csv"))
    peek, _, _ = await peek_at(empty_then_full, chunk=1024, peek_bytes=1024)
    assert peek.empty_files == set()


async def test_a_file_part_not_finished_in_the_peek_is_not_empty() -> None:
    body = multipart(part("options", b"{}"), part("file", b"x" * 500_000, "a.csv"))
    peek, _, _ = await peek_at(body, chunk=10_000, peek_bytes=50_000)
    assert peek.empty_files == set()
