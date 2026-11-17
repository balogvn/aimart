---
name: aimart-parquet
description: "Convert messy CSV, TSV, JSON, Excel, SPSS, Stata, SAS or digital-PDF tables into clean typed Parquet, then inspect, preview or run read-only DuckDB SQL over them, paying per call in USDC on Base with x402 (from $0.002). Use when a task needs a data file cleaned, typed, converted to Parquet or queried with SQL, and the agent can pay with a Coinbase or Circle wallet or an x402 client."
---

# AImart Parquet

A pay-per-call service: send a data file (by URL or base64), get back a clean, typed, zstd
Parquet artifact and a schema report, then ask questions of it in SQL. No account and no API
key: each call is paid in USDC on Base mainnet with x402 v2.

Service: `https://balogvn--aimart-parquet.us-east.modal.run`
(REST under `/v1`, MCP at `/mcp`, free guide at `/llms.txt`, free prices at `/pricing`).

## When to use it

- A user hands you a CSV, TSV, JSON/NDJSON, xlsx/xls/ods, SPSS (sav/zsav/por), Stata (dta), SAS
  (sas7bdat/xpt) file, or tables in a digital (not scanned) PDF, and wants it clean, typed or as
  Parquet.
- You need a quick answer from such a file (counts, sums, group-bys, joins across files) and
  would rather not install pandas or DuckDB.
- Not for scanned PDFs or images, and not for private data the user has not agreed to send to a
  hosted service. A free local version exists: `pip install aimart-parquet`, then
  `aimart-parquet mcp`.

## Calls and prices

| call | body | price |
|---|---|---|
| `POST /v1/convert` | `{"url": "https://.../file.csv"}` or `{"content_base64": "...", "filename": "file.csv"}` | $0.02 |
| `POST /v1/convert/upload` | multipart: `file`, optional `options` (JSON string) | $0.02 |
| `POST /v1/inspect` | `{"artifact_id": "..."}` or `{"url": "https://.../file.parquet"}` | $0.002 |
| `POST /v1/preview` | `{"artifact_id": "...", "limit": 20}` (`limit` up to 500; `format`: json, markdown or csv) | $0.002 |
| `POST /v1/query` | `{"artifact_id": "...", "sql": "SELECT ... FROM data ..."}` | $0.005 |

- Convert returns `artifact.id`, `artifact.download_url` (signed) and `artifact.expires_at`
  (one hour), plus `report` (rows, columns and types). Pass `artifact_id` to the other calls
  instead of re-sending the file.
- Query takes one read-only `SELECT` over table `data`. Join up to 7 more artifacts with
  `"tables": {"orders": {"artifact_id": "..."}}`. Results are `columns` and `rows` (lists in
  column order); `truncated: true` means aggregate in SQL instead of paging.
- Over MCP the tools are `convert_to_parquet`, `inspect_parquet`, `preview_parquet` and
  `query_parquet`, at the same prices.

Sample file to try: `https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.csv`
(6 rows) and its Parquet, `https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.parquet`.

## Before paying

- Spend only what the user approved. A whole convert-then-query task costs $0.025.
- Always cap the amount per call. The highest price is $0.02 (20000 in USDC's 6-decimal atomic
  units); refuse any quote above what the table says.
- The wallet needs USDC on Base (`eip155:8453`) and nothing else: no ETH, no approval. Use a
  plain account (EOA); smart accounts are not confirmed to work.

## How to pay

An unpaid call answers HTTP 402 with body `{}` and a base64 JSON `PAYMENT-REQUIRED` header: x402
v2, scheme `exact`, network `eip155:8453`, USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`.
An x402 client signs it and retries with `PAYMENT-SIGNATURE`.

### An x402 client with a Coinbase or Circle wallet (checked end to end)

If you can run code, use the ready-made scripts in
[docs/recipes](https://github.com/balogvn/aimart/tree/main/docs/recipes): each converts and
queries the sample for $0.025 and stops with one line on any refusal.

```bash
# Coinbase CDP server wallet (CDP_API_KEY_ID, CDP_API_KEY_SECRET, CDP_WALLET_SECRET set)
AIMART_MAINNET_CONFIRM=I_UNDERSTAND_THIS_SPENDS_REAL_USDC \
  uv run https://raw.githubusercontent.com/balogvn/aimart/main/docs/recipes/coinbase_cdp.py --account 0xYourCdpAccount
