# 0003: Free keyless facilitators by default; $0.002 price floor

- Status: accepted; the mainnet list was amended on 2026-11-15, and CDP added before it, when
  the owner configures a key, on 2026-11-17 (see the end)
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
  that round. (What such a round may change was corrected the same day: see "After review".)
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
  payload on Base. Because of the second, Dexter is never given Base (see "After review").
  Make one paid call after switching
  ([deploy/README.md](../../deploy/README.md), "Switching to mainnet").
- Re-run the probe in that section before each mainnet release, as before.

### After review (2026-11-15)

A review of the above reproduced three faults, fixed the same day
(`tests/integration/test_fix_facilitator_routing.py`):

- **A retry round could move a network off the facilitator serving it.** The round compared
  only which networks were served, not by whom: one failed `GET /supported` from xpay in a
  round where Dexter answered handed Base to Dexter for the next 300 s or more. Now a
  facilitator that answered before and fails now keeps its place with its last answer, so a
  network moves only on an answer. After three failed rounds in a row (`KEPT_ROUNDS`, 15
  minutes) it is dropped until it answers again; it used to be quoted until a restart. With
  nobody left the service goes back to the typed, unpaid 503.
- **Base could be quoted with only Dexter behind it.** The 402 asks for an EIP-3009
  authorization and Dexter's only v2 'exact' kind on Base says `assetTransferMethod:
  permit2`: a quote nobody was shown to settle, which could be refused (unpaid) or verified
  and then not settled after the work ran. A facilitator's 'exact' kind that names another
  transfer method than the quote on that network is now ignored, so Dexter serves Solana
  only, and Base with xpay and Primer both unavailable is the unpaid 503. This costs
  availability on purpose: if someone shows that Dexter settles EIP-3009 payloads on Base,
  the rule can be relaxed for it.
- **A price floor that is not a whole number stopped every paid call.** `int("0.78")` raised
  after the new SDK server was in place and before it counted as initialized. Such a floor
  is now ignored with a warning naming the facilitator, and floors are checked before
  anything is swapped.

Still true, and accepted: when every facilitator answered at the start nobody is asked
again, so one that dies afterwards is quoted until the process restarts; its paid calls
fail unpaid. On Modal a container lives 60 idle seconds, so this is short there.

## Amendment, 2026-11-17: CDP first for Base when the owner configures a key

### Context

Two days of mainnet sales through xpay listed the service in no catalog: xpay and Primer
publish none, and the catalogs agents search are fed by settlements. Coinbase's CDP
facilitator indexes a route in its Bazaar once it settles a payment for it whose payload
echoes the route's `bazaar` extension, and agentic.market (Coinbase's marketplace, where
agents with Coinbase wallets look for services) shows what the Bazaar holds. This service
already declares the extension on every paid REST route. The original trade-off (above:
"no CDP Bazaar listing unless an operator adds CDP and its key") is now worth taking.

### What was verified, and where

- Endpoint: `https://api.cdp.coinbase.com/platform/v2/x402`, with `/verify`, `/settle` and
  `/supported` (x402 repository's seller quickstart, "Running on Mainnet"; `@coinbase/x402`
  2.1.0, `createFacilitatorConfig`; cdp-sdk 1.53.0's OpenAPI client, base path
  `https://api.cdp.coinbase.com/platform`, routes `/v2/x402/...`).
- Authentication: `/verify`, `/settle` and `/supported` require the API key (`apiKeyAuth` in
  cdp-sdk's `x402_facilitator_api.py`); the `/discovery/*` lists, `/discovery/merchant` and
  `/validate` are public (cdp-sdk's `public_operations.py`). `@coinbase/x402` sends a JWT on
  verify, settle and supported, and none on the Bazaar list.
- The JWT (cdp-sdk `cdp/auth/utils/jwt.py`; `@coinbase/cdp-sdk` 1.62.0 `auth/utils/jwt.js`):
  `EdDSA` for an Ed25519 key (base64 of a 32-byte seed and the 32-byte public key) or `ES256`
  for an ECDSA P-256 key (PEM); header `alg`, `kid` (the key ID), `typ: JWT`, a random
  `nonce`; claims `sub` (the key ID), `iss: "cdp"`, `nbf`/`iat` now, `exp` now + 120 s, and
  `uris: ["<METHOD> api.cdp.coinbase.com/platform/v2/x402/<endpoint>"]`, one request each.
  The x402 Python SDK (2.25.0) makes no JWT; it carries headers per endpoint through
  `FacilitatorConfig.auth_provider` (`AuthProvider.get_auth_headers()`), which
  `LenientSettleClient` already sends on settle (`_get_settle_headers`).
- Fee: the first 1,000 settlements a month free, then $0.001 each (CDP's facilitator page, as
  reported by search on 2026-11-17: docs.cdp.coinbase.com did not resolve from the build
  machine, so this was not read first-hand).

### Decision

- With `AIMART_CDP_API_KEY_ID` and `AIMART_CDP_API_KEY_SECRET` both set (secrets, from the
  `production` environment) and the mode mainnet, the list is
  `https://api.cdp.coinbase.com/platform/v2/x402`, then the three defaults. Without them,
  nothing changes. CDP is given Base only: its Solana offer is dropped from its `/supported`
  answer, so Dexter, free, keeps Solana.
- The JWT is made per request in `payments/cdp.py` with `cryptography` (no new dependency),
  and handed to the SDK as an `AuthProvider`. Settle answers keep the lenient reading
  (`CdpFacilitatorClient` is a `LenientSettleClient`).
- A key CDP refuses costs the listing, and at most the one call CDP had verified when it
  started refusing: any 401/403 keeps CDP out for the life of the process (not asked again,
  dropped from the next quote without `KEPT_ROUNDS`), every 401/403 is logged at ERROR, and
  the deploy's selftest fails on a refused key with one authenticated `GET /supported`.
- After review (2026-11-17): CDP failing verify in a warm container (401/403, 429, 5xx, a
  timeout) made every Base call a 402 with xpay never asked, because a container where every
  facilitator answered never asks again. Verify now falls through, once, to the next
  facilitator offering the network, and that one settles; settle never falls through, and a
  facilitator whose verify failed rests for `VERIFY_REST_S` (docs/payments.md, "Verify falls
  through; settle never does"). This also amends "Verify and settle are unchanged" above.

### Consequences

- Good: Base sales list the service in the CDP Bazaar and on agentic.market, route by route.
- Bad: a secret to manage, the first in the deployment that a third party issues. It is an
  API credential for the facilitator, not a wallet key: it cannot move the payee's USDC.
- Bad: $0.001 per Base settlement after 1,000 a month: half the margin of a $0.002 call
  ([economics.md](../economics.md)). Deleting the key and redeploying undoes it.
- Not verified: a request to CDP with a real key (none exists yet; the deploy's selftest is
  the first), and CDP's listing behaviour beyond its documentation and other sellers'
  reports. CDP's verify answers an invalid payment with HTTP 400 and a schema body
  (`X402VerifyPaymentRejection`, `isValid: false`); `CdpFacilitatorClient` reads that as the
  payer's answer, so the call is refused before any work, unpaid (a 402 on REST, `isError`
  on MCP), without asking xpay or resting CDP.
