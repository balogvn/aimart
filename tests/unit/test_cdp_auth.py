"""CDP's facilitator: the JWT on each request, the key's reading, and the facilitator order.

CDP indexes a route in its Bazaar (and so on agentic.market) only when it settles a payment
for it, and its /verify, /settle and /supported need a CDP Secret API key: a JWT per request,
as Coinbase's SDKs make it (cdp-sdk 1.53.0 ``cdp/auth/utils/jwt.py``, @coinbase/cdp-sdk 1.62.0
``auth/utils/jwt.js``). A wrong JWT is a 401, which moves Base to xpay: sales go on, unlisted.
A key in a log line is a leaked key. Every key here is made in memory for the test.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import TYPE_CHECKING, Any

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from aimart_parquet.payments import DEFAULT_FACILITATORS, Payments, facilitator_urls
from aimart_parquet.payments import cdp as cdp_module
from aimart_parquet.payments.cdp import (
    CDP_FACILITATOR_URL,
    JWT_LIFETIME_S,
    CdpAuth,
    CdpFacilitatorClient,
    CdpKeyError,
    load_key,
    load_key_id,
    make_jwt,
)
from aimart_parquet.payments.settle import LenientSettleClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from aimart_parquet.config import Settings

KEY_ID = "4f2a9c1e-7b3d-4e8a-9f60-1c2d3e4f5a6b"
ORG_KEY_ID = f"organizations/0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d/apiKeys/{KEY_ID}"
HOST = "api.cdp.coinbase.com"
XPAY, PRIMER, DEXTER = DEFAULT_FACILITATORS[cdp_module.NetworkMode.MAINNET]


def ed25519_secret() -> tuple[str, ed25519.Ed25519PublicKey]:
    """A key as CDP's portal gives one: base64 of the 32-byte seed then the public key."""
    key = ed25519.Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(seed + public).decode(), key.public_key()


def ecdsa_secret(
    fmt: serialization.PrivateFormat = serialization.PrivateFormat.TraditionalOpenSSL,
) -> tuple[str, ec.EllipticCurvePublicKey]:
    """A key as the portal gives an ECDSA one: '-----BEGIN EC PRIVATE KEY-----' (SEC1)."""
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, fmt, serialization.NoEncryption())
    return pem.decode(), key.public_key()


