"""The SSRF guard and input materialization (sources.py).

``fetch_url`` runs against a real HTTP server on 127.0.0.1 (``allow_private=True``; its port is
added to ``ALLOWED_PORTS`` for the test). Where a test needs that loopback server to stand in
for a *public* host, it says so and narrows ``ip_is_public`` to exactly that one address.
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from aimart_parquet import sources
from aimart_parquet.errors import FetchFailed, InputTooLarge, InvalidRequest, UrlNotAllowed
from aimart_parquet.sources import (
    _filename_from,
    _validate_url,
    decode_base64,
    fetch_url,
    ip_is_public,
    safe_filename,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# ------------------------------------------------------------------------------ ip_is_public
@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("1.1.1.1", True),
        ("2606:4700::1111", True),
        ("10.0.0.1", False),  # private
        ("172.16.5.4", False),
        ("192.168.1.1", False),
        ("127.0.0.1", False),  # loopback
        ("127.255.255.254", False),
        ("::1", False),
        ("169.254.169.254", False),  # cloud metadata (link-local)
        ("fe80::1", False),
        ("100.64.1.1", False),  # CGNAT
        ("224.0.0.1", False),  # multicast
        ("ff02::1", False),
        ("0.0.0.0", False),  # unspecified
        ("::", False),
        ("240.0.0.1", False),  # reserved
        ("255.255.255.255", False),
        ("fc00::1", False),  # unique local
        ("64:ff9b::7f00:1", False),  # NAT64 of 127.0.0.1
        ("64:ff9b::808:808", True),  # NAT64 of 8.8.8.8
        ("::ffff:127.0.0.1", False),  # IPv4-mapped
        ("::ffff:169.254.169.254", False),
        ("::ffff:8.8.8.8", True),
        ("::127.0.0.1", False),  # IPv4-compatible (deprecated)
        ("::10.0.0.1", False),
        ("2002:7f00:1::1", False),  # 6to4 of 127.0.0.1
        ("2002:a9fe:a9fe::1", False),  # 6to4 of 169.254.169.254
        ("2002:808:808::1", True),  # 6to4 of 8.8.8.8
        ("2001:0:4136:e378:8000:63bf:3fff:fdd2", False),  # Teredo
        ("2001:db8::1", False),  # documentation
    ],
)
def test_ip_is_public(address: str, public: bool) -> None:
    assert ip_is_public(ipaddress.ip_address(address)) is public


# ------------------------------------------------------------------------------ _validate_url
@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a.csv",
        "http://example.com/a.csv",
        "http://example.com:80/a.csv",
        "https://example.com:443/a.csv",
        "http://example.com:8080/a.csv",
        "https://example.com:8443/a.csv",
    ],
)
def test_validate_url_accepts_web_ports(url: str) -> None:
    _validate_url(url)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://example.com/a.csv", "Only http and https"),
        ("file:///etc/passwd", "Only http and https"),
        ("gopher://example.com/", "Only http and https"),
        ("//example.com/a.csv", "Only http and https"),
        ("https:///a.csv", "no host"),
        ("http://example.com:22/", "Port 22 is not allowed"),
        ("http://example.com:6379/", "Port 6379 is not allowed"),
        ("https://example.com:25/", "Port 25 is not allowed"),
        ("http://example.com:99999/", "invalid port"),
        ("http://example.com:abc/", "invalid port"),
    ],
)
def test_validate_url_rejects(url: str, message: str) -> None:
    with pytest.raises(UrlNotAllowed, match=message):
        _validate_url(url)


# ------------------------------------------------------------------------------ filenames
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("survey.sav", "survey.sav"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\config", "config"),
        ("/abs/path/data.csv", "data.csv"),
        ("my data (final).csv", "my_data_final_.csv"),
        ("..", "input"),
        ("", "input"),
        (".hidden", "hidden"),
        ("dir/", "dir"),
        ("naïve.csv", "na_ve.csv"),
        ("a" * 300 + ".csv", "a" * 120),
    ],
)
def test_safe_filename(name: str, expected: str) -> None:
    assert safe_filename(name) == expected


def test_filename_from_disposition_and_url() -> None:
    assert _filename_from("https://x/a/b.csv", None) == "b.csv"
    assert _filename_from("https://x/a/my%20file.csv?x=1", None) == "my_file.csv"
    assert _filename_from("https://x/", None) is None
    assert _filename_from("https://x/a", 'attachment; filename="../../r.xlsx"') == "r.xlsx"
    assert _filename_from("https://x/a", "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.csv") == (
        "r_sum_.csv"
    )
    assert _filename_from("https://x/q.csv", "inline") == "q.csv"


# ------------------------------------------------------------------------------ base64
def test_decode_base64_plain_and_data_url(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    assert decode_base64("YSxiCjEsMgo=", dest, max_bytes=100) == 8
    assert dest.read_bytes() == b"a,b\n1,2\n"
    assert decode_base64("data:text/csv;base64,YSxiCjEsMgo=", dest, max_bytes=100) == 8
    assert dest.read_bytes() == b"a,b\n1,2\n"


def test_decode_base64_refuses_oversized_text_before_decoding(tmp_path: Path) -> None:
    with pytest.raises(InputTooLarge) as info:
        decode_base64("A" * 200, tmp_path / "out", max_bytes=100)
    assert info.value.details["limit_bytes"] == 100
    assert not (tmp_path / "out").exists()


def test_decode_base64_refuses_oversized_payload_after_decoding(tmp_path: Path) -> None:
    import base64

    data = base64.b64encode(
        b"x" * 101
    ).decode()  # 136 chars: under the text cap, over the bytes cap
    with pytest.raises(InputTooLarge):
        decode_base64(data, tmp_path / "out", max_bytes=100)
    assert not (tmp_path / "out").exists()


def test_decode_base64_exact_limit_is_accepted(tmp_path: Path) -> None:
    import base64

    data = base64.b64encode(b"x" * 100).decode()
    assert decode_base64(data, tmp_path / "out", max_bytes=100) == 100


def test_decode_base64_rejects_garbage(tmp_path: Path) -> None:
    with pytest.raises(InvalidRequest, match="not valid base64"):
        decode_base64("abc", tmp_path / "out", max_bytes=100)


# ------------------------------------------------------------------------------ fetch_url
BIG = b"x" * 5000


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    port: ClassVar[int] = 0
    requests: ClassVar[list[str]] = []

    def log_message(self, *_: Any) -> None:  # keep pytest output clean
        return

    def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if "Content-Length" not in (headers or {}):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:
        type(self).requests.append(self.path)
        base = f"http://127.0.0.1:{self.port}"
        routes: dict[str, Any] = {
            "/data.csv": lambda: self._send(
                200,
                b"a,b\n1,2\n",
                {"Content-Type": "text/csv", "Content-Disposition": 'attachment; filename="r.csv"'},
            ),
            "/plain": lambda: self._send(200, b"hello"),
            "/redirect": lambda: self._send(302, headers={"Location": "/data.csv"}),
            "/redirect-abs": lambda: self._send(301, headers={"Location": f"{base}/plain"}),
            "/redirect-metadata": lambda: self._send(
                302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
            ),
            "/redirect-port": lambda: self._send(
                307, headers={"Location": "http://127.0.0.1:6379/"}
            ),
            "/redirect-file": lambda: self._send(308, headers={"Location": "file:///etc/passwd"}),
            "/redirect-nowhere": lambda: self._send(302),
            "/loop": lambda: self._send(302, headers={"Location": "/loop"}),
            "/big-declared": lambda: self._send(200, BIG),
            "/big-streamed": self._stream_big,
            "/missing": lambda: self._send(404, b"nope"),
            "/error": lambda: self._send(503, b"down"),
            "/slow": self._slow,
            "/garbage": self._garbage,
        }
        routes.get(self.path, lambda: self._send(404))()

    def _stream_big(self) -> None:
        # No Content-Length: the cap must be enforced while streaming, not from the header.
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for _ in range(5):
            chunk = b"y" * 1000
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def _slow(self) -> None:
        time.sleep(1.5)
        with contextlib.suppress(OSError):
            self._send(200, b"late")

    def _garbage(self) -> None:
        self.wfile.write(b"THIS IS NOT HTTP\r\n\r\n")
        self.close_connection = True


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    Handler.port = port
    Handler.requests = []
    monkeypatch.setattr(sources, "ALLOWED_PORTS", frozenset({80, 443, 8080, 8443, port}))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


def loopback_is_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the loopback test server stand in for a public host; nothing else changes."""
    real = sources.ip_is_public
    loopback = ipaddress.ip_address("127.0.0.1")
    monkeypatch.setattr(sources, "ip_is_public", lambda ip: ip == loopback or real(ip))


