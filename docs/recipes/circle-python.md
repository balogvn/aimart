# Circle developer-controlled wallet, Python

Pay AImart Parquet from the Circle developer-controlled wallet your agent already has. Script:
[`circle_dcw.py`](circle_dcw.py). Other wallets: [recipes](README.md).

## You need

- The two variables your Circle setup already uses, named as Circle's docs name them:
  `CIRCLE_API_KEY` and `CIRCLE_ENTITY_SECRET` (the entity secret you registered).
- A developer-controlled **EOA** wallet on `BASE` (not an SCA) holding a little USDC on Base:
  $0.025 runs the example. No ETH and no approval: the facilitator pays the gas.
- [uv](https://docs.astral.sh/uv/) (it installs Python 3.12+ and the pinned packages).

## Install

Nothing to install by hand. The script declares its dependencies (PEP 723), pinned exactly:

```
circle-developer-controlled-wallets==9.6.0
x402[evm,httpx]==2.25.0
```

In your own project: `pip install circle-developer-controlled-wallets==9.6.0 'x402[evm,httpx]==2.25.0'`.

## Run

```bash
export CIRCLE_API_KEY=... CIRCLE_ENTITY_SECRET=...
export AIMART_MAINNET_CONFIRM=I_UNDERSTAND_THIS_SPENDS_REAL_USDC
uv run https://raw.githubusercontent.com/balogvn/aimart/main/docs/recipes/circle_dcw.py --wallet 0xYourCircleWalletOnBase
```

`AIMART_MAINNET_CONFIRM` is there so that nobody spends real USDC by accident. From a clone, run
`uv run docs/recipes/circle_dcw.py`.

It prints:

```
payer: 0xYourCircleWalletOnBase on eip155:8453, at most $0.02 a call
convert: 6 rows; paid 0.02 USDC, tx 0x… https://basescan.org/tx/0x…
artifact: <artifact id> (until <time, one hour later>)
query: ['region', 'units'] [['East', 7], ['North', 15], ['South', 13], ['West', 10]]; paid 0.005 USDC, tx 0x… https://basescan.org/tx/0x…
```

It costs **$0.025**: convert $0.02 plus query $0.005.

## The code

The whole flow fits in one short program; [`circle_dcw.py`](circle_dcw.py) is the same with the
receipts printed and every refusal turned into one line. The wallet part is `CircleSigner`: x402
asks for an EIP-712 signature, and Circle's sign-typed-data API takes one JSON document, so the
adapter adds the `EIP712Domain` type and writes the nonce as hex and every number as a decimal
string, as Circle's own x402 quickstart does. `convert_and_query` takes any signer.

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["circle-developer-controlled-wallets==9.6.0", "x402[evm,httpx]==2.25.0"]
# ///
import asyncio
import json
import os

import httpx
from circle.web3 import developer_controlled_wallets as dcw
from circle.web3 import utils
from x402 import max_amount, x402Client
from x402.http.clients.httpx import x402AsyncTransport
from x402.mechanisms.evm.exact import ExactEvmClientScheme

AIMART_URL = "https://balogvn--aimart-parquet.us-east.modal.run"
SAMPLE_CSV = "https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.csv"
EIP712_DOMAIN = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]


class CircleSigner:  # x402's ClientEvmSigner, signing through Circle's API
    def __init__(self, address: str) -> None:
        self.address = address
        client = utils.init_developer_controlled_wallets_client(
            api_key=os.environ["CIRCLE_API_KEY"], entity_secret=os.environ["CIRCLE_ENTITY_SECRET"]
        )
        self._signing = dcw.SigningApi(client)

    def sign_typed_data(self, domain, types, primary_type, message) -> bytes:
        def value(v):
            if isinstance(v, bytes):
                return "0x" + v.hex()  # the bytes32 nonce
            return str(v) if isinstance(v, int) else v  # uint256 as a decimal string

        document = {
            "types": {
                "EIP712Domain": EIP712_DOMAIN,
                **{
                    name: [{"name": f.name, "type": f.type} for f in fs]
                    for name, fs in types.items()
                },
            },
            "primaryType": primary_type,
            "domain": {
                "name": domain.name,
                "version": domain.version,
                "chainId": str(domain.chain_id),
                "verifyingContract": domain.verifying_contract,
            },
            "message": {k: value(v) for k, v in message.items()},
        }
        request = dcw.SignTypedDataRequest(
            wallet_address=self.address, blockchain="BASE", data=json.dumps(document)
        )
        signature = self._signing.sign_typed_data(request).data.signature
        return bytes.fromhex(signature.removeprefix("0x"))


