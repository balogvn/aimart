# Coinbase CdpX402Client, TypeScript

Pay AImart Parquet with the x402 client that ships in Coinbase's CDP SDK. It builds the signer
for you and has the richest spend controls (per payment, per day, networks, payees). Code:
[`typescript/coinbase/coinbase-x402client.ts`](typescript/coinbase/coinbase-x402client.ts) plus
the shared payment part [`aimart.ts`](typescript/coinbase/aimart.ts). Other wallets:
[recipes](README.md).

This recipe was type-checked but not run: the client reaches Coinbase to set up its account. Its
payment part is the file the [CDP TypeScript recipe](coinbase-cdp-typescript.md) runs, and that
one was run end to end. If you want the path that was exercised, use that one.

## You need

- `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET` and `CDP_WALLET_SECRET`, as Coinbase's docs name them.
- The **name** of your CDP EVM account (an EOA). `CdpX402Client` uses the account with that name
  and creates an empty one if none exists, so check the name. The account needs a little USDC on
  Base: $0.025 runs the example. No ETH and no approval.
- Node 20 or later.

## Install

```bash
git clone --depth 1 https://github.com/balogvn/aimart
cd aimart/docs/recipes/typescript/coinbase
npm ci
```

`npm ci` installs the folder's `package-lock.json`: every package as npm resolved it on 2026-11-03.

In your own project, with the same cutoff:
`npm install --save-exact --before=2026-11-03 @coinbase/cdp-sdk@1.60.0 @x402/core@2.29.0 @x402/evm@2.29.0 @x402/fetch@2.29.0 @x402/extensions@2.29.0 @x402/svm@2.29.0`.
All four `@x402` packages are needed: the client imports them as soon as it loads.

## Run

```bash
export CDP_API_KEY_ID=... CDP_API_KEY_SECRET=... CDP_WALLET_SECRET=...
export AIMART_MAINNET_CONFIRM=I_UNDERSTAND_THIS_SPENDS_REAL_USDC
npx tsx coinbase-x402client.ts --account-name your-cdp-account-name
```

It prints the payer's address, then the same lines as the
[CDP TypeScript recipe](coinbase-cdp-typescript.md#run). It costs **$0.025**: convert $0.02 plus
query $0.005.

## The code

```ts
import { CdpX402Client } from "@coinbase/cdp-sdk/x402";
import { wrapFetchWithPayment } from "@x402/fetch";

const AIMART_URL = "https://balogvn--aimart-parquet.us-east.modal.run";
const SAMPLE_CSV = "https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.csv";
const USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"; // USDC on Base

const payer = new CdpX402Client({ // reads CDP_API_KEY_ID, CDP_API_KEY_SECRET, CDP_WALLET_SECRET
  walletConfig: { type: "eoa", accountName: "your-cdp-account-name" },
  spendControls: {
    maxAmountPerPayment: { atomic: 20_000n, asset: USDC }, // $0.02 a call
    maxCumulativeSpend: { atomic: 1_000_000n, asset: USDC }, // $1 a day
    maxCumulativeSpendWindow: "24h",
    allowedNetworks: ["eip155:8453"],
  },
});
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
console.log((await post("/v1/query", { artifact_id: converted.artifact.id, sql })).rows);
```

The folder's `tsconfig.json` uses `"moduleResolution": "Bundler"`. Under `NodeNext`, TypeScript
reports `CdpX402Client` as not assignable to `x402Client` (the SDK ships two copies of the type);
the code runs the same, and a cast (`payer as any`) silences it.

## Spend cap

Unlike the plain x402 client, `CdpX402Client` applies **no** cap unless `spendControls` is set
(it warns on the console). The recipe sets `maxAmountPerPayment` from `--max-usd` and allows only
Base. Add `maxCumulativeSpend` with `maxCumulativeSpendWindow` for a daily total, and
`allowedPayees` with the `payTo` from the service's 402 quote to pay no one else.

## Check the payment

Each paid line ends with the transaction on basescan.org; the same receipt is in the response
header `PAYMENT-RESPONSE`.

## Troubleshooting

The lines are the [CDP TypeScript recipe's](coinbase-cdp-typescript.md#troubleshooting). A
spend-control refusal arrives as `no payment made: Failed to create payment payload: ...` with
the control's reason; `@x402/fetch` rewraps it, so `instanceof SpendControlError` does not hold.
`CdpX402Client` also offers the `upto` and `authCapture` schemes; this service quotes only
`exact`, so `exact` is what it signs.
