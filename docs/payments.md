# Payments

Callers pay per call in USDC with **x402 v2**, using the `exact` scheme. The server is
receive-only: it knows the `payTo` addresses and nothing else. A **facilitator** checks each
signed authorization and submits the on-chain transfer, paying the gas itself.

## The invariant: verify → run → settle only on success

```text
payment arrives ─► verify with facilitator ─► invalid? ─► 402 / PaymentRequired (no work done)
                        │ valid
                        ▼
                   run the operation ─► failed (HTTP >= 400 / isError) ─► return the error, never settle
                        │ succeeded
                        ▼
                   settle with facilitator ─► failed ─► withhold the result, return the payment error
                        │ settled
                        ▼
                   result + settlement receipt
```

This order is what guarantees that no caller pays for an error and no work is given away:

- **REST:** the x402 FastAPI middleware buffers the route's response and settles only when
  the status is below 400. Every failure is a typed `AimartError` with status ≥ 400, so it
  cancels payment. This includes `busy` (503) and `timeout`, which means there is no "charge
  before capacity" bug.
- **MCP:** `payments/mcp_gate.py` verifies, calls the tool, and settles only if the tool
  result has `isError=false`. Every tool wraps its work in `_guard`, which turns any
  `AimartError` into an `isError` result.
- **Nothing is quoted that can only fail.** Before the 402 or PaymentRequired, a REST body is
  validated against its route's own model (`_body_check` in `api/app.py`), an upload form's
  small fields are read by `FormPeekMiddleware` and checked (`_form_check`), and MCP tool
  arguments go through the SDK's own validation (`argument_checker` in `mcp/server.py`). An
  invalid one is a 400 `invalid_request` or an `isError` result, and a tool this deployment
  does not serve is a free "Unknown tool" error. An unpaid empty body or `{}`, an unpaid form
  without its file, and an unpaid MCP call with no arguments stay price probes: catalogs
  replay them to read the price.
- **An output nobody paid for does not stay.** Outputs stored during a paid call are
  provisional (`artifacts.outputs_of`) until the payment settles. The REST middleware and the
  MCP gate commit them on settlement and delete them otherwise: an error status, an `isError`
  result, a failed settlement, an exception, or a cancellation.
- Prices are strings such as `"$0.02"`. The x402 SDK turns them into integer atomic units on
  the wire (USDC has 6 decimals, so `"$0.02"` becomes `20000`); `pricing.usd_to_atomic` is an
  independent exact re-implementation used for the facilitator price-floor check and as the
  tests' oracle, and the wire tests pin both against hand-written amounts. Floats never
  touch money.

## REST flow (real header names)

1. **Request without payment** → `402 Payment Required` with an empty JSON body `{}` and the
   header **`PAYMENT-REQUIRED`**: base64 of a JSON `PaymentRequired` object:

   ```json
   {
     "x402Version": 2,
     "error": "Payment required",
     "resource": {
       "url": "https://YOUR-SERVICE.run.app/v1/inspect",
       "description": "Describe a Parquet file's schema, null counts, ranges and labels without reading rows.",
       "mimeType": "application/json",
       "serviceName": "AImart Parquet",
       "tags": ["data", "parquet", "etl", "csv", "mcp"]
     },
     "accepts": [{
       "scheme": "exact",
       "network": "eip155:84532",
       "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
       "amount": "2000",
       "payTo": "0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
       "maxTimeoutSeconds": 300,
       "extra": {"name": "USDC", "version": "2"}
     }],
     "extensions": {"bazaar": {"...": "discovery metadata: input example, schemas, output example"}}
   }
   ```

   This was captured from the app in-process with testnet settings. `extensions` is shortened
   here. `accepts` has one entry per configured network.
2. **The client signs** one `accepts` entry. For Base, that is an EIP-3009
   `transferWithAuthorization` for exactly `amount` of `asset` to `payTo`. The client then
   retries the same request with the header **`PAYMENT-SIGNATURE`** (base64 `PaymentPayload`).
3. **Success** → the normal response plus the header **`PAYMENT-RESPONSE`** (base64
   settlement):
   `{"success": true, "transaction": "0x…", "network": "eip155:84532", "payer": "0x…", "amount": "2000"}`
   (it settles exactly the quoted `amount`). `success`, `transaction`, `network` and `amount`
   are always there. `amount` is the quoted amount, the one the payer signed: a facilitator may
   leave it out of its settlement (xpay does) or state another, and the receipt names the
   quoted one all the same. `transaction` is `""` when the facilitator names none. `payer` is
   there when the facilitator reports it.
4. **Rejected payment** (before the work: a bad signature, a wrong amount, a refused verify)
   → `402` again with body `{}`. The reason is in the new `PAYMENT-REQUIRED` header's `error`
   field, and there is no `PAYMENT-RESPONSE`. Nothing was charged.
