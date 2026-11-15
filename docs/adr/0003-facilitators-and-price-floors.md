# 0003: Free keyless facilitators by default; $0.002 price floor

- Status: accepted; the mainnet list was amended on 2026-11-15 (see the end)
- Date: 2026-10-17 (built in commit 40b6380; recorded 2026-10-18)

## Context and problem statement

A facilitator verifies and settles each payment and pays the gas. Facilitators differ in
networks, fees, minimum payment and payer experience. Our cheapest calls cost fractions of a
cent, so a per-settlement fee can exceed the margin. Which facilitators do we use by
default, and what is the lowest price we allow?

## Considered options

| option | for | against |
|---|---|---|
| CDP (Coinbase) | the CDP Bazaar listing | needs an API key (a secret to manage); $0.001 per transaction after 1,000 a month |
| PayAI | Base and Solana | 1,000 lifetime credits, then gas × 1.3, about $0.0015–0.0022: more than the margin on a $0.002 call |
| Dexter | free; Solana and Base; catalog at `/discovery/resources` | minimums of about $0.00125 (Solana) and $0.00145 (Base); Base settlement uses Permit2, so the payer must approve first |
| Heurist, Mogami | free; Base via EIP-3009 (no approval step) | Base only |
| x402.org | free, keyless | testnet only |

## Decision outcome

- **Testnet (default):** `https://x402.org/facilitator`.
- **Mainnet:** `https://facilitator.heurist.xyz` first, then `https://x402.dexter.cash`. The
  first facilitator that supports a `(network, scheme)` serves it, so Heurist settles Base
  (EIP-3009) and Dexter settles Solana. Neither needs a key. Operators can override the list
  with `AIMART_FACILITATOR_URLS`.
- **Floor: $0.002** for any price (`inspect`, `preview`). It clears every facilitator minimum
  and stays non-negative on CDP after its free allowance. At initialization,
  `Payments._check_price_floors` logs an error for any price below a facilitator's
  `minPaymentAmountAtomic`.

### Consequences

- Good: no facilitator secret in the default deployment, and no settlement fees on the
  default routes.
- Good: payers on Base sign one EIP-3009 authorization, with no Permit2 approval.
- Bad: no CDP Bazaar listing unless an operator adds CDP and its key. That trade is
  documented in [discovery.md](../discovery.md).
- Bad: free facilitators can change their terms. Re-check fees and minimums before each
  mainnet release, and re-run the table in [economics.md](../economics.md).

## Amendment, 2026-11-15: a dead facilitator is skipped; mainnet is xpay, Primer, then Dexter

### What happened (2026-11-12 to 15)

Preparing the switch to mainnet, `GET https://facilitator.heurist.xyz/supported` answered 502
(`error code: 502`, a Cloudflare page: its origin is down) at every probe on 11-12, 11-14 and
11-15, while the site's root answered 200 and still called the facilitator live. Two things
followed from that one response:

1. **One dead facilitator refused every paid call.** The SDK's `initialize()` asks the
   configured facilitators in order and stops at the first that raises, so
   `Payments.ensure_initialized()` failed as a whole although Dexter, second, answered. Every
   paid call would have been a 503 and the deploy's smoke test would have failed.
2. **The fallback was not the one this record assumed.** With Heurist gone, Dexter would serve
   Base, and Dexter lists x402 v2 'exact' on Base with `assetTransferMethod: permit2`. The 402
   this service sends for Base asks for an EIP-3009 authorization whatever the facilitator
   lists (the SDK builds it from its own table of assets).

### What changed in the code

- A facilitator whose `GET /supported` fails (an HTTP error, a timeout after 10 s, a
  connection error, a body that is not the expected JSON) is skipped with one warning naming
  its URL and the error's type, and the others serve in the configured order.
- A network no answering facilitator offers as x402 v2 'exact' is left out of the 402 while
  the others are quoted (it used to be a bare 500). When no accepted network can be served
  the answer is the typed, unpaid `payments_unavailable` (503 on REST, the typed MCP error),
  and the next request asks again.
- While a facilitator is skipped or a network is left out, one request every 300 s asks every
  facilitator again, so a preferred one that comes back serves again without a restart and a
  dead host is not called on every request. A request that carries a payment never starts
  that round, and a round that finds less than is being served changes nothing.
- Verify and settle are unchanged: there is no failover once a payment is being verified.

### The survey

Probed with `GET <url>/supported` only, on 2026-11-14 and 11-15; terms read on each
operator's own pages on 11-15. "Base" is x402 v2 'exact' on `eip155:8453`. Nothing was
verified or settled, so "keyless" for verify and settle is each operator's statement.

