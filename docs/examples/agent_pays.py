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
   settlement from the PAYMENT-RESPONSE header. A 402 after paying has its reason in
   PAYMENT-REQUIRED (rejected before the work) or PAYMENT-RESPONSE (settlement failed after it).
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


def read_header(response: httpx.Response, name: str) -> dict[str, Any]:
    """A payment header as a dict: {} when it is absent or is not base64 of a JSON object.
    Never index these headers: which one a 402 carries depends on where the payment stopped."""
    try:
        decoded = decode_header(response.headers.get(name) or "")
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def explain(reason: object, receipt: dict[str, Any]) -> str:
    """One line for a paid attempt that was refused.

    Without a receipt the payment was rejected before the work (verification): nothing was
    settled. With one (``success`` false), the work ran, the settlement did not go through as
    far as the service could tell, and the result was withheld. The facilitator may have
    moved the money all the same (a timeout, an answer the service could not read), so look
    before paying again: at the transaction the receipt names, when it names one.
    """
    why = " ".join(str(reason or "no reason given").split())[:300]
    if not receipt:
        return f"payment rejected before any work was done: {why}"
    line = f"payment not settled, so the result was withheld: {why}"
    transaction = receipt.get("transaction")
    if transaction:
        return (
            f"{line}; the facilitator named transaction {transaction}:"
            " look it up on chain before paying again, the transfer may have been made"
        )
    return f"{line}; check your wallet's transfers to payTo before paying again"


def refusal(response: httpx.Response) -> str:
    """Why a paid request came back 402. The body is {}: the reason is in PAYMENT-REQUIRED
    (``error``) when the payment was rejected, and in PAYMENT-RESPONSE (``errorReason``) when
    the settlement failed after the work. A 402 carries one of them, not both."""
    required = read_header(response, "PAYMENT-REQUIRED")
    receipt = read_header(response, "PAYMENT-RESPONSE")
    return explain(required.get("error") or receipt.get("errorReason"), receipt)


def paid_or_exit(response: httpx.Response, what: str) -> None:
    """Stop with one line unless the paid call succeeded. Only a 200 was charged for."""
    if response.status_code == 402:
        sys.exit(f"{what}: {refusal(response)}")
    if response.status_code != 200:
        text = " ".join(response.text.split())[:200]
        sys.exit(f"{what}: failed with HTTP {response.status_code}, not charged: {text}")


def settled_amount(settlement: dict[str, Any], quoted: str) -> str:
    """What a receipt says was paid. x402 v2 lets a facilitator leave ``amount`` out of its
    settlement, and a service may pass that on untouched; 'exact' settles the quoted amount."""
    return str(settlement.get("amount") or quoted)


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
        paid_or_exit(r, "convert")  # a 402 here has its reason in a header, not the body
        settlement = read_header(r, "PAYMENT-RESPONSE")
        result: dict[str, Any] = r.json()
        report = result["report"]
        print(
            f"converted: {report['rows']} rows, {len(report['columns'])} columns;"
            f" paid {settled_amount(settlement, quote['accepts'][0]['amount'])}"
            f" in tx {settlement.get('transaction') or 'not named'}"
        )
        # Said now, before anything else can fail: this is what the payment bought.
        print(f"artifact: {result['artifact']['id']} at {result['artifact']['download_url']}")
        print(
            f"tokens to read: raw ~{report['estimated_tokens_raw']},"
            f" schema report ~{report['estimated_tokens_schema']}"
        )

        # Pass the artifact_id onward instead of re-sending the file.
        r = await paid.post("/v1/inspect", json={"artifact_id": result["artifact"]["id"]})
        paid_or_exit(r, "inspect")
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
    # sys.exit inside this block would surface as a BaseExceptionGroup from the client's task
    # group, not as its one line: a failure is carried out of the block and reported after it.
    failure: str | None = None
    result: dict[str, Any] = {}
    async with mcp.Client(transport or streamable_http_client(mcp_url)) as session:
        tools = await session.list_tools()  # free: initialize and tools/list never charge
        print("mcp tools:", [t.name for t in tools.tools])

        args = {"artifact_id": artifact_id, "sql": "SELECT count(*) AS n FROM data"}
        first = await session.call_tool("query_parquet", args)
        required = first.structured_content or {}
        if not (first.is_error and "accepts" in required):
            failure = f"expected a PaymentRequired result, got: {str(required)[:300]}"
        else:
            print("mcp quote:", [(a["amount"], a["network"]) for a in required["accepts"]])
            payload = await payer.create_payment_payload(parse_payment_required(required))
            # _meta is an open map at runtime; the cast only tells the type checker so.
            meta = cast(
                "RequestParamsMeta",
                {"x402/payment": payload.model_dump(by_alias=True, exclude_none=True)},
            )
            paid = await session.call_tool("query_parquet", args, meta=meta)
            result = paid.structured_content or {}
            if paid.is_error:
                # A failed settlement's receipt is in structuredContent (a successful one's
                # is in _meta); a payment rejected before the work has none.
                receipt = result.get("x402/payment-response")
                failure = explain(result.get("error"), receipt if isinstance(receipt, dict) else {})
            else:
                settlement = (paid.meta or {}).get("x402/payment-response", {})
                quoted = required["accepts"][0]["amount"]
                print(f"mcp query: {result}; paid {settled_amount(settlement, quoted)}")
    if failure:
        sys.exit(f"mcp query_parquet: {failure}")
    return result


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
