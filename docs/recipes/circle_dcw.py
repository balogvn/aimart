#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["circle-developer-controlled-wallets==9.6.0", "x402[evm,httpx]==2.25.0"]
# ///
"""Pay AImart Parquet from a Circle developer-controlled wallet: convert a CSV, then query it.

    export CIRCLE_API_KEY=... CIRCLE_ENTITY_SECRET=...
    export AIMART_MAINNET_CONFIRM=I_UNDERSTAND_THIS_SPENDS_REAL_USDC
    uv run circle_dcw.py --wallet 0xYourCircleWalletOnBase

It spends $0.025 of USDC on Base: $0.02 to convert, $0.005 to query. The wallet must be an EOA
on BASE holding USDC, and nothing else: no ETH, no approval. Recipe: docs/recipes/circle-python.md.

Two parts. ``circle_signer`` and ``CircleSigner`` are the only Circle code: each signature is
one call to Circle's sign-typed-data API. ``make_payer`` and ``convert_and_query`` take any
x402 signer.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
from x402 import max_amount, x402Client
from x402.http.clients.httpx import PaymentError, x402AsyncTransport
from x402.mechanisms.evm.exact import ExactEvmClientScheme

if TYPE_CHECKING:
    from collections.abc import Callable

AIMART_URL = "https://balogvn--aimart-parquet.us-east.modal.run"
SAMPLE_CSV = "https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.csv"
SQL = "SELECT region, sum(units) AS units FROM data GROUP BY region ORDER BY region"
NETWORKS = {"base": "eip155:8453", "base-sepolia": "eip155:84532"}
EXPLORERS = {"eip155:8453": "https://basescan.org", "eip155:84532": "https://sepolia.basescan.org"}
MAINNET_CONFIRM = "I_UNDERSTAND_THIS_SPENDS_REAL_USDC"
USDC = Decimal(10**6)  # USDC has 6 decimals: 20000 atomic units are $0.02


# ------------------------------------------------------------------------------ Circle part
CIRCLE_CHAINS = {"eip155:8453": "BASE", "eip155:84532": "BASE-SEPOLIA"}
EIP712_DOMAIN = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]


def circle_signer(wallet: str, network: str) -> CircleSigner:
    """Your Circle developer-controlled wallet (its 0x address) as an x402 signer."""
    from circle.web3 import developer_controlled_wallets as dcw
    from circle.web3 import utils

    client = utils.init_developer_controlled_wallets_client(
        api_key=os.environ["CIRCLE_API_KEY"], entity_secret=os.environ["CIRCLE_ENTITY_SECRET"]
    )
    signing = dcw.SigningApi(client)

    def sign(data: str) -> str:
        # The SDK fills in a fresh entity-secret ciphertext for every request.
        request = dcw.SignTypedDataRequest(
            wallet_address=wallet,
            blockchain=CIRCLE_CHAINS[network],
            data=data,
            memo="AImart Parquet x402 payment",
        )
        signature: str = signing.sign_typed_data(request).data.signature
        return signature

    return CircleSigner(wallet, sign)


class CircleSigner:
    """x402's ClientEvmSigner over any function that signs EIP-712 JSON as Circle's API does.

    x402 hands over the domain as an object, the types without EIP712Domain, the nonce as
    bytes and the amounts as ints; Circle takes one JSON document, so the nonce becomes 0x hex
    and every number a decimal string, as Circle's own x402 quickstart sends them.
    """

    def __init__(self, address: str, sign_json: Callable[[str], str]) -> None:
        self._address = address
        self._sign_json = sign_json

    @property
    def address(self) -> str:
        return self._address

    def sign_typed_data(
        self, domain: Any, types: dict[str, Any], primary_type: str, message: dict[str, Any]
    ) -> bytes:
        document = {
            "types": {
                "EIP712Domain": EIP712_DOMAIN,
                **{
                    name: [{"name": f.name, "type": f.type} for f in fields]
                    for name, fields in types.items()
                },
            },
            "primaryType": primary_type,
            "domain": {
                "name": domain.name,
                "version": domain.version,
                "chainId": str(domain.chain_id),
                "verifyingContract": domain.verifying_contract,
            },
            "message": {key: _json_value(value) for key, value in message.items()},
        }
        signature = self._sign_json(json.dumps(document))
        return bytes.fromhex(signature.removeprefix("0x"))


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes | bytearray):
        return "0x" + bytes(value).hex()  # the EIP-3009 nonce, a bytes32
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)  # a uint256, beyond what a JSON number holds exactly
    return value


# ------------------------------------------------------------------------------ payment part
class NotPaid(Exception):
    """A call that did not deliver. Its message is one line that says whether money moved."""


def make_payer(signer: Any, *, network: str, max_usd: Decimal) -> x402Client:
    """An x402 client that signs only on ``network`` and never above ``max_usd`` per call."""
    payer = x402Client()
    payer.register(network, ExactEvmClientScheme(signer))
    payer.register_policy(max_amount(int(max_usd * USDC)))  # a dearer quote is never signed
    return payer


def read_header(response: httpx.Response, name: str) -> dict[str, Any]:
    """A base64 JSON payment header as a dict; {} when it is absent or unreadable."""
    try:
        value = json.loads(base64.b64decode(response.headers.get(name) or ""))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def failure(response: httpx.Response) -> str:
    """Why a paid call did not deliver. A 402's body is {}: the reason is in a header."""
    if response.status_code != 402:  # {"error": {"code", "message", "hint"}}
        try:
            error = response.json()["error"]
        except (ValueError, KeyError, TypeError):
            error = None
        if not isinstance(error, dict):
            error = {"message": response.text[:200]}
        said = " ".join(f"{error.get('message', '')} {error.get('hint', '')}".split())
        return f"HTTP {response.status_code}, not charged: {said}"
    rejected = read_header(response, "PAYMENT-REQUIRED")
    receipt = read_header(response, "PAYMENT-RESPONSE")
    if not receipt:  # refused before the work: nothing was settled
        return f"payment rejected, not charged: {rejected.get('error') or 'no reason given'}"
    why = receipt.get("errorReason") or "no reason given"
    transaction = receipt.get("transaction")
    if transaction:
        return (
            f"payment not settled, result withheld: {why}; the facilitator named transaction"
            f" {transaction}: look it up before paying again, the transfer may have been made"
        )
    return f"payment not settled, result withheld: {why}"


