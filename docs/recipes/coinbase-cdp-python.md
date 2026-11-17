# Coinbase CDP server wallet, Python

Pay AImart Parquet from the CDP server wallet your agent already has. Script:
[`coinbase_cdp.py`](coinbase_cdp.py). Other wallets: [recipes](README.md).

## You need

- The three variables your CDP project already uses, named as Coinbase's docs name them:
  `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET` and `CDP_WALLET_SECRET`.
- A CDP **EVM account** (an EOA, not a smart account) holding a little USDC on Base: $0.025 runs
  the example. No ETH and no approval: the facilitator pays the gas.
- [uv](https://docs.astral.sh/uv/) (it installs Python 3.12+ and the pinned packages).

## Install

Nothing to install by hand. The script declares its dependencies (PEP 723), pinned exactly:

```
cdp-sdk==1.51.0
x402[evm,httpx]==2.25.0
```

In your own project: `pip install cdp-sdk==1.51.0 'x402[evm,httpx]==2.25.0'`.

## Run

```bash
export CDP_API_KEY_ID=... CDP_API_KEY_SECRET=... CDP_WALLET_SECRET=...
export AIMART_MAINNET_CONFIRM=I_UNDERSTAND_THIS_SPENDS_REAL_USDC
uv run https://raw.githubusercontent.com/balogvn/aimart/main/docs/recipes/coinbase_cdp.py --account 0xYourCdpAccount
```

`--account` takes the account's address or its name. `AIMART_MAINNET_CONFIRM` is there so
that nobody spends real USDC by accident. From a clone, run `uv run docs/recipes/coinbase_cdp.py`.

It prints:

```
payer: 0xYourCdpAccount on eip155:8453, at most $0.02 a call
convert: 6 rows; paid 0.02 USDC, tx 0x… https://basescan.org/tx/0x…
artifact: <artifact id> (until <time, one hour later>)
query: ['region', 'units'] [['East', 7], ['North', 15], ['South', 13], ['West', 10]]; paid 0.005 USDC, tx 0x… https://basescan.org/tx/0x…
```

It costs **$0.025**: convert $0.02 plus query $0.005.

## The code

The whole flow fits in one short program; [`coinbase_cdp.py`](coinbase_cdp.py) is the same with
the receipts printed and every refusal turned into one line. The wallet part is
`coinbase_signer`: it wraps the CDP account in `EvmLocalAccount`, which signs synchronously as
x402 needs (a bare `EvmServerAccount` signs with `async` methods and does not work with x402's
Python client). `convert_and_query` takes any signer.

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["cdp-sdk==1.51.0", "x402[evm,httpx]==2.25.0"]
# ///
import asyncio

import httpx
from cdp import CdpClient
from cdp.evm_local_account import EvmLocalAccount
from x402 import max_amount, x402Client
from x402.http.clients.httpx import x402AsyncTransport
from x402.mechanisms.evm.exact import ExactEvmClientScheme

AIMART_URL = "https://balogvn--aimart-parquet.us-east.modal.run"
SAMPLE_CSV = "https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.csv"


async def coinbase_signer(address: str) -> EvmLocalAccount:
    async with CdpClient() as cdp:  # reads CDP_API_KEY_ID, CDP_API_KEY_SECRET, CDP_WALLET_SECRET
        return EvmLocalAccount(await cdp.evm.get_account(address=address))


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


async def main() -> None:
    await convert_and_query(await coinbase_signer("0xYourCdpAccount"))


asyncio.run(main())
```

## Spend cap

`payer.register_policy(max_amount(<atomic USDC>))` refuses any quote above the cap before
anything is signed; the script sets it from `--max-usd` (default `0.02`). x402 also caps any
payment at $1 by default. CDP project policies can allow or deny the signing call, but this path
signs a hash, so CDP cannot see the amount: your cap is the one that counts.

## Check the payment

Each paid line ends with the transaction on basescan.org. The same receipt is in the response
header `PAYMENT-RESPONSE` (base64 JSON: `success`, `transaction`, `network`, `amount`).

## Troubleshooting

| the script stops with | it means |
|---|---|
| `this spends real USDC on Base: set AIMART_MAINNET_CONFIRM=...` | the safety switch is off; nothing was sent |
| `Coinbase: cannot use account ...` | the CDP variables or the account are wrong; nothing was paid |
| `/v1/convert: no payment made: ... filtered out by policies` | the quote is above `--max-usd`; nothing was signed |
| `/v1/convert: no payment made: ...` (anything else) | CDP refused to sign (credentials, or a policy denying the sign-hash call) |
| `payment rejected, not charged: insufficient_funds` | the account has too little USDC on Base. Other reasons come from the `PAYMENT-REQUIRED` header's `error` |
| `payment not settled, result withheld: <reason>` | the work ran but the settlement failed: not charged, unless the line names a transaction; then look it up on basescan.org before paying again |
| `HTTP 4xx, not charged: <message> <hint>` | the call itself failed (bad file, bad SQL); fix what the hint says |

## MCP

The same `payer` pays the MCP tools at `https://balogvn--aimart-parquet.us-east.modal.run/mcp`:
call a tool, sign the `PaymentRequired` in its `structuredContent` with
`payer.create_payment_payload(...)`, and call again with `_meta["x402/payment"]`. See
[agents-guide.md section 7](../agents-guide.md#7-runnable-example-rest-and-mcp-real-url).