async def convert_and_query(signer) -> None:
    payer = x402Client()
    payer.register("eip155:8453", ExactEvmClientScheme(signer))
    payer.register_policy(max_amount(20_000))  # USDC has 6 decimals: never more than $0.02 a call
    paying = x402AsyncTransport(payer)
    async with httpx.AsyncClient(base_url=AIMART_URL, transport=paying, timeout=180) as http:
        converted = await http.post("/v1/convert", json={"url": SAMPLE_CSV})
        converted.raise_for_status()
        artifact_id = converted.json()["artifact"]["id"]
        sql = "SELECT region, sum(units) AS units FROM data GROUP BY region ORDER BY region"
        queried = await http.post("/v1/query", json={"artifact_id": artifact_id, "sql": sql})
        queried.raise_for_status()
        print(queried.json()["rows"])


asyncio.run(convert_and_query(CircleSigner("0xYourCircleWalletOnBase")))
```

Each payment is one call to Circle's signing API. The Circle SDK makes it synchronously inside
x402's signing step: about one Circle round trip per paid call.

## Spend cap

`payer.register_policy(max_amount(<atomic USDC>))` refuses any quote above the cap before Circle
is asked to sign; the script sets it from `--max-usd` (default `0.02`). x402 also caps any
payment at $1 by default. Circle has no per-signature limit for typed data, so your cap is the
one that counts.

## Check the payment

Each paid line ends with the transaction on basescan.org. The same receipt is in the response
header `PAYMENT-RESPONSE` (base64 JSON: `success`, `transaction`, `network`, `amount`).

## Troubleshooting

| the script stops with | it means |
|---|---|
| `this spends real USDC on Base: set AIMART_MAINNET_CONFIRM=...` | the safety switch is off; nothing was sent |
| `set CIRCLE_API_KEY and CIRCLE_ENTITY_SECRET ...` | a variable is missing; nothing was sent |
| `Circle: cannot use wallet ...` | the Circle client could not start (for example a malformed entity secret); nothing was paid |
| `/v1/convert: no payment made: ... filtered out by policies` | the quote is above `--max-usd`; Circle was not asked to sign |
| `/v1/convert: no payment made: ...` (anything else) | Circle refused to sign: a wrong key or entity secret, a wallet not found on `BASE`, or an SCA wallet that is not deployed yet |
| `payment rejected, not charged: insufficient_funds` | the wallet has too little USDC on Base. Other reasons come from the `PAYMENT-REQUIRED` header's `error`; an SCA wallet may be refused here too |
| `payment not settled, result withheld: <reason>` | the work ran but the settlement failed: not charged, unless the line names a transaction; then look it up on basescan.org before paying again |
| `HTTP 4xx, not charged: <message> <hint>` | the call itself failed (bad file, bad SQL); fix what the hint says |

A signed authorization that was never settled expires after 300 seconds.

## MCP

The same `payer` pays the MCP tools at `https://balogvn--aimart-parquet.us-east.modal.run/mcp`:
call a tool, sign the `PaymentRequired` in its `structuredContent` with
`payer.create_payment_payload(...)`, and call again with `_meta["x402/payment"]`. See
[agents-guide.md section 7](../agents-guide.md#7-runnable-example-rest-and-mcp-real-url).
