---
name: verify-payments
description: How to prove the x402 money path works, at four levels - wire-level tests with a fake facilitator, an unpaid 402 decoded from a local server, a paid end-to-end run on Base Sepolia testnet with a funded test wallet, and the rules for mainnet (never without the human-typed confirmation sentence). Use after any change to payments/, service.py, the MCP gate, prices or routes, before a release, or when asked "do payments work?".
---

# Verify payments

The invariant every level checks: **verify -> do the work -> settle only on success**. A
caller is charged exactly once for a call that succeeded and never for one that failed.
Every check below reads what a client receives (headers, `structuredContent`, the chain),
never our own settings read back.

## Level 1 - wire tests, no network (every change)

```bash
make test        # or: .venv/bin/pytest -q -m "not slow and not network and not testnet"
```

These build the real app with `create_app(settings, payments=Payments(settings, [fake]))`, where
the fake facilitator has a **sync** `get_supported()` and **async** `verify()` / `settle()`
that count calls. They must assert: decoded `PAYMENT-REQUIRED` (amount in atomic units, payTo,
network, asset, resource), settle count == 1 on success and == 0 on failure, MCP parity. Use
`Settings(isolate_jobs=False)`; run app lifespans with `async with app.router.lifespan_context(app):`.

Then prove they can fail: break the money path on purpose (settle before the handler runs;
return `is_error=False` from a failing tool; rename a route) and watch a test go red. `make
mutate` does this systematically for `payments/`.

## Level 2 - a real 402 from a local server (needs internet, spends nothing)

```bash
AIMART_PAY_TO_EVM=0x000000000000000000000000000000000000dEaD \
AIMART_PUBLIC_BASE_URL=http://127.0.0.1:8000 \
  .venv/bin/aimart-parquet serve --host 127.0.0.1 --port 8000 &
curl -s -D - -o /dev/null -X POST http://127.0.0.1:8000/v1/inspect \
  -H 'content-type: application/json' -d '{}' \
  | grep -i '^payment-required:' | cut -d' ' -f2 | tr -d '\r' | base64 -d \
  | jq '.accepts[] | {network, amount, payTo, asset}'
```

Expect `network: "eip155:84532"`, `amount: "2000"` ($0.002), the `payTo` you set, and asset
`0x036CbD53842c5426634e7929541eC2318f3dCF7e` (USDC on Base Sepolia). The first paid request
also fetches the testnet facilitator's `/supported` (`https://x402.org/facilitator`).

## Level 3 - paid end-to-end on testnet (spends faucet USDC only)

Needs a **test** wallet that holds Base Sepolia USDC (Circle's faucet: https://faucet.circle.com).

- The private key lives in `.wallets/` or the user's shell, both gitignored. The agent never
  reads, prints, logs or commits it; the user exports it in their own terminal.
- The server still holds only the receive address (`AIMART_PAY_TO_EVM`); the payer's key is on
  the client side.
- Tests that do this carry `@pytest.mark.testnet` and are excluded from `make test`. Ask the user
  before running them: `.venv/bin/pytest -m testnet`.

Paying client (the same code as [docs/agents-guide.md](../../../docs/agents-guide.md)):

```python
from eth_account import Account
from x402 import x402Client
from x402.http.clients.httpx import x402AsyncTransport
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact import ExactEvmClientScheme

payer = x402Client().register(
    "eip155:84532", ExactEvmClientScheme(EthAccountSigner(Account.from_key(key_from_user)))
)
transport = x402AsyncTransport(payer)  # wraps httpx's default transport; pays a 402 and retries
```

What to check, by value:

1. Success: HTTP 200, a `PAYMENT-RESPONSE` header whose base64 JSON has `success: true` and a
   `transaction` hash; that transaction on https://sepolia.basescan.org moves exactly the quoted
   amount of USDC to `payTo`.
2. Failure: a call with bad input (for example `{"url": "https://example.com/missing.csv"}`)
   returns >= 400, carries **no** `PAYMENT-RESPONSE`, and creates no transaction.
3. MCP: the paid retry with `meta={"x402/payment": payload}` returns `isError: false` and
   `_meta["x402/payment-response"]`; a failing paid call returns `isError: true` and no
   settlement.
4. An underfunded wallet: the reason is in the `PAYMENT-REQUIRED` header of the retry response
   (the body is `{}`). Read the header; a client that reads only the body reports nothing.

## Level 4 - mainnet (real money): the owner, by hand

- Any script that can spend real USDC must refuse to run unless `AIMART_MAINNET_CONFIRM` is set
  to the exact sentence `I_UNDERSTAND_THIS_SPENDS_REAL_USDC`, and `--dry-run` must be exempt
  (demanding a spend confirmation to preview a spend is absurd). This is the pattern from the
  owner's previous x402 project.
- An agent never types that sentence, never sets that variable, and never runs a mainnet spend.
  The Bash guard hook blocks any command containing the sentence. Prepare the command, explain
  what it will spend and to whom, and let the user run it.
- Spend the minimum: one inspect at $0.002 proves the whole loop.
