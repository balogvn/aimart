# Coinbase CDP server wallet, TypeScript

Pay AImart Parquet from the CDP server wallet your agent already has. Code:
[`typescript/coinbase/coinbase-cdp.ts`](typescript/coinbase/coinbase-cdp.ts) plus the shared
payment part [`aimart.ts`](typescript/coinbase/aimart.ts). Other wallets: [recipes](README.md).

## You need

- The three variables your CDP project already uses, named as Coinbase's docs name them:
  `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET` and `CDP_WALLET_SECRET`.
- A CDP **EVM account** (an EOA, not a smart account) holding a little USDC on Base: $0.025 runs
  the example. No ETH and no approval: the facilitator pays the gas.
- Node 20 or later.

## Install

The folder's `package.json` pins every package exactly, and its `package-lock.json` pins every
package they pull in, as npm resolved them on 2026-11-03: nothing in the signing path is younger
than two weeks. `npm ci` installs exactly the lock:

```bash
git clone --depth 1 https://github.com/balogvn/aimart
cd aimart/docs/recipes/typescript/coinbase
npm ci
```

In your own project, with the same cutoff:
`npm install --save-exact --before=2026-11-03 @coinbase/cdp-sdk@1.60.0 @x402/core@2.29.0 @x402/evm@2.29.0 @x402/fetch@2.29.0 @x402/extensions@2.29.0 @x402/svm@2.29.0`.
`@coinbase/cdp-sdk/x402` loads `@x402/extensions` and `@x402/svm` as soon as it is imported, so
install them even though only Base is used.

## Run

```bash
export CDP_API_KEY_ID=... CDP_API_KEY_SECRET=... CDP_WALLET_SECRET=...
export AIMART_MAINNET_CONFIRM=I_UNDERSTAND_THIS_SPENDS_REAL_USDC
npx tsx coinbase-cdp.ts --account 0xYourCdpAccount
```

`--account` takes the account's address or its name; `--max-usd` sets the cap (default `0.02`).
`AIMART_MAINNET_CONFIRM` is there so that nobody spends real USDC by accident.

It prints:

```
payer: 0xYourCdpAccount
network eip155:8453, at most $0.02 a call
convert: 6 rows; paid 0.02 USDC, tx 0x… https://basescan.org/tx/0x…
artifact: <artifact id> (until <time, one hour later>)
query: ["region","units"] [["East",7],["North",15],["South",13],["West",10]]; paid 0.005 USDC, tx 0x… https://basescan.org/tx/0x…
```

It costs **$0.025**: convert $0.02 plus query $0.005.

## The code

The whole flow fits in one short program; the folder's files are the same with the receipts
printed and every refusal turned into one line. The wallet part is `fromCdpEvmAccount`, from
Coinbase's SDK: each payment is one call to CDP's sign-typed-data API, and the key never leaves
Coinbase. `convertAndQuery` takes any x402 signer.

```ts
import { CdpClient } from "@coinbase/cdp-sdk";
import { fromCdpEvmAccount } from "@coinbase/cdp-sdk/x402";
import { x402Client } from "@x402/core/client";
import type { ClientEvmSigner } from "@x402/evm";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { wrapFetchWithPayment } from "@x402/fetch";

const AIMART_URL = "https://balogvn--aimart-parquet.us-east.modal.run";
const SAMPLE_CSV = "https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.csv";

async function coinbaseSigner(address: `0x${string}`): Promise<ClientEvmSigner> {
  const cdp = new CdpClient(); // reads CDP_API_KEY_ID, CDP_API_KEY_SECRET, CDP_WALLET_SECRET
  return fromCdpEvmAccount(await cdp.evm.getAccount({ address }));
}

async function convertAndQuery(signer: ClientEvmSigner) {
  const payer = new x402Client()
    .register("eip155:8453", new ExactEvmScheme(signer))
    .registerPolicy((_version, options) => options.filter((o) => BigInt(o.amount) <= 20_000n)); // $0.02 cap
  const pay = wrapFetchWithPayment(fetch, payer);
  const post = async (path: string, body: unknown) => {
    const response = await pay(`${AIMART_URL}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
    return response.json();
  };
  const converted = await post("/v1/convert", { url: SAMPLE_CSV });
  const sql = "SELECT region, sum(units) AS units FROM data GROUP BY region ORDER BY region";
  const queried = await post("/v1/query", { artifact_id: converted.artifact.id, sql });
  console.log(queried.rows);
}

await convertAndQuery(await coinbaseSigner("0xYourCdpAccount"));
```

## Spend cap

The `registerPolicy` filter refuses any quote above the cap before anything is signed; the
recipe sets it from `--max-usd`. `@x402/core` also caps any payment at $1 by default. For a
daily total or a payee allowlist as well, see the
[`CdpX402Client` recipe](coinbase-x402client-typescript.md), whose spend controls do both.

## Check the payment

Each paid line ends with the transaction on basescan.org. The same receipt is in the response
header `PAYMENT-RESPONSE` (base64 JSON: `success`, `transaction`, `network`, `amount`); `@x402/fetch`
decodes it with `decodePaymentResponseHeader`.

## Troubleshooting

| the script stops with | it means |
|---|---|
| `this spends real USDC on Base: set AIMART_MAINNET_CONFIRM=...` | the safety switch is off; nothing was sent |
| `not paid: ...` before any call | the CDP variables or the account are wrong; nothing was paid |
| `/v1/convert: no payment made: ... filtered out by policies` | the quote is above `--max-usd`; nothing was signed |
| `/v1/convert: no payment made: ...` (anything else) | CDP refused to sign (credentials, or a CDP policy) |
| `/v1/convert: no response, nothing paid: ...` | the service did not answer before anything was signed: a wrong `--url`, no network, or a cold start that timed out. Nothing was paid; run it again |
| `/v1/convert: outcome unknown, a signed payment of 0.02 USDC was sent and may have been settled (...): look up https://basescan.org/address/0x… before paying again` | the paid request went out but its answer was lost (the connection dropped, or no answer came). The service may have settled it: open the link and look for that USDC transfer before you run it again. An authorization that was never settled expires after 300 seconds |
| `payment rejected, not charged: insufficient_funds` | the account has too little USDC on Base. Other reasons come from the `PAYMENT-REQUIRED` header's `error` |
| `payment not settled, result withheld: <reason>` | the work ran but the settlement failed: not charged, unless the line names a transaction; then look it up on basescan.org before paying again |
| `HTTP 4xx, not charged: <message> <hint>` | the call itself failed (bad file, bad SQL); fix what the hint says |

`@x402/fetch` does not throw on a refused payment: it returns the final 402, and `aimart.ts`
reads its headers. Use the `@x402/*` packages (v2), not the older `x402-fetch` (v1), which this
service refuses.

## MCP

`@x402/mcp@2.29.0` pays the MCP tools at `https://balogvn--aimart-parquet.us-east.modal.run/mcp`
with the same payer: `wrapMCPClientWithPayment(client, payer, { autoPayment: true })`, then
`callTool("query_parquet", { artifact_id, sql })`.