5. **Failed settlement** (after the work: the facilitator refused to settle, did not answer,
   or answered without saying that it settled) → `402` with body `{}` and the header
   **`PAYMENT-RESPONSE`**, and no `PAYMENT-REQUIRED`:
   `{"success": false, "errorReason": "insufficient_funds", "transaction": "", "network": "eip155:84532"}`.
   The result is withheld and the converted file deleted. `errorReason` says why, and
   `errorMessage` says more when there is more to say. `transaction` is `""` unless the
   facilitator named one: if it did, a transfer was submitted and may still have been made, so
   look that transaction up on chain before paying again.

A client that has paid must therefore read whichever header a `402` carries, because the body
says nothing: `PAYMENT-REQUIRED` (`error`) or `PAYMENT-RESPONSE` (`errorReason`).
[`examples/agent_pays.py`](examples/agent_pays.py) does (`refusal`).

What counts as "settled" is the service's own reading of the facilitator's answer
(`payments/settle.py`), not the SDK's strict one, because by then the payer's money may have
moved: any 2xx answer whose JSON says `"success": true` is a settlement however the rest is
worded (the amount as a number, no `network`, the hash under `txHash` or `transactionHash`),
unless something beside it says otherwise (an error, reason or message, a `status` such as
"failed", `ok: false`); `"success": false` is a refusal; `success` set to anything else (null,
"failed", -1) is not settled; an answer with no `success` is a settlement only if it names a
transaction id of the network's shape (0x and 64 hex digits on Base, a base58 signature on
Solana) and nothing that says otherwise; everything else, and every status that is not 2xx, is
not settled. Each answer read outside the SDK's schema is one log line (`aimart.payments.settle`:
the facilitator's URL, the HTTP status, the transaction), which is what the owner reconciles
against the chain. A refusal that names a transaction (a transfer submitted and perhaps still
pending) is logged the same way, however it is worded: it stays a refusal, and the transfer
may still land.

Prices are also published, for free, in OpenAPI: every paid operation in `/openapi.json`
carries `x-payment-info`, for example
`{"price": {"mode": "fixed", "currency": "USD", "amount": "0.005"}, "protocols": [{"x402": {"version": 2}}]}`.
They also appear at `GET /pricing` and `GET /.well-known/x402`.

## MCP flow (x402 MCP transport)

The format follows the x402 v2 MCP transport spec. It is implemented in
`payments/mcp_gate.py`, because the SDK's `x402.mcp` helpers target mcp 1.x
([ADR 0002](adr/0002-x402-v2-rest-middleware-and-mcp-extension.md)).

| step | wire |
|---|---|
| discover | `initialize`, `tools/list`: free, never gated |
| unpaid `tools/call` to a paid tool | result `isError: true`. `structuredContent` is the `PaymentRequired` object (same shape as above, `resource.url` = `https://host/mcp#<tool>`), and the same JSON is the first text block |
| paid retry | same call with `params._meta["x402/payment"]` = the `PaymentPayload` JSON (an object, not base64) |
| success | the tool's normal result plus `_meta["x402/payment-response"]` = the settlement |
| settlement failure after the tool ran | `isError: true`. `structuredContent` is a `PaymentRequired` object whose `error` is `Payment settlement failed: <reason>`, and the receipt sits in it, at `structuredContent["x402/payment-response"]` (not in `_meta`, where a successful one is): `{"success": false, "errorReason": …, "transaction": …, "network": …}`, with the transaction the facilitator named or `""`. The same JSON is the first text block. The tool output is withheld |
| x402 v1 payload | rejected with a PaymentRequired that asks for `x402Version` 2 |

`verify_provenance` is free and never gated.

## Facilitators

`AIMART_FACILITATOR_URLS` is a comma-separated list in priority order. Each
`(network, scheme)` is served by the first facilitator that answered `GET /supported` and
lists it. The defaults (`payments/__init__.py`, `DEFAULT_FACILITATORS`):

| mode | facilitators | networks |
|---|---|---|
| testnet (default) | `https://x402.org/facilitator` | Base Sepolia `eip155:84532` (and Solana devnet if `AIMART_PAY_TO_SVM` is set and supported) |
| mainnet | `https://facilitator.xpay.sh`, then `https://x402.primer.systems`, then `https://x402.dexter.cash` | Base `eip155:8453` (xpay, EIP-3009; Primer if xpay is skipped) and Solana `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp` (Dexter) |

xpay comes first on mainnet because it settles Base with standard EIP-3009 transfers, charges
no fee and needs no key; Primer settles Base the same way and is there for when xpay does not
answer. Neither lists Solana, so Dexter, last, serves it. Dexter is free too, but it lists
Base as Permit2, which is not what a 402 from this service asks a payer to sign, so it is
never given Base: with xpay and Primer both unavailable, Base is refused (the unpaid 503
below), not quoted. Heurist was the first
default until 2026-11-15: its `/supported` answered 502 for days. The fees and minimums are
in [economics.md](economics.md), and the choice and the survey behind it are recorded in
[ADR 0003](adr/0003-facilitators-and-price-floors.md).

