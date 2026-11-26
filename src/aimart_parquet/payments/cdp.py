"""Coinbase's CDP facilitator: the one route into the CDP Bazaar and agentic.market.

CDP indexes a resource in its Bazaar when it settles a payment for it whose 402 carried the
``bazaar`` extension (this service's REST 402s do; its MCP 402s carry none, and CDP lists only
web resources today), and agentic.market lists what the Bazaar holds. xpay and Primer settle
as well and charge nothing, but publish no catalog, so real sales through them listed the
service nowhere (docs/discovery.md). CDP charges $0.001 per
settlement after 1,000 a month (docs/economics.md) and needs a CDP Secret API key, so it is
used only when the owner configured one, on mainnet, and only for Base: xpay and Primer stay
behind it, and Dexter keeps Solana.

Authentication, as Coinbase's own SDKs do it (PyPI ``cdp-sdk`` 1.53.0,
``cdp/auth/utils/jwt.py``; npm ``@coinbase/cdp-sdk`` 1.62.0 ``auth/utils/jwt.js``, which
``@coinbase/x402`` 2.1.0 calls for its facilitator): every request carries
``Authorization: Bearer <JWT>``, a JWT made for that one request and valid 120 s. Its header
names the algorithm (``EdDSA`` for an Ed25519 key, ``ES256`` for an ECDSA P-256 key), ``kid``
(the key's ID), ``typ: JWT`` and a random ``nonce``; its claims are ``sub`` (the key's ID),
``iss: "cdp"``, ``iat``/``nbf`` (now), ``exp`` (now + 120) and ``uris``, one entry binding it to
the request: ``"POST api.cdp.coinbase.com/platform/v2/x402/settle"``. CDP's API marks
``/verify``, ``/settle`` and ``/supported`` as needing the key and the ``/discovery/*`` lists
as public (``cdp/openapi_client/public_operations.py``). Built here with ``cryptography``
(already a dependency); the x402 SDK only carries headers (``AuthProvider``), it makes none.

The key never reaches a log line, an exception message or a ``repr``: only the parsed private
key object is kept, and every message about a bad key names the setting, not its value.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import secrets
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from pydantic import ValidationError
from x402.http import FacilitatorConfig
from x402.http.facilitator_client import _attach_extension_responses, _parse_facilitator_response
from x402.http.facilitator_client_base import AuthHeaders
from x402.schemas import VerifyResponse

from aimart_parquet.config import NetworkMode
from aimart_parquet.payments.settle import LenientSettleClient

if TYPE_CHECKING:
    from x402 import SupportedResponse

    from aimart_parquet.config import Settings

log = logging.getLogger("aimart.payments.cdp")

CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
# The networks CDP is given: Base only (mainnet, or Sepolia when an operator names CDP on
# testnet). Solana stays with Dexter, which is free.
BASE_MAINNET = "eip155:8453"
BASE_SEPOLIA = "eip155:84532"
JWT_LIFETIME_S = 120  # what both of Coinbase's SDKs give a request's JWT
AUTH_REFUSED = frozenset({401, 403})
# CDP's 402 on settle: ``PaymentMethodRequiredError`` (cdp-sdk openapi.yaml), "A valid payment
# method is required ... Please add a payment method to your account at
# https://portal.cdp.coinbase.com", once the account's free settlements are used up. Like a
# refused key, it does not pass by itself.
PAYMENT_METHOD_REQUIRED = 402
SETTINGS_NAMES = "AIMART_CDP_API_KEY_ID and AIMART_CDP_API_KEY_SECRET"
# Verify answers in which CDP says it could not read the chain (cdp-sdk openapi.yaml,
# ``x402VerifyInvalidReason``): CDP's failure, not the payer's answer, so the call is verified
# by the next facilitator and CDP is rested, as for a 5xx. Reasons that may be the payer's
# doing (``..._failed_to_parse_signature``, ``..._failed_to_verify_signature``,
# ``..._failed_to_execute_transfer``) stay the payer's answer.
CHAIN_UNREAD = frozenset(
    {
        "invalid_exact_evm_failed_to_get_network_config",
        "invalid_exact_evm_failed_to_check_nonce",
        "invalid_exact_evm_failed_to_get_balance",
        "invalid_exact_evm_failed_to_check_deployment",
        "invalid_exact_evm_failed_to_get_receipt",
    }
)

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
# A Secret API key's ID: a bare UUID (Ed25519 keys, as the portal shows them) or the older
# 'organizations/<uuid>/apiKeys/<uuid>' (cdp-sdk's JwtOptions docstring gives both).
KEY_ID = re.compile(rf"{_UUID}|organizations/{_UUID}/apiKeys/{_UUID}")

PrivateKey = ed25519.Ed25519PrivateKey | ec.EllipticCurvePrivateKey


class CdpKeyError(ValueError):
    """A CDP key that cannot be used. The message never contains any part of the key."""


def load_key_id(key_id: str) -> str:
    key_id = key_id.strip()
    if not KEY_ID.fullmatch(key_id):
        msg = (
            "AIMART_CDP_API_KEY_ID is not a CDP API key ID (a UUID, or "
            "organizations/<uuid>/apiKeys/<uuid>); were the ID and the secret swapped?"
        )
        raise CdpKeyError(msg)
    return key_id


def load_key(secret: str) -> PrivateKey:
    """The private key of a CDP Secret API key, as Coinbase's SDKs read it.

    Ed25519 (the portal's default): base64 of 64 bytes, a 32-byte seed then the public key.
    ECDSA: a PEM block, 'EC PRIVATE KEY' (as the portal gives it) or PKCS #8, on the P-256
    curve. A secret pasted with literal '\\n' in place of line breaks is read as cdp-sdk reads
    it.
    """
    text = secret.strip().replace("\\n", "\n")
    if text.startswith("{"):
        msg = (
            "AIMART_CDP_API_KEY_SECRET holds the whole downloaded key file: paste only the "
            "value of its privateKey field"
        )
        raise CdpKeyError(msg)
    if "-----BEGIN" in text:
        try:
            key = serialization.load_pem_private_key(text.encode(), password=None)
        except (ValueError, TypeError):
            key = None
        if isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1):
            return key
        msg = "AIMART_CDP_API_KEY_SECRET is a PEM block but not an ECDSA P-256 private key"
        raise CdpKeyError(msg)
    try:
        raw = base64.b64decode("".join(text.split()), validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != 64:
        msg = (
            "AIMART_CDP_API_KEY_SECRET is neither an Ed25519 secret (base64 of 64 bytes, "
            "88 characters ending in ==) nor an ECDSA PEM block"
        )
        raise CdpKeyError(msg)
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _json(data: dict[str, Any]) -> bytes:
    return json.dumps(data, separators=(",", ":")).encode()


def make_jwt(key_id: str, key: PrivateKey, method: str, url: str, *, now: int | None = None) -> str:
    """The JWT CDP expects on one request: ``method`` ``url`` (the facilitator's full URL)."""
    parts = urlsplit(url)
    issued = int(time.time()) if now is None else now
    algorithm = "EdDSA" if isinstance(key, ed25519.Ed25519PrivateKey) else "ES256"
    header = {
        "alg": algorithm,
        "kid": key_id,
        "typ": "JWT",
        "nonce": secrets.token_hex(16),  # as @coinbase/cdp-sdk: 16 random bytes in hex
    }
    claims = {
        "sub": key_id,
        "iss": "cdp",
        "iat": issued,
        "nbf": issued,
        "exp": issued + JWT_LIFETIME_S,
        "uris": [f"{method.upper()} {parts.netloc}{parts.path}"],
    }
    signing_input = f"{_b64url(_json(header))}.{_b64url(_json(claims))}".encode()
    if isinstance(key, ed25519.Ed25519PrivateKey):
        signature = key.sign(signing_input)
    else:
        # JWS wants R || S, 32 bytes each (RFC 7518, 3.4), not cryptography's DER.
        r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input.decode()}.{_b64url(signature)}"


class CdpAuth:
    """The x402 SDK's ``AuthProvider`` for CDP: a fresh JWT per request, bound to its URI.

    The SDK asks for all of the headers before each request and uses the ones for that
    endpoint. Discovery is public on CDP, so the Bazaar lists get no Authorization.
    """

    def __init__(self, key_id: str, key: PrivateKey, base_url: str = CDP_FACILITATOR_URL) -> None:
        self._key_id = key_id
        self._key = key
        self._base = base_url.rstrip("/")

    def __repr__(self) -> str:
        return f"CdpAuth(base_url={self._base!r})"

    def bearer(self, method: str, endpoint: str) -> dict[str, str]:
        token = make_jwt(self._key_id, self._key, method, f"{self._base}/{endpoint}")
        return {"Authorization": f"Bearer {token}"}

    def get_auth_headers(self) -> AuthHeaders:
        return AuthHeaders(
            verify=self.bearer("POST", "verify"),
            settle=self.bearer("POST", "settle"),
            supported=self.bearer("GET", "supported"),
        )


class CdpFacilitatorClient(LenientSettleClient):
    """CDP's facilitator: the service's settle reading, CDP's auth, Base only.

    A 401 or 403 from any of its endpoints is a key CDP no longer accepts (revoked, deleted,
    or never right). It sets ``refused``, which ``Payments`` reads: CDP is not asked again by
    this process, a paid call whose verify CDP refused is verified and settled by the next
    facilitator (xpay), unlisted, and the next quote is built without CDP. A settle CDP refuses
    after verifying is an unpaid failure (settle never falls through). It is logged at ERROR
    naming the facilitator, never the key or the body. A 402 is CDP asking for a payment method
    on the account (its free settlements are used up): the same, with a line naming that fix.
    """

    refused = False

    def __init__(
        self, config: FacilitatorConfig, *, networks: frozenset[str] = frozenset({BASE_MAINNET})
    ) -> None:
        super().__init__(config)
        self.networks = networks

    def get_supported(self) -> SupportedResponse:
        # Only the networks CDP is put first for: listed first, it would otherwise also take
        # Solana from Dexter (free) and spend the free settlements on it.
        supported = super().get_supported()
        kinds = [kind for kind in supported.kinds if kind.network in self.networks]
        return supported.model_copy(update={"kinds": kinds})

    async def _verify_http(
        self, version: int, payload_dict: dict[str, Any], requirements_dict: dict[str, Any]
    ) -> VerifyResponse:
        """The SDK's verify, plus CDP's answer to an invalid payment: HTTP 400 with
        ``{"isValid": false, "invalidReason": ...}`` (cdp-sdk 1.53.0,
        ``x402_facilitator_api.verify_x402_payment``: '400' is ``X402VerifyPaymentRejection``).

        That is the payer's answer, read as one: the SDK raises on any non-200, which would
        make ``Payments`` ask the next facilitator and rest CDP for a payer's bad signature.
        But not when the reason says CDP could not read the chain (``CHAIN_UNREAD``), with a
        400 or a 200: that is raised, so the next facilitator verifies and CDP is rested.
        """
        response = await self._get_async_client().post(
            f"{self._url}/verify",
            headers=self._get_verify_headers(),
            json=self._build_request_body(version, payload_dict, requirements_dict),
        )
        if response.status_code == 400:
            rejection = _rejection(response)
            if rejection is not None:
                return rejection
        if response.status_code != 200:
            # The status only: Payments logs the exception's type, never this message.
            msg = f"Facilitator verify failed ({response.status_code})"
            raise ValueError(msg)
        verified = _parse_facilitator_response(response, VerifyResponse, "verify")
        if _chain_unread(verified):
            msg = "Facilitator verify failed (200: it could not read the chain)"
            raise ValueError(msg)
        return _attach_extension_responses(verified, response)

    def _get_sync_client(self) -> httpx.Client:
        client = super()._get_sync_client()
        client.event_hooks = {"response": [self._refused_sync]}
        return client

    def _get_async_client(self) -> httpx.AsyncClient:
        client = super()._get_async_client()
        hooks = client.event_hooks
        if self._refused not in hooks["response"]:
            client.event_hooks = {**hooks, "response": [*hooks["response"], self._refused]}
        return client

    def _note(self, response: httpx.Response) -> None:
        where = {
            "facilitator": self.url,
            "status": response.status_code,
            "endpoint": response.request.url.path.rsplit("/", 1)[-1],
        }
        if response.status_code in AUTH_REFUSED:
            self.refused = True
            log.error(
                "facilitator refused this service's API key: check %s (the key may be "
                "deleted, disabled or mistyped); Base is served, unlisted, by the next "
                "facilitator until the service restarts with a key CDP accepts",
                SETTINGS_NAMES,
                extra=where,
            )
        elif response.status_code == PAYMENT_METHOD_REQUIRED:
            self.refused = True
            log.error(
                "CDP asks for a payment method on the account this service's API key belongs "
                "to (its free settlements are used up): add one in the CDP portal "
                "(portal.cdp.coinbase.com), then run Deploy to Modal; until then Base is "
                "served, unlisted, by the next facilitator",
                extra=where,
            )

    def _refused_sync(self, response: httpx.Response) -> None:
        self._note(response)

    async def _refused(self, response: httpx.Response) -> None:
        self._note(response)


def _chain_unread(answer: VerifyResponse) -> bool:
    """Whether CDP says it could not read the chain, rather than that the payment is invalid."""
    return answer.is_valid is False and answer.invalid_reason in CHAIN_UNREAD


def _rejection(response: httpx.Response) -> VerifyResponse | None:
    """A 400 body that says, in the schema, that the payment is invalid; else None."""
    try:
        answer = VerifyResponse.model_validate(response.json())
    except (ValueError, TypeError, ValidationError):
        return None
    if _chain_unread(answer):
        return None
    return answer if answer.is_valid is False and answer.invalid_reason else None


def _values(settings: Settings) -> tuple[str, str]:
    key_id = settings.cdp_api_key_id.get_secret_value() if settings.cdp_api_key_id else ""
    secret = settings.cdp_api_key_secret.get_secret_value() if settings.cdp_api_key_secret else ""
    return key_id.strip(), secret.strip()


def credentials(settings: Settings) -> tuple[str, str] | None:
    """The configured key ID and secret, both non-empty, else None."""
    key_id, secret = _values(settings)
    return (key_id, secret) if key_id and secret else None


def half_configured(settings: Settings) -> bool:
    """One of the two set without the other: a mistake to name, not a choice."""
    return sum(1 for value in _values(settings) if value) == 1


def load_auth(settings: Settings) -> CdpAuth | None:
    """The configured key as a ``CdpAuth``; raises ``CdpKeyError`` for a key that is wrong."""
    pair = credentials(settings)
    if pair is None:
        return None
    return CdpAuth(load_key_id(pair[0]), load_key(pair[1]))


def cdp_client(settings: Settings) -> CdpFacilitatorClient | None:
    """CDP's client with the configured key, for Base on the configured network.

    ``payments.facilitator_urls`` decides whether CDP is in the list (first, on mainnet with a
    key; or wherever ``AIMART_FACILITATOR_URLS`` names it). A key that cannot be read, or none,
    leaves CDP out with one ERROR line naming the problem (never the key): the service starts
    and Base goes to the next facilitator. The deploy's selftest fails on the same key before
    it can cost a sale.
    """
    try:
        auth = load_auth(settings)
    except CdpKeyError as exc:
        log.error("CDP facilitator not used: %s", exc)
        return None
    if auth is None:
        log.error("CDP facilitator not used: it needs %s", SETTINGS_NAMES)
        return None
    return CdpFacilitatorClient(
        FacilitatorConfig(
            url=CDP_FACILITATOR_URL, timeout=settings.facilitator_timeout_s, auth_provider=auth
        ),
        networks=frozenset({base_network(settings)}),
    )


def base_network(settings: Settings) -> str:
    return BASE_MAINNET if settings.network_mode is NetworkMode.MAINNET else BASE_SEPOLIA


async def check_key(settings: Settings, *, timeout_s: float = 15.0) -> str:
    """The deploy's money-free check of the configured key: one authenticated GET /supported.

    Raises ``CdpKeyError`` when CDP refuses the key (401/403) or the key cannot be read, so the
    deploy fails before the key costs a sale. Any other outcome (CDP down, a timeout) is
    reported, not raised: the service skips a facilitator that does not answer, and a CDP
    outage must not block a release.
    """
    if half_configured(settings):
        msg = f"set both {SETTINGS_NAMES}, or neither"
        raise CdpKeyError(msg)
    auth = load_auth(settings)
    if auth is None:
        return "no CDP key configured: Base settles through xpay, then Primer"
    url = f"{CDP_FACILITATOR_URL}/supported"
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.get(url, headers=auth.bearer("GET", "supported"))
    except httpx.HTTPError as exc:
        return f"CDP not reached ({type(exc).__name__}): the key was not checked"
    if response.status_code in AUTH_REFUSED:
        msg = (
            f"CDP refused the API key (HTTP {response.status_code}): check {SETTINGS_NAMES} "
            "in the production environment's secrets"
        )
        raise CdpKeyError(msg)
    if response.status_code != 200:
        return f"CDP answered {response.status_code}: the key was not checked"
    network = base_network(settings)
    try:
        kinds = response.json().get("kinds", [])
        base = any(
            (k.get("x402Version"), k.get("scheme"), k.get("network")) == (2, "exact", network)
            for k in kinds
        )
    except (ValueError, AttributeError, TypeError):
        base = False
    listed = "lists" if base else "does NOT list"
    return f"CDP accepted the key; it {listed} x402 v2 'exact' on {network}"
