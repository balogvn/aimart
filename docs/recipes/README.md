# Pay AImart Parquet from the wallet your agent already has

AImart Parquet turns messy data files into clean Parquet and answers SQL over them. Each call is
paid in USDC on Base with x402: no account, no API key, no subscription. If your agent already
holds a Coinbase or Circle wallet, one of these recipes plugs it in. Each is one script: set the
environment variables you already have, run it, and it converts the
[sample CSV](https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.csv) and
queries it for $0.025.

| wallet | language | minutes | recipe |
|---|---|---|---|
| Coinbase CDP server wallet (EVM account) | Python | 5 | [coinbase-cdp-python.md](coinbase-cdp-python.md) |
| Coinbase CDP server wallet (EVM account) | TypeScript | 5 | [coinbase-cdp-typescript.md](coinbase-cdp-typescript.md) |
| Coinbase CDP, through Coinbase's ready-made `CdpX402Client` | TypeScript | 5 | [coinbase-x402client-typescript.md](coinbase-x402client-typescript.md) |
| Circle developer-controlled wallet (EOA on BASE) | Python | 5 | [circle-python.md](circle-python.md) |
| Circle developer-controlled wallet (EOA on BASE) | TypeScript | 5 | [circle-typescript.md](circle-typescript.md) |

A coding agent (Claude Code, Cursor, Codex and others) can also learn the service as a skill:
`npx skills@1.7.5 add balogvn/aimart` installs [skills/aimart-parquet](../../skills/aimart-parquet/SKILL.md).

## Where and how much

Service: `https://balogvn--aimart-parquet.us-east.modal.run` (REST under `/v1`, MCP at `/mcp`).
Payment: x402 v2, scheme `exact`, Base mainnet (`eip155:8453`), USDC. The wallet needs USDC on
Base and nothing else: the facilitator pays the gas, so no ETH and no approval.

| call | price |
|---|---|
| `POST /v1/convert` (a file by `url` or `content_base64`) | $0.02 |
| `POST /v1/convert/upload` (multipart) | $0.02 |
| `POST /v1/inspect` | $0.002 |
| `POST /v1/preview` | $0.002 |
| `POST /v1/query` | $0.005 |

`GET /pricing` and `GET /llms.txt` are free and always current.

## Set a spend cap

Every recipe refuses to sign a quote above `--max-usd` (default `0.02`, the highest price
above). A dearer quote stops the call before anything is signed or sent. Keep it there unless you
call something dearer, and keep only what the agent needs in the paying wallet: in the
Coinbase Python recipe the wallet signs a hash, so CDP's own policies cannot see the amount, and
Circle has no per-signature limit for typed data. The cap in your code is the one that counts.

## When a call fails

- **A call that fails is not charged.** Any answer with status 400 or above other than 402 (a
  bad file, bad SQL, a busy server) settles nothing. The body says why:
  `{"error": {"code", "message", "hint"}}`.
- **402 after paying, with a `PAYMENT-REQUIRED` header**: the payment was refused before the
  work (no USDC, a bad signature, an expired quote). Nothing was charged. The reason is that
  header's `error` field (base64 JSON); the body is `{}`.
- **402 after paying, with a `PAYMENT-RESPONSE` header** and `"success": false`: the work ran
  but the settlement did not go through, so the result is withheld. `errorReason` says why.
  If `transaction` is not empty, a transfer was submitted: look it up on
  [basescan.org](https://basescan.org) before paying again.

The recipes turn each of these into one line that says whether money moved.

## Paid with care: what was checked

Four recipes were run end to end against the real app on a local machine in testnet mode, with
a throwaway key standing in for the Coinbase or Circle account and a fake facilitator that
checked every signature; the test suite does the same for the Python recipes on every change.
The `CdpX402Client` recipe was type-checked only, because that client reaches Coinbase to set up
its account; its payment code is the same file the others run. No real wallet, key or USDC was
used. What runs only on your side is the one call to Coinbase's or Circle's signing API.

Use an **EOA** (a plain account), not a smart account: smart accounts sign ERC-1271, and
whether the facilitator settles those has not been checked.

## Other wallet tools, honestly

- **Coinbase Agentic Wallet** (`npx awal`) and **Coinbase Payments MCP**: retired by Coinbase
  on 2026-11-20 in favor of Wallet MCP, which uses a different wallet. Their x402 payments no
  longer work, so do not fund them. Wallet MCP has not been checked against this service.
- **Circle CLI agent wallets** (`circle services pay`): agent wallets are smart accounts, so the
  same open question as above applies. REST only.
- **Coinbase for Agents** (`agents.coinbase.com/mcp`, `coinbase x402`): pays only services in
  Coinbase's own catalog, which does not list this one.
- **Python AgentKit** (`coinbase-agentkit` 0.7.4): uses x402 version 1, which this service
  refuses. Build the Python recipe's payer from the same CDP account instead.
- **TypeScript AgentKit** (`@coinbase/agentkit` 0.10.4): speaks x402 v2 but sets no spend cap
  and has had no stable release since December 2025. The TypeScript recipe is the safer path.
- **Circle Gateway nanopayments**: a different settlement this service does not offer.
