"""CDP's facilitator over the wire: every request carries a JWT CDP would accept, and a key CDP
refuses costs the listing, never a sale.

The real app, with the service's own CDP client (``payments/cdp.CdpFacilitatorClient``, what
``Payments.from_settings`` builds when the owner sets a key) and only the socket replaced by a
fake CDP that checks each request's ``Authorization`` the way CDP must: the JWT's signature
against the key's public half, its ``uris`` against the request's own method, host and path,
and its lifetime. Anything else is CDP's 401. Every key is made in memory for the test.
"""

from __future__ import annotations

import base64
import functools
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from x402.http import FacilitatorConfig

import aimart_testkit as kit
from aimart_parquet.payments.cdp import (
    CDP_FACILITATOR_URL,
    CdpAuth,
    CdpFacilitatorClient,
    CdpKeyError,
    check_key,
    load_key,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

KEY_ID = "4f2a9c1e-7b3d-4e8a-9f60-1c2d3e4f5a6b"
CSV = b"region,units\nNorth,10\nSouth,20\n"
BODY = {"content_base64": kit.b64(CSV), "filename": "sales.csv"}
TX = kit.TX_HASH
REFUSED = {"errorType": "unauthorized", "errorMessage": "Unauthorized."}


def new_secret() -> tuple[str, ed25519.Ed25519PublicKey]:
    key = ed25519.Ed25519PrivateKey.generate()
    raw = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    ) + key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode(), key.public_key()


