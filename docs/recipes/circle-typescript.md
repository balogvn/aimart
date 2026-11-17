# Circle developer-controlled wallet, TypeScript

Pay AImart Parquet from the Circle developer-controlled wallet your agent already has. Code:
[`typescript/circle/circle.ts`](typescript/circle/circle.ts) plus the shared payment part
[`aimart.ts`](typescript/circle/aimart.ts). Other wallets: [recipes](README.md).

## You need

- The two variables your Circle setup already uses, named as Circle's docs name them:
  `CIRCLE_API_KEY` and `CIRCLE_ENTITY_SECRET` (the entity secret you registered).
- A developer-controlled **EOA** wallet on `BASE` (not an SCA) holding a little USDC on Base:
  $0.025 runs the example. No ETH and no approval: the facilitator pays the gas.
- Node 20 or later.

## Install

The folder's `package.json` pins every package exactly:

```bash
git clone --depth 1 https://github.com/balogvn/aimart
cd aimart/docs/recipes/typescript/circle
npm install
```

In your own project:
`npm install --save-exact @circle-fin/developer-controlled-wallets@10.8.1 @x402/core@2.29.0 @x402/evm@2.29.0 @x402/fetch@2.29.0`.

## Run

```bash
export CIRCLE_API_KEY=... CIRCLE_ENTITY_SECRET=...
export AIMART_MAINNET_CONFIRM=I_UNDERSTAND_THIS_SPENDS_REAL_USDC
npx tsx circle.ts --wallet 0xYourCircleWalletOnBase
```

`--max-usd` sets the cap (default `0.02`). `AIMART_MAINNET_CONFIRM` is there so that nobody
spends real USDC by accident.

It prints:

```
payer: 0xYourCircleWalletOnBase
network eip155:8453, at most $0.02 a call
convert: 6 rows; paid 0.02 USDC, tx 0x… https://basescan.org/tx/0x…
artifact: <artifact id> (until <time, one hour later>)
query: ["region","units"] [["East",7],["North",15],["South",13],["West",10]]; paid 0.005 USDC, tx 0x… https://basescan.org/tx/0x…
```

It costs **$0.025**: convert $0.02 plus query $0.005.

## The code

The whole flow fits in one short program; the folder's files are the same with the receipts
printed and every refusal turned into one line. The wallet part is `circleSigner`: x402 asks for
an EIP-712 signature, and Circle's sign-typed-data API takes one JSON document, so the adapter
adds the `EIP712Domain` type and writes bigints as decimal strings, as Circle's own x402
quickstart does. `convertAndQuery` takes any x402 signer.

```ts
import { initiateDeveloperControlledWalletsClient } from "@circle-fin/developer-controlled-wallets";
import { x402Client } from "@x402/core/client";
import type { ClientEvmSigner } from "@x402/evm";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { wrapFetchWithPayment } from "@x402/fetch";

const AIMART_URL = "https://balogvn--aimart-parquet.us-east.modal.run";
const SAMPLE_CSV = "https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.csv";
const EIP712Domain = [
  { name: "name", type: "string" },
  { name: "version", type: "string" },
  { name: "chainId", type: "uint256" },
  { name: "verifyingContract", type: "address" },
];

function circleSigner(walletAddress: `0x${string}`): ClientEvmSigner {
  const circle = initiateDeveloperControlledWalletsClient({
    apiKey: process.env.CIRCLE_API_KEY!,
    entitySecret: process.env.CIRCLE_ENTITY_SECRET!,
  });
  return {
    address: walletAddress,
    async signTypedData({ domain, types, primaryType, message }) {
      const data = JSON.stringify({ types: { EIP712Domain, ...types }, primaryType, domain, message }, (_k, v) =>
        typeof v === "bigint" ? v.toString() : v,
      );
      const signature = (await circle.signTypedData({ walletAddress, blockchain: "BASE", data })).data?.signature;
      if (!signature) throw new Error("Circle returned no signature");
      return (signature.startsWith("0x") ? signature : `0x${signature}`) as `0x${string}`;
    },
  };
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

await convertAndQuery(circleSigner("0xYourCircleWalletOnBase"));
```

## Spend cap

The `registerPolicy` filter refuses any quote above the cap before Circle is asked to sign; the
recipe sets it from `--max-usd`. `@x402/core` also caps any payment at $1 by default. Circle has
no per-signature limit for typed data, so your cap is the one that counts.

## Check the payment

Each paid line ends with the transaction on basescan.org. The same receipt is in the response
header `PAYMENT-RESPONSE` (base64 JSON: `success`, `transaction`, `network`, `amount`); `@x402/fetch`
decodes it with `decodePaymentResponseHeader`.

## Troubleshooting

| the script stops with | it means |
|---|---|
| `this spends real USDC on Base: set AIMART_MAINNET_CONFIRM=...` | the safety switch is off; nothing was sent |
| `not paid: set CIRCLE_API_KEY and CIRCLE_ENTITY_SECRET` | a variable is missing; nothing was sent |
| `/v1/convert: no payment made: ... filtered out by policies` | the quote is above `--max-usd`; Circle was not asked to sign |
| `/v1/convert: no payment made: ...` (anything else) | Circle refused to sign: a wrong key or entity secret, a wallet not found on `BASE`, or an SCA wallet that is not deployed yet |
| `payment rejected, not charged: insufficient_funds` | the wallet has too little USDC on Base. Other reasons come from the `PAYMENT-REQUIRED` header's `error`; an SCA wallet may be refused here too |
| `payment not settled, result withheld: <reason>` | the work ran but the settlement failed: not charged, unless the line names a transaction; then look it up on basescan.org before paying again |
| `HTTP 4xx, not charged: <message> <hint>` | the call itself failed (bad file, bad SQL); fix what the hint says |

## MCP

`@x402/mcp@2.29.0` pays the MCP tools at `https://balogvn--aimart-parquet.us-east.modal.run/mcp`
with the same payer: `wrapMCPClientWithPayment(client, payer, { autoPayment: true })`, then
`callTool("query_parquet", { artifact_id, sql })`.
