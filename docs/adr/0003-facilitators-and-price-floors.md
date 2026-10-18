# 0003: Free keyless facilitators by default; $0.002 price floor

- Status: accepted
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