def b64url(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def decode(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    head, body, signature = token.split(".")
    return (
        json.loads(b64url(head)),
        json.loads(b64url(body)),
        f"{head}.{body}".encode(),
        b64url(signature),
    )


def check_signature(token: str, public: Any) -> dict[str, Any]:
    """Verify as CDP must: the JWS signing input against the key's public half."""
    header, claims, signed, signature = decode(token)
    if isinstance(public, ed25519.Ed25519PublicKey):
        assert header["alg"] == "EdDSA"
        assert len(signature) == 64
        public.verify(signature, signed)
    else:
        assert header["alg"] == "ES256"
        # RFC 7518: R || S, 32 bytes each; DER here is a signature no JWT library accepts.
        assert len(signature) == 64
        r, s = (int.from_bytes(signature[i : i + 32], "big") for i in (0, 32))
        public.verify(encode_dss_signature(r, s), signed, ec.ECDSA(hashes.SHA256()))
    return claims


# ------------------------------------------------------------------------------ the JWT
@pytest.mark.parametrize(
    "make",
    [
        pytest.param(ed25519_secret, id="ed25519"),
        pytest.param(ecdsa_secret, id="ecdsa-sec1"),
        pytest.param(lambda: ecdsa_secret(serialization.PrivateFormat.PKCS8), id="ecdsa-pkcs8"),
    ],
)
def test_the_jwt_is_signed_by_the_key_and_says_what_cdp_reads(make: Callable[[], Any]) -> None:
    secret, public = make()
    token = make_jwt(KEY_ID, load_key(secret), "post", f"{CDP_FACILITATOR_URL}/settle", now=1000)
    claims = check_signature(token, public)
    header = decode(token)[0]
    assert set(header) == {"alg", "kid", "typ", "nonce"}
    assert header["kid"] == KEY_ID
    assert header["typ"] == "JWT"
    assert len(header["nonce"]) == 32
    int(header["nonce"], 16)  # hex, as @coinbase/cdp-sdk makes it
    assert claims == {
        "sub": KEY_ID,
        "iss": "cdp",
        "iat": 1000,
        "nbf": 1000,
        "exp": 1000 + 120,
        # The request it is good for, and only that one: method, host and path, no scheme.
        "uris": [f"POST {HOST}/platform/v2/x402/settle"],
    }
    assert JWT_LIFETIME_S == 120


def test_a_tampered_jwt_does_not_verify() -> None:
    """The check above can fail: a claim changed after signing is caught."""
    secret, public = ed25519_secret()
    token = make_jwt(KEY_ID, load_key(secret), "POST", f"{CDP_FACILITATOR_URL}/settle")
    head, _, signature = token.split(".")
    claims = decode(token)[1] | {"uris": [f"POST {HOST}/platform/v2/x402/verify"]}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    with pytest.raises(InvalidSignature):
        check_signature(f"{head}.{body}.{signature}", public)


def test_each_endpoint_gets_its_own_fresh_jwt_and_the_lists_none() -> None:
    secret, public = ed25519_secret()
    auth = CdpAuth(KEY_ID, load_key(secret))
    first, second = auth.get_auth_headers(), auth.get_auth_headers()
    for endpoint, method in (("verify", "POST"), ("settle", "POST"), ("supported", "GET")):
        header = getattr(first, endpoint)["Authorization"]
        assert header.startswith("Bearer ")
        claims = check_signature(header.removeprefix("Bearer "), public)
        assert claims["uris"] == [f"{method} {HOST}/platform/v2/x402/{endpoint}"]
        # Made per request: never one token replayed for every call.
        assert getattr(second, endpoint)["Authorization"] != header
    assert first.bazaar == {}  # CDP's discovery is public


def test_the_lifetime_is_counted_from_now() -> None:
    secret, _ = ed25519_secret()
    token = make_jwt(KEY_ID, load_key(secret), "GET", f"{CDP_FACILITATOR_URL}/supported")
    claims = decode(token)[1]
    import time

    assert abs(claims["nbf"] - time.time()) < 5
    assert claims["exp"] - claims["nbf"] == 120


# ------------------------------------------------------------------------------ the key
def test_a_key_pasted_with_literal_line_breaks_or_spaces_is_read() -> None:
    pem, public = ecdsa_secret()
    escaped = pem.strip().replace("\n", "\\n")  # what an env var holds when \n was typed
    for pasted in (escaped, f"  {pem}\r\n"):
        check_signature(make_jwt(KEY_ID, load_key(pasted), "GET", CDP_FACILITATOR_URL), public)
    secret, ed_public = ed25519_secret()
    wrapped = f" {secret[:44]}\n{secret[44:]} \n"
    check_signature(make_jwt(KEY_ID, load_key(wrapped), "GET", CDP_FACILITATOR_URL), ed_public)


def _rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _p384_pem() -> str:
    key = ec.generate_private_key(ec.SECP384R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


@pytest.mark.parametrize(
    ("make", "says"),
    [
        (lambda: base64.b64encode(b"k" * 32).decode(), "base64 of 64 bytes"),
        (lambda: "not a key at all", "neither an Ed25519 secret"),
        (lambda: base64.b64encode(b"k" * 64).decode() + "!", "neither an Ed25519 secret"),
        (_rsa_pem, "not an ECDSA P-256 private key"),
        (_p384_pem, "not an ECDSA P-256 private key"),
        (lambda: "-----BEGIN EC PRIVATE KEY-----\nAAAA\n-----END EC PRIVATE KEY-----", "P-256"),
        (lambda: json.dumps({"id": KEY_ID, "privateKey": ed25519_secret()[0]}), "whole"),
    ],
)
def test_a_key_that_cannot_be_used_is_named_without_its_value(
    make: Callable[[], str], says: str
) -> None:
    secret = make()
    with pytest.raises(CdpKeyError, match=says) as raised:
        load_key(secret)
    message = str(raised.value)
    assert "AIMART_CDP_API_KEY_SECRET" in message
    for piece in secret.split():
        if len(piece) >= 8 and "-----" not in piece:
            assert piece not in message
    assert raised.value.__cause__ is None or secret not in str(raised.value.__cause__)


def test_key_ids_in_both_forms_are_read_and_a_swapped_pair_is_named() -> None:
    assert load_key_id(f" {KEY_ID}\n") == KEY_ID
    assert load_key_id(ORG_KEY_ID) == ORG_KEY_ID
    secret, _ = ed25519_secret()
    with pytest.raises(CdpKeyError, match="swapped") as raised:
        load_key_id(secret)
    assert secret not in str(raised.value)


def test_settings_strip_the_pasted_values_and_never_show_them(
    make_settings: Callable[..., Settings],
) -> None:
    secret, _ = ed25519_secret()
    settings = make_settings(cdp_api_key_id=f"{KEY_ID}\r\n", cdp_api_key_secret=f" {secret}\n")
    assert settings.cdp_api_key_id is not None
    assert settings.cdp_api_key_secret is not None
    assert settings.cdp_api_key_id.get_secret_value() == KEY_ID
    assert settings.cdp_api_key_secret.get_secret_value() == secret
    shown = repr(settings) + str(settings) + settings.model_dump_json()
    assert secret not in shown
    assert KEY_ID not in shown
    auth = cdp_module.load_auth(settings)
    assert auth is not None
    assert secret not in repr(auth)
    assert KEY_ID not in repr(auth)


# ------------------------------------------------------------------------------ the order
@pytest.fixture
def keyed(make_settings: Callable[..., Settings]) -> Callable[..., Settings]:
    secret, _ = ed25519_secret()

    def make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "network_mode": "mainnet",
            "facilitator_urls": None,
            "cdp_api_key_id": KEY_ID,
            "cdp_api_key_secret": secret,
        }
        return make_settings(**(values | overrides))

    return make


def test_with_a_key_on_mainnet_cdp_comes_first_and_the_others_stay_behind(
    keyed: Callable[..., Settings],
) -> None:
    settings = keyed()
    assert facilitator_urls(settings) == [CDP_FACILITATOR_URL, XPAY, PRIMER, DEXTER]
    clients = Payments.from_settings(settings)._facilitators
    assert [getattr(c, "url", None) for c in clients] == facilitator_urls(settings)
    first = clients[0]
    assert isinstance(first, CdpFacilitatorClient)
    assert isinstance(first, LenientSettleClient)  # settle answers keep the lenient reading
    assert first.networks == frozenset({"eip155:8453"})  # Base only: Dexter keeps Solana
    assert isinstance(first._auth_provider, CdpAuth)
    assert first._timeout == settings.facilitator_timeout_s
    for other in clients[1:]:
        assert type(other) is LenientSettleClient
        assert other._auth_provider is None  # the key goes to CDP and nowhere else


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"cdp_api_key_id": None, "cdp_api_key_secret": None}, id="no-key"),
        pytest.param({"cdp_api_key_id": "  ", "cdp_api_key_secret": "\n"}, id="blank-key"),
    ],
)
def test_without_a_key_nothing_changes(
    keyed: Callable[..., Settings], overrides: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    settings = keyed(**overrides)
    assert facilitator_urls(settings) == [XPAY, PRIMER, DEXTER]
    clients = Payments.from_settings(settings)._facilitators
    assert [type(c) for c in clients] == [LenientSettleClient] * 3
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_on_testnet_a_key_changes_nothing(keyed: Callable[..., Settings]) -> None:
    settings = keyed(network_mode="testnet")
    assert facilitator_urls(settings) == ["https://x402.org/facilitator"]


def test_an_explicit_list_is_the_whole_list_and_cdp_in_it_gets_the_key(
    keyed: Callable[..., Settings],
) -> None:
    alone = keyed(facilitator_urls=XPAY)
    assert facilitator_urls(alone) == [XPAY]
    named = keyed(facilitator_urls=f"{XPAY}, {CDP_FACILITATOR_URL}/", network_mode="testnet")
    clients = Payments.from_settings(named)._facilitators
    assert type(clients[0]) is LenientSettleClient
    assert isinstance(clients[1], CdpFacilitatorClient)
    assert clients[1].networks == frozenset({"eip155:84532"})  # Base Sepolia on testnet


@pytest.mark.parametrize(
    ("overrides", "says"),
    [
        pytest.param({"cdp_api_key_secret": None}, "set both", id="secret-missing"),
        pytest.param({"cdp_api_key_id": None}, "set both", id="id-missing"),
        pytest.param({"cdp_api_key_secret": "x" * 88}, "neither an Ed25519", id="bad-secret"),
        pytest.param({"cdp_api_key_id": "my key"}, "not a CDP API key ID", id="bad-id"),
    ],
)
def test_a_key_that_cannot_be_used_leaves_cdp_out_with_one_error_and_the_service_starts(
    keyed: Callable[..., Settings],
    overrides: dict[str, Any],
    says: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = keyed(**overrides)
    with caplog.at_level(logging.INFO):
        clients = Payments.from_settings(settings)._facilitators
    assert [getattr(c, "url", None) for c in clients] == [XPAY, PRIMER, DEXTER]
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, [r.getMessage() for r in errors]
    assert says in errors[0].getMessage()
    assert "CDP facilitator not used" in errors[0].getMessage()
    secret = settings.cdp_api_key_secret.get_secret_value() if settings.cdp_api_key_secret else ""
    for record in caplog.records:
        text = record.getMessage() + repr(record.__dict__)
        assert not secret or secret not in text
        assert KEY_ID not in text


def test_cdp_takes_only_base_from_its_supported_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    from x402 import SupportedResponse
    from x402.schemas import SupportedKind

    listed = SupportedResponse(
        kinds=[
            SupportedKind(x402_version=2, scheme="exact", network=n)
            for n in ("eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "eip155:137")
        ],
        extensions=["bazaar"],
    )
    monkeypatch.setattr(LenientSettleClient, "get_supported", lambda self: listed)
    client = CdpFacilitatorClient(cdp_module.FacilitatorConfig(url=CDP_FACILITATOR_URL))
    answer = client.get_supported()
    assert [k.network for k in answer.kinds] == ["eip155:8453"]
    assert answer.extensions == ["bazaar"]
