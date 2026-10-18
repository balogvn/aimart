#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["x402[evm,httpx]>=2.25,<3", "mcp>=2.3,<3", "httpx>=0.28", "cryptography>=48"]
# ///
"""Pay AImart Parquet per call with x402, over REST and over MCP. Testnet unless told otherwise.

    export AIMART_URL=https://YOUR-SERVICE.run.app      # placeholder: your deployment
    export PAYER_PRIVATE_KEY=0x...                        # a Base Sepolia TEST wallet with test USDC
    uv run docs/examples/agent_pays.py sales.csv

What it does, in the order an agent should do it:

1. REST: POST /v1/convert unpaid, decode the base64 PAYMENT-REQUIRED header and print the quote.
2. REST: the same call through an x402 transport, which signs the quote and retries; read the
   settlement from the PAYMENT-RESPONSE header.
3. MCP: tools/list (free), call query_parquet unpaid (isError + PaymentRequired in
   structuredContent), sign it, retry with params._meta["x402/payment"].
4. Provenance (free): verify the artifact's ML-DSA-65 signature offline with the published key,
   and check the downloaded file's SHA-256 against the signed manifest.

The private key stays here, on the paying side. The service never sees it: it only receives a
signed authorization for one exact amount to one payTo address. A spend cap (``--max-usd``)
refuses any quote above it, and mainnet needs an explicit confirmation sentence.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
import httpx
import mcp
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.mldsa import MLDSA65PublicKey
from eth_account import Account
from mcp.client.streamable_http import streamable_http_client
from x402 import max_amount, parse_payment_required, x402Client
from x402.http.clients.httpx import x402AsyncTransport
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact import ExactEvmClientScheme

if TYPE_CHECKING:
    from mcp_types import RequestParamsMeta

BASE_SEPOLIA = "eip155:84532"
BASE_MAINNET = "eip155:8453"
MAINNET_CONFIRM = "I_UNDERSTAND_THIS_SPENDS_REAL_USDC"


def decode_header(value: str) -> dict[str, Any]:
    """PAYMENT-REQUIRED and PAYMENT-RESPONSE are base64 of JSON."""
    decoded: dict[str, Any] = json.loads(base64.b64decode(value))
    return decoded


def make_payer(private_key: str, *, network: str, max_usd: Decimal) -> x402Client:
    """An x402 client that can sign on exactly one network and never above ``max_usd``."""
    payer = x402Client()
    payer.register(network, ExactEvmClientScheme(EthAccountSigner(Account.from_key(private_key))))
    payer.register_policy(max_amount(int(max_usd * 10**6)))  # USDC has 6 decimals
    return payer


async def rest_flow(
    base_url: str,
    payer: x402Client,
    data: bytes,
    filename: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    body = {"content_base64": base64.b64encode(data).decode(), "filename": filename}
    inner = transport or httpx.AsyncHTTPTransport(retries=2)

    # 1) Unpaid: the 402 carries the quote. The body is {}; everything is in the header.
    async with httpx.AsyncClient(base_url=base_url, transport=inner, timeout=60) as plain:
        r = await plain.post("/v1/convert", json=body)
    if r.status_code != 402:
        sys.exit(f"expected 402 from an unpaid call, got {r.status_code}: {r.text[:200]}")
    quote = decode_header(r.headers["PAYMENT-REQUIRED"])
    for option in quote["accepts"]:
        print(
            f"quote: {option['amount']} atomic units of {option['asset']} on {option['network']}"
            f" to {option['payTo']}"
        )

    # 2) Paid: the transport signs the quote and retries with PAYMENT-SIGNATURE.
    paying = x402AsyncTransport(payer, inner)
    async with httpx.AsyncClient(base_url=base_url, transport=paying, timeout=180) as paid:
        r = await paid.post("/v1/convert", json=body)
        if r.status_code == 402:  # rejected payment: the reason is in the header, not the body
            reason = decode_header(r.headers["PAYMENT-REQUIRED"]).get("error")
            sys.exit(f"payment rejected: {reason}")
        r.raise_for_status()
        settlement = decode_header(r.headers["PAYMENT-RESPONSE"])
        result: dict[str, Any] = r.json()
        report = result["report"]
        print(
            f"converted: {report['rows']} rows, {len(report['columns'])} columns;"
            f" paid {settlement['amount']} in tx {settlement['transaction']}"
        )
        print(
            f"tokens to read: raw ~{report['estimated_tokens_raw']},"
            f" schema report ~{report['estimated_tokens_schema']}"
        )

        # Pass the artifact_id onward instead of re-sending the file.
        r = await paid.post("/v1/inspect", json={"artifact_id": result["artifact"]["id"]})
        r.raise_for_status()
        print("inspect:", [(c["name"], c["type"]) for c in r.json()["columns"]])

        # Download (free, signed link) and verify provenance offline.
        download = await paid.get(result["artifact"]["download_url"])
        download.raise_for_status()
        key_doc = (await paid.get("/.well-known/aimart-signing-key")).json()
    verify_offline(result["artifact"]["provenance"], key_doc, download.content)
    return result


def verify_offline(provenance: dict[str, Any], key_doc: dict[str, Any], file_bytes: bytes) -> None:
    """Check origin and integrity without trusting the service at verification time."""
    manifest = provenance["manifest"]
    message = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    key = MLDSA65PublicKey.from_public_bytes(base64.b64decode(key_doc["public_key"]))
    try:
        key.verify(
            base64.b64decode(provenance["signature"]),
            message.encode(),
            key_doc["context"].encode(),
        )
    except InvalidSignature:
        sys.exit("provenance signature does NOT verify")
    if hashlib.sha256(file_bytes).hexdigest() != manifest["sha256"]:
        sys.exit("downloaded file does NOT match the signed sha256")
    if key_doc.get("ephemeral"):
        print("warning: the server uses an ephemeral signing key (signatures break on restart)")
    print(f"provenance: {provenance['algorithm']} signature valid, sha256 matches")


async def mcp_flow(
    mcp_url: str,
    payer: x402Client,
    artifact_id: str,
    *,
    transport: Any = None,
) -> dict[str, Any]:
    async with mcp.Client(transport or streamable_http_client(mcp_url)) as session:
        tools = await session.list_tools()  # free: initialize and tools/list never charge
        print("mcp tools:", [t.name for t in tools.tools])

        args = {"artifact_id": artifact_id, "sql": "SELECT count(*) AS n FROM data"}
        first = await session.call_tool("query_parquet", args)
        required = first.structured_content or {}
        if not (first.is_error and "accepts" in required):
            sys.exit(f"expected a PaymentRequired result, got: {required}")
        print("mcp quote:", [(a["amount"], a["network"]) for a in required["accepts"]])

        payload = await payer.create_payment_payload(parse_payment_required(required))
        # _meta is an open map at runtime; the cast only tells the type checker so.
        meta = cast(
            "RequestParamsMeta",
            {"x402/payment": payload.model_dump(by_alias=True, exclude_none=True)},
        )
        paid = await session.call_tool("query_parquet", args, meta=meta)
        if paid.is_error:
            sys.exit(f"paid call failed (not settled): {paid.structured_content}")
        settlement = (paid.meta or {}).get("x402/payment-response", {})
        print(f"mcp query: {paid.structured_content}; paid {settlement.get('amount')}")
        return paid.structured_content or {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("file", type=Path, help="A small CSV/JSON/Excel/SPSS/Stata/SAS file.")
    parser.add_argument("--url", default=os.environ.get("AIMART_URL"), help="Service origin.")
    parser.add_argument("--network", choices=["testnet", "mainnet"], default="testnet")
    parser.add_argument(
        "--max-usd", type=Decimal, default=Decimal("0.02"), help="Refuse quotes above this."
    )
    args = parser.parse_args()
    if not args.url:
        parser.error("set AIMART_URL or pass --url")
    key = os.environ.get("PAYER_PRIVATE_KEY")
    if not key:
        parser.error("set PAYER_PRIVATE_KEY to a test wallet's key (it never leaves this process)")
    network = BASE_SEPOLIA
    if args.network == "mainnet":
        if os.environ.get("AIMART_MAINNET_CONFIRM") != MAINNET_CONFIRM:
            parser.error(f"mainnet spends real USDC: set AIMART_MAINNET_CONFIRM={MAINNET_CONFIRM}")
        network = BASE_MAINNET

    base_url = args.url.rstrip("/")
    payer = make_payer(key, network=network, max_usd=args.max_usd)

    async def run() -> None:
        converted = await rest_flow(base_url, payer, args.file.read_bytes(), args.file.name)
        await mcp_flow(f"{base_url}/mcp", payer, converted["artifact"]["id"])

    anyio.run(run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
