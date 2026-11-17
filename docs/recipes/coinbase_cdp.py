#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["cdp-sdk==1.51.0", "x402[evm,httpx]==2.25.0"]
# ///
"""Pay AImart Parquet from a Coinbase CDP server wallet: convert a CSV, then query it.

    export CDP_API_KEY_ID=... CDP_API_KEY_SECRET=... CDP_WALLET_SECRET=...
    export AIMART_MAINNET_CONFIRM=I_UNDERSTAND_THIS_SPENDS_REAL_USDC
    uv run coinbase_cdp.py --account 0xYourCdpAccount

It spends $0.025 of USDC on Base: $0.02 to convert, $0.005 to query. The account needs USDC
on Base and nothing else: no ETH, no approval. Recipe: docs/recipes/coinbase-cdp-python.md.

Two parts. ``coinbase_signer`` is the only Coinbase code: it fetches your CDP account and
returns it as a signer. ``make_payer`` and ``convert_and_query`` take any x402 signer.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from decimal import Decimal
from typing import Any

import httpx
from x402 import max_amount, x402Client
from x402.http.clients.httpx import PaymentError, x402AsyncTransport
from x402.mechanisms.evm.exact import ExactEvmClientScheme

AIMART_URL = "https://balogvn--aimart-parquet.us-east.modal.run"
SAMPLE_CSV = "https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.csv"
SQL = "SELECT region, sum(units) AS units FROM data GROUP BY region ORDER BY region"
NETWORKS = {"base": "eip155:8453", "base-sepolia": "eip155:84532"}
EXPLORERS = {"eip155:8453": "https://basescan.org", "eip155:84532": "https://sepolia.basescan.org"}
MAINNET_CONFIRM = "I_UNDERSTAND_THIS_SPENDS_REAL_USDC"
USDC = Decimal(10**6)  # USDC has 6 decimals: 20000 atomic units are $0.02


# ------------------------------------------------------------------------------ Coinbase part
async def coinbase_signer(account: str) -> Any:
    """Your CDP EVM account (an address or a name) as an x402 signer.

    ``EvmLocalAccount`` signs synchronously, which is what x402's scheme needs: a bare
    ``EvmServerAccount`` signs with ``async`` methods and cannot be passed to it. Each payment
    is one call to CDP's signing API; the key never leaves Coinbase.
    """
    from cdp import CdpClient
    from cdp.evm_local_account import EvmLocalAccount

    # CdpClient reads CDP_API_KEY_ID, CDP_API_KEY_SECRET and CDP_WALLET_SECRET from the
    # environment. EvmLocalAccount keeps its own HTTP client, so this one may close.
    async with CdpClient() as cdp:
        if account.startswith("0x"):
            server_account = await cdp.evm.get_account(address=account)
        else:
            server_account = await cdp.evm.get_account(name=account)
    return EvmLocalAccount(server_account)


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
    parser.add_argument(
        "--account", required=True, help="Your CDP EVM account: 0x address or name."
    )
    parser.add_argument("--max-usd", type=Decimal, default=Decimal("0.02"), help="Cap per call.")
    parser.add_argument("--url", default=os.environ.get("AIMART_URL", AIMART_URL))
    parser.add_argument("--network", choices=sorted(NETWORKS), default="base")
    args = parser.parse_args()
    network = NETWORKS[args.network]
    if network == NETWORKS["base"] and os.environ.get("AIMART_MAINNET_CONFIRM") != MAINNET_CONFIRM:
        parser.error(f"this spends real USDC on Base: set AIMART_MAINNET_CONFIRM={MAINNET_CONFIRM}")

    async def run() -> None:
        try:
            signer = await coinbase_signer(args.account)
        except Exception as error:  # one line, not a traceback: nothing was paid
            raise NotPaid(f"Coinbase: cannot use account {args.account}: {error}") from error
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