| facilitator | `/supported` | Base | fees and terms, by its own pages | decision |
|---|---|---|---|---|
| xpay `https://facilitator.xpay.sh` | 200 | yes, no `extra` (EIP-3009); also Base Sepolia; nothing else | "Zero — no fees for verify or settle"; authentication "None required"; xpay pays the gas; settles with `transferWithAuthorization`; 100 verify and 50 settle requests a minute; no SLA or terms page ([docs](https://docs.xpay.sh/en/x402-protocol/facilitator)) | **first: serves Base** |
| Primer `https://x402.primer.systems` | 200 | yes, no `extra` (EIP-3009); EVM only | USDC by EIP-3009, "no approval needed"; the facilitator pays the gas; fees, keys, limits and terms are not stated anywhere ([site](https://x402.primer.systems/), [docs](https://docs.primer.systems/facilitator.html)) | **second: Base if xpay is skipped** |
| Dexter `https://x402.dexter.cash` | 200 | yes, `assetTransferMethod: permit2`, `gasSponsored`; no minimum on Base; Solana minimum 780 atomic ($0.00078) | "Dexter charges no facilitator fee"; "No account required"; Dexter Labs Corporation ([site](https://dexter.cash/facilitator)) | **third: serves Solana**; Base only if both others are skipped |
| Heurist `https://facilitator.heurist.xyz` | **502** on 11-12, 11-14, 11-15 | unknown | its site and docs still say live, free, no API key; no notice of a shutdown or a move was found ([site](https://facilitator.heurist.xyz/), [docs](https://docs.heurist.ai/x402-products/facilitator)) | **dropped**: down or discontinued, we cannot tell which |
| PayAI `https://facilitator.payai.network` | 200 | yes, no `extra` | no account to start; 1,000 free credits per `payTo` ($1 = 1,000 credits); a Base EIP-3009 settlement costs 1.66 credits ($0.00166, from 2026-11-13), Solana 0.99; out of credits, `/settle` answers 403 `free_tier_exhausted` ([site](https://facilitator.payai.network/), [pricing](https://facilitator.payai.network/pricing)) | no: after about 600 Base settlements every paid call would fail at settle, after its work, until credits are bought; and $0.00166 is most of a $0.002 call |
| OpenFacilitator `https://pay.openfacilitator.io` | 200 | yes, no `extra`; also Solana | "free"; "No signup, no account, no rate limits"; Apache-2.0; the operator is not named and it asks for donations ([site](https://www.openfacilitator.io/)); its `/supported` names an EVM signer that is not an address | no; the next candidate if one of the three goes |
| Kobaru `https://gateway.kobaru.io` | 200 | yes (`name` "USD Coin", `version` "2"); also Solana | the site says a $0 public facilitator, "No sign-up, rate-limited", "free during early access"; the API docs say every request needs an API key ([site](https://www.kobaru.io/), [docs](https://docs.kobaru.io/integration/direct-api)) | no: its two pages disagree |
| Daydreams `https://facilitator.daydreams.systems` | 200 | yes, no `extra` | no terms for the hosted endpoint; the code's README says "currently in alpha" ([repo](https://github.com/daydreamsai/facilitator)) | no |
| Ultravioleta DAO `https://facilitator.ultravioletadao.xyz` | 200 | yes | terms not read | no |
| Mogami `https://facilitator.mogami.tech` | 200 | x402 v1 only | | no: this service speaks v2 |
| x402.rs `https://facilitator.x402.rs` | 200 | no Base 'exact' | | no |
| x402.org `https://x402.org/facilitator` | 200 | testnet only | | the testnet default, unchanged |
| CDP (Coinbase) | not probed (needs an API key) | | as in the table above | no: a secret to manage |

Permit2 against EIP-3009, from the x402 specification
([scheme_exact_evm.md](https://github.com/coinbase/x402/blob/main/specs/schemes/exact/scheme_exact_evm.md)):
with EIP-3009 the payer signs one authorization and needs neither an approval nor ETH. With
Permit2 the payer must first have approved the Permit2 contract: by an on-chain `approve` paid
with their own ETH, or, when the seller's 402 declares a gas-sponsoring extension and the
client supports it, by signing an EIP-2612 permit (USDC supports it) or an approval
transaction that the facilitator submits. Dexter lists both extensions. The x402 Python
client this repo pins (2.25.0) signs Permit2 payloads and both extensions, the EIP-2612 one
only with a signer that can read the chain. None of it applies to this service's callers: its
402 never names `assetTransferMethod` and declares no gas-sponsoring extension, so every
client takes the EIP-3009 path.

### Decision

- **Mainnet:** `https://facilitator.xpay.sh`, then `https://x402.primer.systems`, then
  `https://x402.dexter.cash`. xpay serves Base. Primer is an independent second for Base that
  settles the same way. Dexter serves Solana, which neither of the others lists, exactly as
  before. Heurist is dropped: an operator who sees it answer again can put it back with
  `AIMART_FACILITATOR_URLS`.
- **Testnet:** unchanged.
- **Floor:** unchanged at $0.002. Dexter's published minimum on Solana fell to $0.00078 and
  it now publishes none on Base; no default charges a settlement fee.

### Consequences

- Good: the payer on Base still signs one EIP-3009 authorization and holds only USDC.
- Good: a facilitator that is down at start costs a warning, not the service.
- Bad: none of the three publishes an SLA, and Primer publishes no terms at all. A
  facilitator cannot redirect or enlarge a payment (the payer's signature fixes amount and
  payee), so the exposure is a call that fails unpaid, or work delivered for a settlement a
  dishonest facilitator only claimed.
- Not verified: that xpay's and Primer's `/verify` and `/settle` accept this service's
  payloads without a key (no payment was made), and whether Dexter verifies an EIP-3009
  payload on Base. If Dexter does not, a start with both others down quotes Base and then
  refuses each paid retry with a 402, unpaid. Make one paid call after switching
  ([deploy/README.md](../../deploy/README.md), "Switching to mainnet").
- Re-run the probe in that section before each mainnet release, as before.
