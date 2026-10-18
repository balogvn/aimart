"""The HTTP surface: discovery documents, OpenAPI pricing, downloads, uploads, middleware.

Discovery fields are load-bearing for agents and catalogs but inert for function, so each one
gets an assertion that fails when it goes missing. Prices are configured to non-default values
and compared with literals, so a document that ignores Settings cannot pass.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import anyio
import httpx
import pyarrow.parquet as pq
import pytest

import aimart_testkit as kit
from aimart_parquet import __version__
from aimart_parquet.api import app as app_module
from aimart_parquet.api.app import _transport_security, create_app
from aimart_parquet.api.middleware import BodyLimitMiddleware, RateLimitMiddleware, client_ip
from aimart_parquet.payments.pricing import BY_KEY, active_operations

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from starlette.types import Receive, Scope, Send

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

CSV = b"region,units\nNorth,10\nSouth,20\nEast,30\n"
ACTIVE = active_operations()
CUSTOM_PRICES = {
    "price_convert": "$0.03",
    "price_inspect": "$0.004",
    "price_preview": "$0.006",
    "price_query": "$0.007",
    "price_quantum_compress": "$0.75",
    "price_quantum_portfolio": "$1.25",
}
EXPECTED_PRICE = {
    "convert": "$0.03",
    "convert_upload": "$0.03",
    "inspect": "$0.004",
    "preview": "$0.006",
    "query": "$0.007",
    "quantum_compress": "$0.75",
    "quantum_portfolio": "$1.25",
}
FREE_DOCUMENTED = [
    "/v1/verify",
    "/.well-known/aimart-signing-key",
    "/v1/artifacts/{artifact_id}/{filename}",
]


@pytest.fixture
async def priced_app(make_settings: Callable[..., Settings]) -> AsyncIterator[kit.AppHarness]:
    async with kit.running_app(make_settings(**CUSTOM_PRICES)) as harness:
        yield harness


async def convert_free(harness: kit.AppHarness, data: bytes = CSV) -> dict[str, Any]:
    r = await harness.http.post(
        "/v1/convert", json={"content_base64": kit.b64(data), "filename": "sales.csv"}
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


# ------------------------------------------------------------------------------ discovery
async def test_llms_txt_lists_every_operation_route_price_and_tool(
    priced_app: kit.AppHarness,
) -> None:
    r = await priced_app.http.get("/llms.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    text = r.text
    for op in ACTIVE:
        assert f"- `{op.route}` ({EXPECTED_PRICE[op.key]}):" in text, op.key
        assert op.tool in text
    assert "http://test/mcp" in text
    assert "PAYMENT-REQUIRED" in text
    assert "60 minutes" in text  # artifact TTL, from Settings


async def test_pricing_reports_configured_prices(priced_app: kit.AppHarness) -> None:
    body = (await priced_app.http.get("/pricing")).json()
    assert body["x402Version"] == 2
    assert body["currency"] == "USDC"
    assert {o["operation"]: o["price"] for o in body["operations"]} == {
        op.key: EXPECTED_PRICE[op.key] for op in ACTIVE
    }
    for entry in body["operations"]:
        op = BY_KEY[entry["operation"]]
        assert entry["http"] == op.route
        assert entry["mcp_tool"] == op.tool
    assert body["networks"] == [
        {"network": kit.BASE_SEPOLIA, "asset": "USDC", "payTo": kit.PAY_TO_EVM, "scheme": "exact"}
    ]
    assert "GET /llms.txt" in body["free"]


async def test_server_card_lists_each_tool_once_with_its_price(priced_app: kit.AppHarness) -> None:
    card = (await priced_app.http.get("/.well-known/mcp/server-card.json")).json()
    assert card["serverInfo"] == {
        "name": "aimart-parquet",
        "title": "AImart Parquet",
        "version": __version__,
    }
    assert card["transport"] == {"type": "streamable-http", "url": "http://test/mcp"}
    tools = {t["name"]: t["price"] for t in card["tools"]}
    assert len(tools) == len(card["tools"])  # convert_upload shares convert's tool
    assert tools == {
        "convert_to_parquet": "$0.03",
        "inspect_parquet": "$0.004",
        "preview_parquet": "$0.006",
        "query_parquet": "$0.007",
        **(
            {"quantum_compress_features": "$0.75", "quantum_optimize_portfolio": "$1.25"}
            if "quantum_compress" in {op.key for op in ACTIVE}
            else {}
        ),
    }
    assert card["authentication"]["required"] is False


async def test_well_known_x402_lists_every_paid_resource(priced_app: kit.AppHarness) -> None:
    doc = (await priced_app.http.get("/.well-known/x402")).json()
    assert doc["version"] == 1
    assert doc["resources"] == [f"http://test{op.path}" for op in ACTIVE]
    assert sorted(doc["resources"]) == sorted(
        f"http://test{p}"
        for p in (
            "/v1/convert",
            "/v1/convert/upload",
            "/v1/inspect",
            "/v1/preview",
            "/v1/query",
            *(
                ("/v1/quantum/compress", "/v1/quantum/portfolio")
                if "quantum_compress" in {op.key for op in ACTIVE}
                else ()
            ),
        )
    )


async def test_signing_key_document(
    make_settings: Callable[..., Settings],
) -> None:
    seed = base64.b64encode(b"\x07" * 32).decode()
    async with kit.running_app(make_settings(signing_key_mldsa=seed)) as h:
        doc = (await h.http.get("/.well-known/aimart-signing-key")).json()
        again = (await h.http.get("/.well-known/aimart-signing-key")).json()
    assert doc["algorithm"] == "ML-DSA-65"
    assert doc["standard"] == "NIST FIPS 204"
    assert len(base64.b64decode(doc["public_key"])) == 1952
    assert doc["ephemeral"] is False
    assert doc["context"] == "aimart-parquet/provenance/v1"
    assert doc["verify_endpoint"] == "http://test/v1/verify"
    assert again == doc
    # Deterministic from the seed: the same seed on another boot publishes the same key.
    async with kit.running_app(make_settings(signing_key_mldsa=seed)) as h2:
        assert (await h2.http.get("/.well-known/aimart-signing-key")).json() == doc


async def test_ephemeral_signing_key_is_disclosed(free_app: kit.AppHarness) -> None:
    doc = (await free_app.http.get("/.well-known/aimart-signing-key")).json()
    assert doc["ephemeral"] is True


async def test_index_and_health(free_app: kit.AppHarness) -> None:
    index = (await free_app.http.get("/")).json()
    assert index["mcp"] == "http://test/mcp"
    assert index["pricing"] == "http://test/pricing"
    assert index["x402_discovery"] == "http://test/.well-known/x402"
    assert ".csv" in index["formats"]
    assert (await free_app.http.get("/health")).json() == {"status": "ok", "version": __version__}


async def test_free_deployment_advertises_no_paid_resources(free_app: kit.AppHarness) -> None:
    assert free_app.app.state.payments is None
    assert (await free_app.http.get("/.well-known/x402")).json() == {
        "x402Version": 2,
        "resources": [],
    }
    assert (await free_app.http.get("/pricing")).json()["networks"] == []
    body = await convert_free(free_app)
    assert body["report"]["rows"] == 3


def test_payments_enabled_by_default_builds_real_facilitator_clients(
    make_settings: Callable[..., Settings],
) -> None:
    app = create_app(make_settings())
    assert app.state.payments is not None
    assert app.state.payments.accepted == [(kit.BASE_SEPOLIA, kit.PAY_TO_EVM)]
    off = create_app(make_settings(payments_enabled=False, pay_to_evm=None))
    assert off.state.payments is None


# ------------------------------------------------------------------------------ OpenAPI
async def test_openapi_prices_every_paid_route_and_frees_the_rest(
    priced_app: kit.AppHarness,
) -> None:
    spec = (await priced_app.http.get("/openapi.json")).json()
    for op in ACTIVE:
        operation = spec["paths"][op.path][op.method.lower()]
        info = operation["x-payment-info"]
        assert info["price"] == {
            "mode": "fixed",
            "currency": "USD",
            "amount": EXPECTED_PRICE[op.key].removeprefix("$"),
        }, op.key
        assert info["protocols"] == [{"x402": {"version": 2}}]
        assert "402" in operation["responses"]
        assert "security" not in operation
    for path in FREE_DOCUMENTED:
        [operation] = spec["paths"][path].values()
        assert operation["security"] == [], path
        assert "x-payment-info" not in operation, path

    # No route is priced in OpenAPI unless the pricing table lists it.
    priced = {
        (method.upper(), path)
        for path, item in spec["paths"].items()
        for method, operation in item.items()
        if "x-payment-info" in operation
    }
    assert priced == {(op.method, op.path) for op in ACTIVE}


# ------------------------------------------------------------------------------ downloads
async def test_download_link_serves_the_exact_artifact(free_app: kit.AppHarness) -> None:
    artifact = (await convert_free(free_app))["artifact"]
    url = httpx.URL(artifact["download_url"])
    assert url.host == "test"
    r = await free_app.http.get(url.raw_path.decode())
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/vnd.apache.parquet"
    assert 'filename="sales.parquet"' in r.headers["content-disposition"]
    assert hashlib.sha256(r.content).hexdigest() == artifact["sha256"]
    assert len(r.content) == artifact["size_bytes"]
    assert r.content[:4] == b"PAR1"


async def test_download_link_rejects_tampering_and_expiry(free_app: kit.AppHarness) -> None:
    artifact = (await convert_free(free_app))["artifact"]
    url = httpx.URL(artifact["download_url"])
    exp, sig = url.params["exp"], url.params["sig"]
    path = url.path
    store = free_app.app.state.service.store
    past = int(time.time()) - 10

    cases = {
        "tampered sig": f"{path}?exp={exp}&sig={'0' * 32}",
        "extended exp": f"{path}?exp={int(exp) + 3600}&sig={sig}",
        "other id": path.replace(artifact["id"], "Zz" * 12) + f"?exp={exp}&sig={sig}",
        "expired": f"{path}?exp={past}&sig={store.sign(artifact['id'], past)}",
    }
    for name, target in cases.items():
        r = await free_app.http.get(target)
        assert r.status_code == 404, name
        assert r.json()["error"]["code"] == "artifact_not_found", name
        assert r.content[:4] != b"PAR1", name

    # A validly signed link to an artifact that is gone (swept) is a 404, not a 500.
    ghost = "Gh" * 12
    r = await free_app.http.get(
        f"/v1/artifacts/{ghost}/x.parquet?exp={exp}&sig={store.sign(ghost, int(exp))}"
    )
    assert r.status_code == 404
    r = await free_app.http.get(f"{path}?exp={exp}")
    assert r.status_code == 400  # missing sig: validation error, not a download


# ------------------------------------------------------------------------------ uploads
async def test_multipart_upload_converts(free_app: kit.AppHarness) -> None:
    r = await free_app.http.post(
        "/v1/convert/upload",
        files={"file": ("sales.csv", CSV, "text/csv")},
        data={"options": json.dumps({"sort_by": ["units"]})},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["report"]["rows"] == 3
    assert body["artifact"]["filename"] == "sales.parquet"
    r = await free_app.http.post("/v1/convert/upload", files={"file": ("x.csv", CSV)})
    assert r.status_code == 200


async def test_multipart_upload_rejects_bad_options(free_app: kit.AppHarness) -> None:
    r = await free_app.http.post(
        "/v1/convert/upload",
        files={"file": ("sales.csv", CSV)},
        data={"options": json.dumps({"no_such_option": 1})},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


async def test_multipart_upload_over_the_input_limit_is_413(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(max_input_bytes=2 * 1024 * 1024)
    async with kit.running_app(settings, paid=False) as h:
        big = b"a,b\n" + b"1,2\n" * (600 * 1024)  # ~2.4 MB: over the input cap, under the body cap
        r = await h.http.post("/v1/convert/upload", files={"file": ("big.csv", big)})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "input_too_large"
    leftovers = [p for p in settings.artifact_dir.iterdir() if p.name not in {".work"}]
    assert leftovers == []  # the spooled upload was cleaned up


async def test_inline_base64_over_the_input_limit_is_413(
    make_settings: Callable[..., Settings],
) -> None:
    async with kit.running_app(make_settings(max_input_bytes=1000), paid=False) as h:
        r = await h.http.post(
            "/v1/convert", json={"content_base64": kit.b64(b"x" * 2000), "filename": "a.csv"}
        )
    assert r.status_code == 413
    assert r.json()["error"]["details"]["limit_bytes"] == 1000


# ------------------------------------------------------------------------------ body limits
async def test_body_over_limit_with_content_length_is_413(free_app: kit.AppHarness) -> None:
    body = b"{" + b" " * (16 * 1024 * 1024) + b"}"
    r = await free_app.http.post(
        "/v1/convert", content=body, headers={"content-type": "application/json"}
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "input_too_large"


async def _echo_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Reads the whole body; on disconnect it gives up without answering (like a real app)."""
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        size += len(message.get("body", b""))
        if not message.get("more_body"):
            break
    payload = json.dumps({"size": size}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": payload})


def limited_client(limit: int = 100) -> httpx.AsyncClient:
    app = BodyLimitMiddleware(
        _echo_app, default_limit=limit, upload_limit=limit * 10, upload_paths=("/upload",)
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_body_limit_without_content_length_is_413() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(5):
            yield b"x" * 40

    async with limited_client() as client:
        r = await client.post("/x", content=chunks())
        assert "content-length" not in r.request.headers
        assert r.status_code == 413
        assert r.json()["error"]["code"] == "input_too_large"

        r = await client.post("/x", content=b"x" * 100)  # exactly at the limit
        assert r.status_code == 200
        assert r.json() == {"size": 100}
        r = await client.post("/x", content=b"x" * 101)
        assert r.status_code == 413
        r = await client.post("/upload", content=b"x" * 900)  # the upload path's own limit
        assert r.status_code == 200
        r = await client.post("/upload", content=b"x" * 1001)
        assert r.status_code == 413


async def test_body_limit_never_overwrites_a_started_response() -> None:
    async def early(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 202, "headers": []})
        while (await receive())["type"] != "http.disconnect":
            pass
        await send({"type": "http.response.body", "body": b"ok"})

    app = BodyLimitMiddleware(early, default_limit=10, upload_limit=10, upload_paths=())

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b"y" * 8

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/", content=chunks())
    assert r.status_code == 202


def _oversized_json_stream() -> AsyncIterator[bytes]:
    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"content_base64": "'
        for _ in range(17):
            yield b"A" * (1024 * 1024)
        yield b'"}'

    return chunks()


async def test_streamed_body_over_limit_on_the_real_app_is_not_processed(
    free_app: kit.AppHarness,
) -> None:
    r = await free_app.http.post(
        "/v1/convert",
        content=_oversized_json_stream(),
        headers={"content-type": "application/json"},
    )
    assert 400 <= r.status_code < 500, r.text
    assert "artifact" not in r.text
    assert [p for p in free_app.settings.artifact_dir.iterdir() if p.name != ".work"] == []


async def test_streamed_body_over_limit_on_the_real_app_is_413(free_app: kit.AppHarness) -> None:
    r = await free_app.http.post(
        "/v1/convert",
        content=_oversized_json_stream(),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "input_too_large"


# ------------------------------------------------------------------------------ rate limits
async def test_rate_limit_returns_429_with_retry_after(
    make_settings: Callable[..., Settings],
) -> None:
    async with kit.running_app(make_settings(rate_limit_per_minute=3), paid=False) as h:
        statuses = [(await h.http.get("/pricing")).status_code for _ in range(3)]
        limited = await h.http.get("/pricing")
        health = [(await h.http.get("/health")).status_code for _ in range(5)]
    assert statuses == [200, 200, 200]
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    retry = int(limited.headers["retry-after"])
    assert 1 <= retry <= 20  # 3/minute: one token every 20 s
    assert f"Retry after {retry} s." == limited.json()["error"]["hint"]
    assert health == [200] * 5  # health checks are exempt


async def test_rate_limit_buckets_per_forwarded_client_only_when_trusted(
    make_settings: Callable[..., Settings],
) -> None:
    # Only the entry our proxy appended (the rightmost) identifies the caller; anything to its
    # left is caller-controlled, so a fresh fake prefix per request must not buy a fresh bucket.
    async with kit.running_app(
        make_settings(rate_limit_per_minute=1, trust_forwarded_for=True), paid=False
    ) as h:
        a1 = await h.http.get("/pricing", headers={"x-forwarded-for": "1.1.1.1, 203.0.113.1"})
        b1 = await h.http.get("/pricing", headers={"x-forwarded-for": "203.0.113.2"})
        a2 = await h.http.get("/pricing", headers={"x-forwarded-for": "9.9.9.9, 203.0.113.1"})
    assert (a1.status_code, b1.status_code, a2.status_code) == (200, 200, 429)

    async with kit.running_app(
        make_settings(rate_limit_per_minute=1, trust_forwarded_for=True, trusted_proxy_hops=2),
        paid=False,
    ) as h:
        a1 = await h.http.get("/pricing", headers={"x-forwarded-for": "198.51.100.7, 10.0.0.1"})
        b1 = await h.http.get("/pricing", headers={"x-forwarded-for": "198.51.100.8, 10.0.0.1"})
        a2 = await h.http.get(
            "/pricing", headers={"x-forwarded-for": "6.6.6.6, 198.51.100.7, 10.0.0.1"}
        )
    assert (a1.status_code, b1.status_code, a2.status_code) == (200, 200, 429)

    async with kit.running_app(make_settings(rate_limit_per_minute=1), paid=False) as h:
        a1 = await h.http.get("/pricing", headers={"x-forwarded-for": "203.0.113.1"})
        spoof = await h.http.get("/pricing", headers={"x-forwarded-for": "203.0.113.9"})
    assert (a1.status_code, spoof.status_code) == (200, 429)  # untrusted: the header is ignored


async def test_served_app_does_not_let_callers_choose_their_rate_limit_bucket(
    make_settings: Callable[..., Settings],
) -> None:
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from aimart_parquet.cli import forwarded_allow_ips

    async with kit.running_app(make_settings(rate_limit_per_minute=1), paid=False) as h:
        # What `serve` installs for these settings (trust_forwarded_for is off by default).
        served = ProxyHeadersMiddleware(
            h.app, trusted_hosts=forwarded_allow_ips(h.settings.trust_forwarded_for)
        )
        transport = httpx.ASGITransport(app=served)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get(
                "/pricing", headers={"x-forwarded-for": "6.6.6.1, 203.0.113.1"}
            )
            second = await client.get(
                "/pricing", headers={"x-forwarded-for": "6.6.6.2, 203.0.113.1"}
            )
    assert (first.status_code, second.status_code) == (200, 429)


def test_rate_limiter_refills_and_bounds_memory() -> None:
    limiter = RateLimitMiddleware(None, per_minute=60, trust_forwarded_for=False, max_clients=2)  # type: ignore[arg-type]
    assert limiter.allow("a", 0.0)[0]
    for _ in range(59):
        limiter.allow("a", 0.0)
    allowed, retry = limiter.allow("a", 0.0)
    assert not allowed
    assert retry == pytest.approx(1.0)
    assert limiter.allow("a", 1.0)[0]  # one token per second at 60/minute
    limiter.allow("b", 1.0)
    limiter.allow("c", 1.0)
    assert list(limiter.buckets) == ["b", "c"]  # least recently seen client evicted


def test_client_ip() -> None:
    scope: dict[str, Any] = {
        "headers": [(b"x-forwarded-for", b" 1.1.1.1 , 198.51.100.7 , 10.0.0.2")],
        "client": ("10.0.0.3", 1234),
    }
    assert client_ip(scope, trust_forwarded_for=True) == "10.0.0.2"
    assert client_ip(scope, trust_forwarded_for=True, trusted_hops=2) == "198.51.100.7"
    assert client_ip(scope, trust_forwarded_for=True, trusted_hops=9) == "1.1.1.1"
    assert client_ip(scope, trust_forwarded_for=True, trusted_hops=0) == "10.0.0.2"
    assert client_ip(scope, trust_forwarded_for=False) == "10.0.0.3"
    split: dict[str, Any] = {
        "headers": [(b"x-forwarded-for", b"9.9.9.9"), (b"x-forwarded-for", b"8.8.8.8")],
        "client": ("10.0.0.3", 1),
    }
    assert client_ip(split, trust_forwarded_for=True) == "8.8.8.8"  # repeated headers join
    empty: dict[str, Any] = {"headers": [(b"x-forwarded-for", b" , ")], "client": ("1.2.3.4", 1)}
    assert client_ip(empty, trust_forwarded_for=True) == "1.2.3.4"
    assert client_ip({"headers": []}, trust_forwarded_for=False) == "unknown"


# ------------------------------------------------------------------------------ validation
async def test_validation_errors_are_400_with_a_field_list(free_app: kit.AppHarness) -> None:
    r = await free_app.http.post("/v1/query", json={"artifact_id": "x", "limit": 0, "bogus": 1})
    assert r.status_code == 400
    error = r.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["hint"] == "Fix the listed fields."
    fields = {p["field"] for p in error["details"]["problems"]}
    assert fields == {"sql", "limit", "bogus"}
    assert all(p["problem"] for p in error["details"]["problems"])

    r = await free_app.http.post(
        "/v1/inspect", content=b"{nope", headers={"content-type": "application/json"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


async def test_service_errors_map_to_their_status(free_app: kit.AppHarness) -> None:
    r = await free_app.http.post("/v1/inspect", json={"artifact_id": "Zz" * 12})
    assert r.status_code == 404
    error = r.json()["error"]
    assert error["code"] == "artifact_not_found"
    assert error["hint"]


async def test_preview_query_and_inspect_round_trip(free_app: kit.AppHarness) -> None:
    artifact_id = (await convert_free(free_app))["artifact"]["id"]
    inspected = (await free_app.http.post("/v1/inspect", json={"artifact_id": artifact_id})).json()
    assert inspected["rows"] == 3
    preview = (
        await free_app.http.post(
            "/v1/preview", json={"artifact_id": artifact_id, "columns": ["units"], "limit": 2}
        )
    ).json()
    assert preview["columns"] == ["units"]
    assert preview["row_count"] == 2
    query = (
        await free_app.http.post(
            "/v1/query",
            json={
                "artifact_id": artifact_id,
                "sql": "SELECT region FROM data ORDER BY units DESC",
                "limit": 1,
            },
        )
    ).json()
    assert query["rows"] == [["East"]]
    assert query["truncated"] is True


async def test_verify_endpoint_checks_the_stored_file(free_app: kit.AppHarness) -> None:
    artifact = (await convert_free(free_app))["artifact"]
    prov = artifact["provenance"]
    ok = await free_app.http.post(
        "/v1/verify",
        json={
            "manifest": prov["manifest"],
            "signature": prov["signature"],
            "artifact_id": artifact["id"],
        },
    )
    assert ok.json()["valid"] is True
    assert ok.json()["file_matches"] is True

    other = (await convert_free(free_app, b"x,y\n1,2\n"))["artifact"]
    swapped = await free_app.http.post(
        "/v1/verify",
        json={
            "manifest": prov["manifest"],
            "signature": prov["signature"],
            "artifact_id": other["id"],
        },
    )
    body = swapped.json()
    assert body["signature_valid"] is True
    assert body["file_matches"] is False
    assert body["valid"] is False
    assert prov["public_key_url"] == "http://test/.well-known/aimart-signing-key"


# ------------------------------------------------------------------------------ MCP transport
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


async def test_mcp_accepts_the_public_host_and_refuses_others(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(public_base_url="https://aimart.example.com")
    async with kit.running_app(settings, paid=False) as h:
        transport = httpx.ASGITransport(app=h.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://aimart.example.com"
        ) as c:
            ok = await c.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
            ok_origin = await c.post(
                "/mcp",
                json=INITIALIZE,
                headers=MCP_HEADERS | {"origin": "https://aimart.example.com"},
            )
            bad_origin = await c.post(
                "/mcp", json=INITIALIZE, headers=MCP_HEADERS | {"origin": "https://evil.example"}
            )
        async with httpx.AsyncClient(transport=transport, base_url="https://evil.example") as c:
            bad_host = await c.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as c:
            local = await c.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert ok.status_code == 200, ok.text
    assert ok.json()["result"]["serverInfo"]["name"] == "aimart-parquet"
    assert ok_origin.status_code == 200
    assert bad_origin.status_code in {400, 403, 421}
    assert bad_host.status_code in {400, 403, 421}
    assert local.status_code == 200


def test_transport_security_allows_the_public_host_with_any_port(
    make_settings: Callable[..., Settings],
) -> None:
    security = _transport_security(make_settings(public_base_url="https://h.example:8443"))
    assert "h.example:8443" in security.allowed_hosts
    assert "h.example:*" in security.allowed_hosts
    assert "localhost:*" in security.allowed_hosts
    assert "https://h.example:8443" in security.allowed_origins


# ------------------------------------------------------------------------------ logs + lifespan
async def test_access_log_records_settlement_and_request_ids(
    paid_app: kit.AppHarness, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="aimart.access"):
        unpaid = await paid_app.http.post("/v1/inspect", json={"artifact_id": "Zz" * 12})
        paid = await paid_app.paying.post(
            "/v1/convert", json={"content_base64": kit.b64(CSV), "filename": "s.csv"}
        )
        await paid_app.http.get("/health")
    assert len(unpaid.headers["x-request-id"]) == 16
    assert paid.status_code == 200
    records = [r for r in caplog.records if r.name == "aimart.access"]
    by_path = [(r.path, r.status, r.payment_settled) for r in records]
    assert ("/v1/inspect", 402, False) in by_path
    assert ("/v1/convert", 200, True) in by_path
    assert all(r.path != "/health" for r in records)
    assert all(isinstance(r.duration_ms, int) for r in records)


async def test_background_sweeper_removes_expired_artifacts_and_survives_errors(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "SWEEP_INTERVAL_S", 0.01)
    settings = make_settings(artifact_ttl_s=-1)
    async with kit.running_app(settings, paid=False) as h:
        store = h.app.state.service.store
        calls: list[int] = []
        real = store.sweep

        def flaky() -> int:
            calls.append(1)
            if len(calls) == 1:
                raise OSError("disk hiccup")
            return real()

        monkeypatch.setattr(store, "sweep", flaky)
        art = store.put(kit.write(settings.artifact_dir / "in.bin", b"x"), "x.parquet")
        with anyio.fail_after(5):
            # The effect under test is a background task's: there is no event to wait on.
            while (settings.artifact_dir / art.id).exists():  # noqa: ASYNC110
                await anyio.sleep(0.01)
    assert len(calls) >= 2  # kept running after the first sweep raised


async def test_converted_parquet_is_typed(free_app: kit.AppHarness, tmp_path: Path) -> None:
    artifact = (await convert_free(free_app))["artifact"]
    url = httpx.URL(artifact["download_url"])
    r = await free_app.http.get(url.raw_path.decode())
    out = tmp_path / "x.parquet"
    out.write_bytes(r.content)
    table = pq.read_table(out)
    assert table.column_names == ["region", "units"]
    assert str(table.schema.field("units").type).startswith("int")