def _part(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class FakeCdp:
    """CDP's facilitator on a mock socket: 401 unless the request's JWT is right for it."""

    def __init__(
        self,
        public: ed25519.Ed25519PublicKey,
        *,
        refuse: tuple[str, ...] = (),
        settle: tuple[int, Any] = (200, {"success": True, "transaction": TX, "network": None}),
    ) -> None:
        self.public = public
        self.refuse = refuse
        self.settle = settle
        self.requests: list[httpx.Request] = []
        self.accepted: list[str] = []  # endpoints whose JWT checked out

    def authorized(self, request: httpx.Request) -> bool:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme != "Bearer" or token.count(".") != 2:
            return False
        head, body, signature = token.split(".")
        header, claims = json.loads(_part(head)), json.loads(_part(body))
        try:
            self.public.verify(_part(signature), f"{head}.{body}".encode())
        except Exception:  # noqa: BLE001 - any failure is CDP's 401
            return False
        now = time.time()
        return (
            header.get("alg") == "EdDSA"
            and header.get("kid") == KEY_ID == claims.get("sub")
            and claims.get("iss") == "cdp"
            and claims["nbf"] - 5 <= now < claims["exp"]
            and claims.get("uris") == [f"{request.method} {request.url.host}{request.url.path}"]
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint in self.refuse or not self.authorized(request):
            return httpx.Response(401, json=REFUSED)
        self.accepted.append(endpoint)
        if endpoint == "supported":
            kinds = [
                {"x402Version": 2, "scheme": "exact", "network": n}
                for n in (kit.BASE_SEPOLIA, "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1")
            ]
            return httpx.Response(200, json={"kinds": kinds, "extensions": ["bazaar"]})
        if endpoint == "verify":
            return httpx.Response(200, json={"isValid": True, "payer": kit.PAYER})
        status, answer = self.settle
        if isinstance(answer, dict) and "network" in answer and answer["network"] is None:
            answer = {**answer, "network": kit.BASE_SEPOLIA}
        return httpx.Response(status, json=answer)

    def bodies(self, endpoint: str) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith(f"/{endpoint}")]


@pytest.fixture
def secret() -> tuple[str, ed25519.Ed25519PublicKey]:
    return new_secret()


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Callable[[FakeCdp], CdpFacilitatorClient]:
    return cdp_wire(monkeypatch)


def cdp_wire(monkeypatch: pytest.MonkeyPatch) -> Callable[[FakeCdp], CdpFacilitatorClient]:
    """The service's CDP client with its sockets, async and the SDK's sync one, on ``fake``."""

    def build(fake: FakeCdp, secret: str) -> CdpFacilitatorClient:
        transport = httpx.MockTransport(fake)
        # GET /supported goes through the SDK's own httpx.Client, made per call.
        monkeypatch.setattr(httpx, "Client", functools.partial(httpx.Client, transport=transport))
        return CdpFacilitatorClient(
            FacilitatorConfig(
                url=CDP_FACILITATOR_URL,
                http_client=httpx.AsyncClient(transport=transport),
                auth_provider=CdpAuth(KEY_ID, load_key(secret)),
            ),
            networks=frozenset({kit.BASE_SEPOLIA}),  # testnet here, as the paying client is
        )

    return build  # type: ignore[return-value]


def all_logged(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(r.getMessage() + repr(r.__dict__) for r in caplog.records)


@pytest.fixture
def no_secret_logged(caplog: pytest.LogCaptureFixture, secret: tuple[str, Any]) -> Iterator[None]:
    caplog.set_level(logging.INFO)
    yield
    text = all_logged(caplog)
    assert secret[0] not in text
    assert secret[0][:20] not in text
    assert KEY_ID not in text
    assert "Bearer " not in text


# ------------------------------------------------------------------------------ authenticated
@pytest.mark.usefixtures("no_secret_logged")
async def test_a_paid_call_is_verified_and_settled_by_cdp_with_its_jwt(
    settings: Settings, secret: tuple[str, Any], wire: Any
) -> None:
    fake = FakeCdp(secret[1])
    behind = kit.FakeFacilitator()  # xpay's place: untouched while CDP answers
    async with kit.running_app(settings, facilitators=[wire(fake, secret[0]), behind]) as app:
        response = await app.paying.post("/v1/convert", json=BODY)
    assert response.status_code == 200, response.text
    receipt = kit.payment_response(response)
    assert receipt["success"] is True
    assert receipt["transaction"] == TX
    # Each endpoint was called with a JWT made for it (the fake answers 401 otherwise).
    assert fake.accepted == ["supported", "verify", "settle"]
    assert [r.method for r in fake.requests] == ["GET", "POST", "POST"]
    assert (behind.verified, behind.settled) == ([], [])
    # What lists the route in CDP's Bazaar: the payload CDP settles echoes the 402's
    # declaration, with the route's own absolute URL.
    [settled] = fake.bodies("settle")
    payload = settled["paymentPayload"]
    assert "bazaar" in payload["extensions"]
    assert payload["resource"]["url"] == f"{kit.BASE_URL}/v1/convert"
    assert settled["paymentRequirements"]["amount"] == "20000"


async def test_without_the_jwt_the_same_call_is_refused_by_cdp(
    settings: Settings, secret: tuple[str, Any], wire: Any
) -> None:
    """The fake's check is real: the same client with no key gets 401 and serves nothing."""
    fake = FakeCdp(secret[1])
    client = wire(fake, secret[0])
    client._auth_provider = None
    async with kit.running_app(settings, facilitators=[client]) as app:
        response = await app.paying.post("/v1/convert", json=BODY)
    assert response.status_code == 503  # nobody serves Base Sepolia: unpaid
    assert fake.accepted == []


@pytest.mark.usefixtures("no_secret_logged")
async def test_a_settle_answer_outside_the_schema_is_still_read_leniently(
    settings: Settings, secret: tuple[str, Any], wire: Any
) -> None:
    fake = FakeCdp(secret[1], settle=(201, {"success": True, "txHash": TX}))
    async with kit.running_app(settings, facilitators=[wire(fake, secret[0])]) as app:
        response = await app.paying.post("/v1/convert", json=BODY)
    assert response.status_code == 200, response.text
    assert kit.payment_response(response)["transaction"] == TX


# ------------------------------------------------------------------------------ refused
@pytest.mark.usefixtures("no_secret_logged")
async def test_a_key_refused_at_start_skips_cdp_and_xpay_serves(
    settings: Settings, secret: tuple[str, Any], wire: Any, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeCdp(secret[1], refuse=("supported", "verify", "settle"))
    behind = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitators=[wire(fake, secret[0]), behind]) as app:
        response = await app.paying.post("/v1/convert", json=BODY)
    assert response.status_code == 200, response.text
    assert (len(behind.verified), len(behind.settled)) == (1, 1)
    assert [r.url.path.rsplit("/", 1)[-1] for r in fake.requests] == ["supported"]
    refused = [r for r in caplog.records if "refused this service's API key" in r.getMessage()]
    assert len(refused) == 1
    assert refused[0].levelno == logging.ERROR
    assert refused[0].facilitator == CDP_FACILITATOR_URL  # type: ignore[attr-defined]
    assert refused[0].status == 401  # type: ignore[attr-defined]
    assert refused[0].endpoint == "supported"  # type: ignore[attr-defined]
    assert "AIMART_CDP_API_KEY_ID" in refused[0].getMessage()
    skipped = [
        r for r in caplog.records if r.getMessage() == "facilitator skipped: GET /supported failed"
    ]
    assert [getattr(r, "facilitator", None) for r in skipped] == [CDP_FACILITATOR_URL]


@pytest.mark.parametrize("endpoint", ["verify", "settle"])
@pytest.mark.usefixtures("no_secret_logged")
async def test_a_key_refused_mid_call_is_an_unpaid_failure_and_an_error_line(
    endpoint: str,
    settings: Settings,
    secret: tuple[str, Any],
    wire: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeCdp(secret[1], refuse=(endpoint,))
    async with kit.running_app(settings, facilitators=[wire(fake, secret[0])]) as app:
        response = await app.paying.post("/v1/convert", json=BODY)
    # Never a 200 without a settlement: the caller is not charged and gets no result.
    assert response.status_code == 402, response.text
    calls = [r.url.path.rsplit("/", 1)[-1] for r in fake.requests]
    assert calls.count("settle") == (1 if endpoint == "settle" else 0)
    refused = [r for r in caplog.records if "refused this service's API key" in r.getMessage()]
    assert [(r.levelno, r.endpoint) for r in refused] == [(logging.ERROR, endpoint)]  # type: ignore[attr-defined]
    seen = response.text + "".join(f"{k}: {v}" for k, v in response.headers.items())
    assert secret[0] not in seen
    assert "Bearer" not in seen


# ------------------------------------------------------------------------------ the selftest
@pytest.fixture
def cdp_socket(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], list[httpx.Request]]:
    def install(answer: Any) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if isinstance(answer, Exception):
                raise answer
            return answer(request) if callable(answer) else answer

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
        )
        return seen

    return install


async def test_the_deploy_check_passes_a_key_cdp_accepts(
    make_settings: Callable[..., Settings], secret: tuple[str, Any], cdp_socket: Any
) -> None:
    fake = FakeCdp(secret[1])
    seen = cdp_socket(fake)
    mainnet = make_settings(
        network_mode="mainnet", cdp_api_key_id=KEY_ID, cdp_api_key_secret=secret[0]
    )
    said = await check_key(mainnet)
    # Base Sepolia is what the fake lists: on mainnet the check says Base is missing.
    assert said == "CDP accepted the key; it does NOT list x402 v2 'exact' on eip155:8453"
    testnet = make_settings(cdp_api_key_id=KEY_ID, cdp_api_key_secret=secret[0])
    assert await check_key(testnet) == (
        "CDP accepted the key; it lists x402 v2 'exact' on eip155:84532"
    )
    # One money-free request each: GET /supported, nothing else.
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/platform/v2/x402/supported")] * 2
    assert fake.accepted == ["supported", "supported"]


@pytest.mark.parametrize("status", [401, 403])
async def test_the_deploy_check_fails_on_a_refused_key_without_naming_it(
    status: int,
    make_settings: Callable[..., Settings],
    secret: tuple[str, Any],
    cdp_socket: Any,
) -> None:
    cdp_socket(httpx.Response(status, json=REFUSED))
    settings = make_settings(cdp_api_key_id=KEY_ID, cdp_api_key_secret=secret[0])
    with pytest.raises(CdpKeyError, match=f"CDP refused the API key \\(HTTP {status}\\)") as raised:
        await check_key(settings)
    assert secret[0] not in str(raised.value)
    assert KEY_ID not in str(raised.value)


@pytest.mark.parametrize(
    ("answer", "says"),
    [
        (httpx.Response(503, text="<html>down</html>"), "CDP answered 503"),
        (httpx.ConnectError("no route"), "CDP not reached (ConnectError)"),
        (httpx.Response(200, text="not json"), "does NOT list"),
    ],
)
async def test_a_cdp_outage_does_not_fail_the_deploy(
    answer: Any,
    says: str,
    make_settings: Callable[..., Settings],
    secret: tuple[str, Any],
    cdp_socket: Any,
) -> None:
    cdp_socket(answer)
    settings = make_settings(cdp_api_key_id=KEY_ID, cdp_api_key_secret=secret[0])
    assert says in await check_key(settings)


async def test_the_deploy_check_without_a_key_asks_nobody_and_a_half_key_fails(
    make_settings: Callable[..., Settings], secret: tuple[str, Any], cdp_socket: Any
) -> None:
    seen = cdp_socket(httpx.Response(500))
    assert "no CDP key configured" in await check_key(make_settings())
    with pytest.raises(CdpKeyError, match="set both"):
        await check_key(make_settings(cdp_api_key_secret=secret[0]))
    with pytest.raises(CdpKeyError, match="neither an Ed25519"):
        await check_key(make_settings(cdp_api_key_id=KEY_ID, cdp_api_key_secret="x" * 88))
    assert seen == []