# Circle developer-controlled EOA wallet on BASE (CIRCLE_API_KEY, CIRCLE_ENTITY_SECRET set)
AIMART_MAINNET_CONFIRM=I_UNDERSTAND_THIS_SPENDS_REAL_USDC \
  uv run https://raw.githubusercontent.com/balogvn/aimart/main/docs/recipes/circle_dcw.py --wallet 0xYourCircleWallet
```

TypeScript versions are in the same folder. In your own code: Python `x402[evm,httpx]==2.25.0`
(`x402Client` + `ExactEvmClientScheme` + `x402AsyncTransport`, with
`register_policy(max_amount(20000))`), or TypeScript `@x402/fetch@2.29.0` (`wrapFetchWithPayment`)
with a policy that drops quotes above 20000. These clients also pay the MCP tools.

### Coinbase Agentic Wallet CLI (not yet confirmed)

Pays the REST calls only, not MCP tools. Its signing runs in Coinbase's hosted wallet and has
not been checked against this service, so make the first call a $0.002 inspect:

```bash
npx awal@2.12.1 x402 pay https://balogvn--aimart-parquet.us-east.modal.run/v1/inspect -X POST \
  -d '{"url":"https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.parquet"}' \
  --max-amount 2000 --json
npx awal@2.12.1 x402 pay https://balogvn--aimart-parquet.us-east.modal.run/v1/convert -X POST \
  -d '{"url":"https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.csv"}' \
  --max-amount 20000 --json
npx awal@2.12.1 x402 pay https://balogvn--aimart-parquet.us-east.modal.run/v1/query -X POST \
  -d '{"artifact_id":"<artifact.id>","sql":"SELECT count(*) AS n FROM data"}' --max-amount 5000 --json
```

`--max-amount` is in atomic USDC. In this CLI `-h` means headers, not help. The Coinbase
Payments MCP's `make_http_request_with_x402` tool takes the same values (`baseURL`, `path`,
`method` POST, `body`, `maxAmountPerRequest`) and has the same status.

### Circle CLI agent wallet (not yet confirmed)

Circle agent wallets are smart accounts, which this service's facilitator is not confirmed to
accept, and the wallet must have made one transaction on Base before it can sign. REST only.
First call a $0.002 inspect the same way:

```bash
circle services pay "https://balogvn--aimart-parquet.us-east.modal.run/v1/convert" -X POST \
  --address 0xYourAgentWallet --chain BASE \
  -d '{"url":"https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.csv"}' \
  --max-amount 0.02 --timeout 120 --output json
```

`--max-amount` is in USDC here, and `--timeout 120` covers a cold start. Do not use Circle
Gateway (nanopayments): this service does not offer it.

## Reading the result

- **200**: the result is the JSON body. The receipt is the base64 JSON `PAYMENT-RESPONSE` header:
  `success`, `transaction` (look it up on `https://basescan.org/tx/<transaction>`), `network`,
  `amount`.
- **400 or above, not 402**: the call failed and **was not charged**. The body is
  `{"error": {"code", "message", "hint"}}`; do what the hint says.
- **402 after paying, with `PAYMENT-REQUIRED`**: the payment was refused before the work and
  nothing was charged. The reason is that header's `error` (for example `insufficient_funds`).
- **402 after paying, with `PAYMENT-RESPONSE`** and `"success": false`: the work ran but the
  settlement failed, so the result is withheld. `errorReason` says why. If `transaction` is not
  empty, a transfer was submitted: look it up before paying again, and tell the user.
- An x402 v1 payment is refused with a new quote that asks for version 2.

More: [agents guide](https://github.com/balogvn/aimart/blob/main/docs/agents-guide.md),
[payments](https://github.com/balogvn/aimart/blob/main/docs/payments.md).