async def paid_post(http: httpx.AsyncClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST through the paying transport; the JSON result, with the receipt under ``_receipt``."""
    try:
        response = await http.post(path, json=body)
    except PaymentError as error:  # over the cap, or the wallet could not sign
        raise NotPaid(f"{path}: no payment made: {error}") from error
    if response.status_code != 200:
        raise NotPaid(f"{path}: {failure(response)}")
    result: dict[str, Any] = response.json()
    result["_receipt"] = read_header(response, "PAYMENT-RESPONSE")
    return result


def paid_line(receipt: dict[str, Any]) -> str:
    amount = Decimal(str(receipt.get("amount") or 0)) / USDC
    transaction = receipt.get("transaction") or "not named"
    explorer = EXPLORERS.get(str(receipt.get("network")), "")
    link = f" {explorer}/tx/{transaction}" if explorer and transaction.startswith("0x") else ""
    return f"paid {amount} USDC, tx {transaction}{link}"


async def convert_and_query(
    payer: x402Client,
    *,
    base_url: str = AIMART_URL,
    source: dict[str, Any] | None = None,
    sql: str = SQL,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Convert ``source`` (default: the sample CSV's URL) and query it; two paid calls.

    Raises NotPaid with one line when a call does not deliver; a call that fails is not
    charged. The artifact is printed as soon as the conversion is paid for.
    """
    paying = x402AsyncTransport(payer, transport or httpx.AsyncHTTPTransport(retries=2))
    async with httpx.AsyncClient(base_url=base_url, transport=paying, timeout=180) as http:
        converted = await paid_post(http, "/v1/convert", source or {"url": SAMPLE_CSV})
        artifact = converted["artifact"]
        print(f"convert: {converted['report']['rows']} rows; {paid_line(converted['_receipt'])}")
        print(f"artifact: {artifact['id']} (until {artifact['expires_at']})")
        queried = await paid_post(http, "/v1/query", {"artifact_id": artifact["id"], "sql": sql})
        print(f"query: {queried['columns']} {queried['rows']}; {paid_line(queried['_receipt'])}")
    return {"artifact_id": artifact["id"], "convert": converted, "query": queried}


# ------------------------------------------------------------------------------ command line
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--wallet", required=True, help="Your Circle EOA wallet's 0x address.")
    parser.add_argument("--max-usd", type=Decimal, default=Decimal("0.02"), help="Cap per call.")
    parser.add_argument("--url", default=os.environ.get("AIMART_URL", AIMART_URL))
    parser.add_argument("--network", choices=sorted(NETWORKS), default="base")
    args = parser.parse_args()
    network = NETWORKS[args.network]
    if network == NETWORKS["base"] and os.environ.get("AIMART_MAINNET_CONFIRM") != MAINNET_CONFIRM:
        parser.error(f"this spends real USDC on Base: set AIMART_MAINNET_CONFIRM={MAINNET_CONFIRM}")
    missing = [
        name for name in ("CIRCLE_API_KEY", "CIRCLE_ENTITY_SECRET") if not os.environ.get(name)
    ]
    if missing:
        parser.error(f"set {' and '.join(missing)} (from your Circle developer console)")

    async def run() -> None:
        try:  # Circle is first called to sign
            signer = circle_signer(args.wallet, network)
        except Exception as error:  # one line, not a traceback: nothing was paid
            raise NotPaid(f"Circle: cannot use wallet {args.wallet}: {error}") from error
        print(f"payer: {signer.address} on {network}, at most ${args.max_usd} a call")
        payer = make_payer(signer, network=network, max_usd=args.max_usd)
        await convert_and_query(payer, base_url=args.url.rstrip("/"))

    try:
        asyncio.run(run())
    except NotPaid as stopped:
        sys.exit(str(stopped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
