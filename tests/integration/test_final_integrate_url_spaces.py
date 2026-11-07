"""A URL with a space is fetched as a browser would send it, not refused after the verify.

``validate_url`` refuses before the quote any URL the fetcher could never fetch, and already
percent-encoded non-ASCII characters. But it returned every ASCII URL unchanged, so a literal
space in the path or query, or a trailing space, passed the pre-quote check and was quoted;
paid, it was verified and then h11 refused the request target ("Illegal target characters"): a
422 fetch_failed with verified=1, settled=0, for a file that converts as '/my%20file.csv'. A
Location header with a space failed the same way on the next hop.

Now spaces and control characters in the path, query and fragment are percent-encoded (h11
takes only 0x21-0x7E there), and leading and trailing ones are stripped first, as WHATWG does.
A host with a space or control character can never resolve: that is refused before the quote.

Two over-reaches of that fix, both after the verify or before the quote of a URL 07fa450 served
and settled: a tab in a Location header was percent-encoded ('/x.c%09sv', a 404 and an unsettled
422; a host '127.0.0.1%09'), where WHATWG and urllib remove tabs and line breaks from a URL; and
a space in the userinfo ('http://user:p w@host/x.csv') was refused as a space in the host,
though userinfo is never sent (no Authorization header goes out).
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

import aimart_testkit as kit
from aimart_parquet import sources
from aimart_parquet.errors import UrlNotAllowed
from aimart_parquet.sources import validate_url

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

CSV = b"a,b\n1,2\n3,4\n"


class Files(BaseHTTPRequestHandler):
    requests: ClassVar[list[str]] = []
    authorization: ClassVar[list[str | None]] = []

    def do_GET(self) -> None:
        self.requests.append(self.path)
        self.authorization.append(self.headers.get("Authorization"))
        port = self.server.server_address[1]
        location = {
            "/moved": "/my file.csv",
            "/tab-in-path": "/x.c\tsv",
            "/tab-in-host": f"http://127.0.0.1\t:{port}/x.csv",
        }.get(self.path)
        if location is not None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path not in {"/my%20file.csv", "/x.csv", "/x.csv?q=a%20b"}:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(CSV)))
        self.end_headers()
        self.wfile.write(CSV)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def base(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Files)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    Files.requests = []
    Files.authorization = []
    monkeypatch.setattr(sources, "ALLOWED_PORTS", frozenset({80, 443, 8080, 8443, port}))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


# Each: the URL's path as the caller wrote it, then the request target the server must see.
FETCHABLE = [
    ("/my file.csv", "/my%20file.csv"),  # before: 422 'Illegal target characters' after verify
    ("/x.csv ", "/x.csv"),  # a trailing space, as pasted
    ("/x.csv \n\t", "/x.csv"),
    ("/x.csv?q=a b", "/x.csv?q=a%20b"),
    ("/moved", "/my%20file.csv"),  # a Location header with a space, followed
    # A tab is removed from a Location, as WHATWG and urllib do; before: '/x.c%09sv', a 404, and
    # a host '127.0.0.1%09' that could not resolve, each a 422 after the verify.
    ("/tab-in-path", "/x.csv"),
    ("/tab-in-host", "/x.csv"),
]


@pytest.mark.parametrize(("path", "target"), FETCHABLE)
async def test_a_paid_convert_of_a_url_with_spaces_is_served_and_settled(
    path: str, target: str, base: str, make_settings: Callable[..., Settings]
) -> None:
    fake = kit.FakeFacilitator()
    settings = make_settings(allow_private_urls=True)
    async with kit.running_app(settings, facilitator=fake) as h:
        quote = await h.http.post("/v1/convert", json={"url": base + path})
        assert quote.status_code == 402
        r = await h.paying.post("/v1/convert", json={"url": base + path})
    assert r.status_code == 200, r.text
    assert r.json()["report"]["rows"] == 2
    assert Files.requests[-1] == target
    assert len(fake.verified) == 1
    assert len(fake.settled) == 1


@pytest.mark.parametrize("userinfo", ["user:p w@", "my user@", "u:\x01@"])
async def test_a_space_in_the_userinfo_is_fetched_without_it_and_settled(
    userinfo: str, base: str, make_settings: Callable[..., Settings]
) -> None:
    """Before: 422 url_not_allowed "The URL's host contains a space or a control character."
    before the quote; 07fa450 fetched '/x.csv' and settled. Userinfo is never sent."""
    fake = kit.FakeFacilitator()
    settings = make_settings(allow_private_urls=True)
    url = base.replace("http://", "http://" + userinfo) + "/x.csv"
    async with kit.running_app(settings, facilitator=fake) as h:
        quote = await h.http.post("/v1/convert", json={"url": url})
        assert quote.status_code == 402, quote.text
        r = await h.paying.post("/v1/convert", json={"url": url})
    assert r.status_code == 200, r.text
    assert r.json()["report"]["rows"] == 2
    assert Files.requests == ["/x.csv"]
    assert Files.authorization == [None]
    assert len(fake.verified) == 1
    assert len(fake.settled) == 1


async def test_mcp_convert_of_a_url_with_a_space_is_served_and_settled(
    base: str, make_settings: Callable[..., Settings]
) -> None:
    fake = kit.FakeFacilitator()
    settings = make_settings(allow_private_urls=True)
    args = {"url": f"{base}/my file.csv"}
    async with (
        kit.running_app(settings, facilitator=fake) as h,
        kit.mcp_over_http(h.app) as client,
    ):
        quote = await client.call_tool("convert_to_parquet", args)
        meta = await kit.mcp_payment_meta(kit.paying_client(), quote.structured_content)
        result = await client.call_tool("convert_to_parquet", args, meta=meta)
    assert not result.is_error, result.structured_content
    assert (result.structured_content or {})["report"]["rows"] == 2
    assert Files.requests == ["/my%20file.csv"]
    assert len(fake.settled) == 1


@pytest.mark.parametrize("paying", [False, True], ids=["unpaid", "paid"])
async def test_a_host_with_a_space_is_refused_before_the_quote(
    paying: bool, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    client = paid_app.paying if paying else paid_app.http
    r = await client.post("/v1/convert", json={"url": "https://data.ex ample.com/x.csv"})
    assert r.status_code == 422, r.text
    assert "payment-required" not in r.headers
    error = r.json()["error"]
    assert error["code"] == "url_not_allowed"
    assert error["message"] == "The URL's host contains a space or a control character."
    assert facilitator.verified == []


def test_validate_url_encodes_what_h11_refuses_in_a_target() -> None:
    assert (
        validate_url("https://e.example/a b?q=c d#e f") == "https://e.example/a%20b?q=c%20d#e%20f"
    )
    assert validate_url(" https://e.example/x.csv\r\n") == "https://e.example/x.csv"
    assert validate_url("https://e.example/a\x7fb\x01c") == "https://e.example/a%7Fb%01c"
    assert validate_url("https://e.example/a%20b é") == "https://e.example/a%20b%20%C3%A9"
    # Unchanged when nothing needs it: a signed URL's query is not rewritten.
    signed = "https://e.example/x.csv?X-Amz-Signature=ab%2Fcd&x=1|2"
    assert validate_url(signed) == signed
    with pytest.raises(UrlNotAllowed, match="contains a space or a control character"):
        validate_url("https://e.exa\x00mple/x.csv")
    with pytest.raises(UrlNotAllowed, match="contains a space or a control character"):
        validate_url("https://u:p@e.exa mple/x.csv")  # the host after the userinfo still counts
    assert sources._redirect_target("https://e.example/d/", b"\x01 a\tb\r\n.csv\x00") == (
        "https://e.example/d/ab.csv"
    )
    assert sources._redirect_target("https://e.example/d/", b"my file.csv") == (
        "https://e.example/d/my%20file.csv"
    )