async def fetch(url: str, tmp_path: Path, **kwargs: Any) -> sources.Fetched:
    options: dict[str, Any] = {"max_bytes": 1000, "deadline_s": 5.0, "allow_private": True}
    options.update(kwargs)
    return await fetch_url(url, tmp_path / "download", **options)


@pytest.mark.anyio
async def test_fetch_200(server: str, tmp_path: Path) -> None:
    got = await fetch(f"{server}/data.csv", tmp_path)
    assert got.path.read_bytes() == b"a,b\n1,2\n"
    assert got.size == 8
    assert got.filename == "r.csv"
    assert got.content_type == "text/csv"


@pytest.mark.anyio
async def test_fetch_follows_relative_and_absolute_redirects(server: str, tmp_path: Path) -> None:
    got = await fetch(f"{server}/redirect", tmp_path)
    assert got.path.read_bytes() == b"a,b\n1,2\n"
    got = await fetch(f"{server}/redirect-abs", tmp_path)
    assert got.path.read_bytes() == b"hello"
    assert got.filename == "plain"
    assert Handler.requests == ["/redirect", "/data.csv", "/redirect-abs", "/plain"]


@pytest.mark.anyio
async def test_redirect_to_cloud_metadata_is_blocked(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A "public" host answering with a redirect into the metadata service: each hop is re-checked.
    loopback_is_public(monkeypatch)
    with pytest.raises(UrlNotAllowed, match="non-public"):
        await fetch(f"{server}/redirect-metadata", tmp_path, allow_private=False)
    assert Handler.requests == ["/redirect-metadata"]


@pytest.mark.anyio
async def test_public_host_works_with_the_guard_on(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loopback_is_public(monkeypatch)
    got = await fetch(f"{server}/redirect", tmp_path, allow_private=False)
    assert got.path.read_bytes() == b"a,b\n1,2\n"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "message"),
    [("/redirect-port", "Port 6379"), ("/redirect-file", "Only http and https")],
)
async def test_redirect_hops_are_revalidated(
    path: str, message: str, server: str, tmp_path: Path
) -> None:
    with pytest.raises(UrlNotAllowed, match=message):
        await fetch(f"{server}{path}", tmp_path)