**What a payer on Base needs.** USDC on Base for the price, and nothing else: no ETH and no
approval. The client signs one EIP-3009 `TransferWithAuthorization` (EIP-712, domain
`USD Coin` version `2` on chain 8453, the USDC contract) for the quoted amount to the quoted
`payTo`, valid 300 seconds, and sends it with the retry. The facilitator submits it and pays
the gas. The 402 never names `assetTransferMethod`, so no client is sent down the Permit2
path, and a facilitator whose 'exact' kind on a network names another method than the quote
does (Dexter on Base: `permit2`) counts as not offering that network
(`facilitator's kind ignored` in the log, at INFO).

**Lazy initialization.** Facilitator capabilities are fetched on the first paid request, not
at boot. A scale-to-zero host cold-starts on that request, and a facilitator blip at that
moment should cost one failed (unpaid) request, not a crash loop. Watch out for the SDK middleware's
`sync_facilitator_on_start=False`: with that flag the SDK **never** initializes the resource
server on its own. `Payments.http_middleware()` therefore initializes it before handing paid
requests to the SDK.

**One dead facilitator does not stop the rest.** Every configured facilitator is asked for
`/supported` at once and given 10 seconds. One that fails (an HTTP error, a timeout, a
connection error, a body that is not the expected JSON) is skipped with one warning,
`facilitator skipped: GET /supported failed`, that names its URL and the error's type, and the
others serve in the configured order. What follows from that:

| after asking | unpaid request | paid request |
|---|---|---|
| every accepted network has a facilitator | 402 quoting each | verified, run, settled |
| some accepted network has none (x402 v2 'exact') | 402 quoting only the networks that have one | the same, on those networks |
| no accepted network has one | `503 payments_unavailable` (REST), the same typed error as an MCP result; never a 402 | the same 503: nothing is verified, nothing is charged |

A network is never quoted unless a facilitator lists it the way it is quoted. After a 503
the next request asks again. While a facilitator is skipped or a network is left out, one
request every 300 seconds asks them all again (never a request that carries a payment: its
authorization is already running out), so a facilitator that comes back serves again without
a restart, and a dead host is not called on every request.

In those rounds a network moves only on an answer. A facilitator that answered before and
fails `/supported` now keeps its place with its last answer (its verify and settle can work
while `/supported` does not), so one 502 does not hand its network to the next in the list.
After three failed rounds in a row (`KEPT_ROUNDS`, 15 minutes) it is dropped, with the
warning `facilitator dropped`, until it answers again: its networks go to the next
facilitator that lists them, or out of the quote, or, with nothing left, to the 503. A
facilitator that answers and no longer lists a network loses it at once.

Verify and settle have no failover: once a payment is being verified, its facilitator's
failure is that call's typed, unpaid failure, as in the tables above. When every facilitator
answered at the start nobody is asked again, so one that dies afterwards is quoted until the
process restarts (on Modal, after 60 idle seconds) and its paid calls fail unpaid.

## Price floors

Facilitators publish minimums (`minPaymentAmountAtomic` in their `/supported` kinds). At
initialization, `Payments._check_price_floors` compares every active operation's price with
the minimum and **logs an error** for any price below it, because such payments will be
rejected. A minimum that is not a whole number of atomic units is ignored with the warning
`facilitator's price floor ignored`. Our own floor is **$0.002**, set by `config.py` defaults and the
fee-versus-margin arithmetic in [economics.md](economics.md). Do not set
`AIMART_PRICE_*` below it.

## Testnet versus mainnet

| | testnet (default) | mainnet |
|---|---|---|
| `AIMART_NETWORK_MODE` | `testnet` | `mainnet` |
| Base | Sepolia `eip155:84532`, USDC `0x036CbD53842c5426634e7929541eC2318f3dCF7e` | `eip155:8453`, USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |
| Solana | devnet `solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1` | `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp` |
| facilitators | x402.org | xpay, Primer, Dexter |
| money | test USDC from a faucet | **real** |

To switch, an operator sets `AIMART_NETWORK_MODE=mainnet` plus mainnet `AIMART_PAY_TO_EVM`
and/or `AIMART_PAY_TO_SVM`, then redeploys and runs the smoke test against the live service:

```bash
make smoke URL=https://YOUR-SERVICE.run.app \
  SMOKE_ARGS="--expect-network eip155:8453 --expect-pay-to 0xYourMainnetAddress --require-provenance"
```

The smoke test decodes every paid route's and tool's live 402 and compares amount, `payTo`,
network and the canonical USDC contract with independent expectations. A deployment is not
"switched" until that passes. State that is keyed by network (such as directory listings and
records from testnet) must be re-checked after the switch, because records written on testnet
can keep routing traffic.

Other payment settings: `AIMART_PAYMENTS_ENABLED=false` serves everything for free (no payee
required). `AIMART_BAZAAR_TAG` places a discovery tag in `accepts[].extra.tag`.
`AIMART_PRICE_CONVERT`, `AIMART_PRICE_INSPECT`, `AIMART_PRICE_PREVIEW`,
`AIMART_PRICE_QUERY`, `AIMART_PRICE_QUANTUM_COMPRESS` and `AIMART_PRICE_QUANTUM_PORTFOLIO`
override prices. They must look like `$0.01`, with at most 6 decimals.
