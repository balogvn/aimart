"""The app must work behind a host that passes only what the ASGI spec requires.

The first deploy on Modal answered every request, ``/health`` included, with 500
``Internal error (KeyError)``: Modal's web endpoints pass no ``raw_path`` (the spec makes it
optional), and x402's FastAPI middleware reads ``request.scope["raw_path"]`` unguarded. uvicorn
and httpx's test transport always send it, so no test and no container smoke run had seen it.
``ScopeDefaultsMiddleware`` now fills in what a host may leave out, before anything reads it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from x402.http.clients.httpx import x402AsyncTransport

import aimart_testkit as kit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from starlette.types import ASGIApp, Receive, Scope, Send

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

OPTIONAL = ("raw_path", "root_path", "client", "server", "scheme", "state")
BODY = {"content_base64": kit.b64(b"id,city\n1,Lagos\n2,Abuja\n"), "filename": "cities.csv"}


def sparse_host(app: ASGIApp, drop: Iterable[str]) -> ASGIApp:
    """The app as a host that leaves ``drop`` out of every request's scope would call it."""
    dropped = frozenset(drop)

    async def host(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope = {key: value for key, value in scope.items() if key not in dropped}
        await app(scope, receive, send)

    return host


@pytest.fixture
async def harness(settings: Settings) -> AsyncIterator[kit.AppHarness]:
    async with kit.running_app(settings, facilitator=kit.FakeFacilitator()) as running:
        yield running


def clients(app: ASGIApp) -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
    plain = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=kit.BASE_URL)
    paying = httpx.AsyncClient(
        transport=x402AsyncTransport(kit.paying_client(), httpx.ASGITransport(app=app)),
        base_url=kit.BASE_URL,
    )
    return plain, paying


@pytest.mark.parametrize(
    "drop",
    [("raw_path",), OPTIONAL, ("query_string",), (*OPTIONAL, "query_string")],
    ids=["no raw_path (Modal)", "nothing optional", "no query_string", "the bare minimum"],
)
async def test_free_and_paid_routes_work_behind_a_sparse_host(
    harness: kit.AppHarness, drop: tuple[str, ...]
) -> None:
    http, paying = clients(sparse_host(harness.app, drop))
    async with http, paying:
        # Before: 500 internal_error (KeyError) on every one of these.
        assert (await http.get("/health")).status_code == 200
        assert (await http.get("/pricing")).status_code == 200
        unpaid = await http.post("/v1/convert", json=BODY)
        assert unpaid.status_code == 402, unpaid.text  # the payment gate still sees the route
        paid = await paying.post("/v1/convert", json=BODY)
        assert paid.status_code == 200, paid.text
        assert paid.json()["report"]["rows"] == 2
    assert harness.facilitator is not None
    assert len(harness.facilitator.settled) == 1


async def test_a_path_that_needs_escaping_is_rebuilt_for_the_payment_gate(
    harness: kit.AppHarness,
) -> None:
    """The derived raw_path is the escaped path, so a non-ASCII path cannot crash the gate."""
    seen: list[Any] = []

    async def spy(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope.get("raw_path"))
        await harness.app(scope, receive, send)

    http, paying = clients(sparse_host(spy, ()))
    async with http, paying:
        assert (await http.get("/caf%C3%A9/a%20b")).status_code == 404
    assert seen == [b"/caf%C3%A9/a%20b"]  # the host gave it: left exactly as given

    http, paying = clients(sparse_host(harness.app, ("raw_path",)))
    async with http, paying:
        missing = await http.get("/caf%C3%A9/a%20b")
        assert missing.status_code == 404, missing.text  # before: 500 (KeyError)


async def test_a_download_link_keeps_its_query_behind_a_sparse_host(
    harness: kit.AppHarness,
) -> None:
    """The signed link's query string must survive the defaults: only a missing one is filled."""
    http, paying = clients(sparse_host(harness.app, OPTIONAL))
    async with http, paying:
        paid = await paying.post("/v1/convert", json=BODY)
        assert paid.status_code == 200, paid.text
        link = paid.json()["artifact"]["download_url"]
        path_and_query = link.removeprefix(harness.settings.public_base_url)
        download = await http.get(path_and_query)
        assert download.status_code == 200
        assert download.content[:4] == b"PAR1"