@pytest.mark.anyio
async def test_redirect_without_location_and_redirect_loops_fail(
    server: str, tmp_path: Path
) -> None:
    with pytest.raises(FetchFailed, match="without a Location"):
        await fetch(f"{server}/redirect-nowhere", tmp_path)
    Handler.requests.clear()
    with pytest.raises(FetchFailed, match="More than 5 redirects"):
        await fetch(f"{server}/loop", tmp_path)
    assert len(Handler.requests) == sources.MAX_REDIRECTS + 1


@pytest.mark.anyio
async def test_declared_content_length_over_cap_is_refused(server: str, tmp_path: Path) -> None:
    with pytest.raises(InputTooLarge) as info:
        await fetch(f"{server}/big-declared", tmp_path, max_bytes=4999)
    assert info.value.details["limit_bytes"] == 4999
    got = await fetch(f"{server}/big-declared", tmp_path, max_bytes=5000)
    assert got.size == 5000


@pytest.mark.anyio
async def test_streamed_body_over_cap_is_refused(server: str, tmp_path: Path) -> None:
    with pytest.raises(InputTooLarge):
        await fetch(f"{server}/big-streamed", tmp_path, max_bytes=2500)
    got = await fetch(f"{server}/big-streamed", tmp_path, max_bytes=5000)
    assert got.size == 5000


@pytest.mark.anyio
@pytest.mark.parametrize(("path", "status"), [("/missing", 404), ("/error", 503)])
async def test_http_errors_are_fetch_failed(
    path: str, status: int, server: str, tmp_path: Path
) -> None:
    with pytest.raises(FetchFailed, match=f"HTTP {status}") as info:
        await fetch(f"{server}{path}", tmp_path)
    assert info.value.details["status"] == status


@pytest.mark.anyio
async def test_deadline_covers_the_whole_download(server: str, tmp_path: Path) -> None:
    started = time.monotonic()
    with pytest.raises(FetchFailed, match=r"longer than 0\.3 s"):
        await fetch(f"{server}/slow", tmp_path, deadline_s=0.3)
    assert time.monotonic() - started < 1.4


@pytest.mark.anyio
async def test_protocol_garbage_is_fetch_failed(server: str, tmp_path: Path) -> None:
    with pytest.raises(FetchFailed, match="Could not download"):
        await fetch(f"{server}/garbage", tmp_path)


@pytest.mark.anyio
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
async def test_guard_blocks_loopback_by_default(host: str, server: str, tmp_path: Path) -> None:
    port = server.rsplit(":", 1)[1]
    with pytest.raises(UrlNotAllowed, match="non-public"):
        await fetch(f"http://{host}:{port}/data.csv", tmp_path, allow_private=False)
    assert Handler.requests == []  # refused before connecting


@pytest.mark.anyio
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://0.0.0.0/",
    ],
)
async def test_guard_blocks_private_literals(url: str, tmp_path: Path) -> None:
    with pytest.raises(UrlNotAllowed, match="non-public"):
        await fetch(url, tmp_path, allow_private=False)


@pytest.mark.anyio
async def test_unresolvable_host_is_not_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_dns(*_: Any, **__: Any) -> Any:
        raise socket.gaierror("nodename nor servname provided")

    monkeypatch.setattr(sources.anyio, "getaddrinfo", no_dns)
    with pytest.raises(UrlNotAllowed, match="Could not resolve"):
        await fetch("https://does-not-exist.invalid/a.csv", tmp_path)


@pytest.mark.anyio
async def test_host_with_no_addresses_is_not_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def empty(*_: Any, **__: Any) -> list[Any]:
        return []

    monkeypatch.setattr(sources.anyio, "getaddrinfo", empty)
    with pytest.raises(UrlNotAllowed, match="no addresses"):
        await fetch("https://empty.invalid/a.csv", tmp_path)


@pytest.mark.anyio
async def test_any_private_address_in_the_answer_blocks_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DNS answers with one public and one private address: refuse, do not pick the "good" one.
    async def mixed(*_: Any, **__: Any) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 443)),
        ]

    monkeypatch.setattr(sources.anyio, "getaddrinfo", mixed)
    with pytest.raises(UrlNotAllowed, match="non-public"):
        await fetch("https://rebind.example/a.csv", tmp_path, allow_private=False)


@pytest.mark.anyio
async def test_connection_refused_is_fetch_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setattr(sources, "ALLOWED_PORTS", frozenset({port}))
    with pytest.raises(FetchFailed, match="Could not connect"):
        await fetch(f"http://127.0.0.1:{port}/a.csv", tmp_path)
