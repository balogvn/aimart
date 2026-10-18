# Economics

A micro-payment business lives or dies by one comparison: **the settlement fee against the
margin, not against the price**. A flat fee equal to the margin makes every call net zero.
That bug shipped in the owner's previous x402 project. This page records the numbers behind
the defaults. Fee schedules change, so they are given as last checked in October 2026.

## Facilitator fees and minimums

| facilitator | networks | settlement fee to us | minimum payment | notes |
|---|---|---|---|---|
| x402.org | Base Sepolia (testnet) | free | n/a | the testnet default |
| **Heurist** | Base | free | n/a | EIP-3009 `transferWithAuthorization`: the payer needs no approval step. **Mainnet default for Base.** |
| Mogami | Base | free | n/a | EIP-3009, Base only. A drop-in alternative to Heurist. |
| **Dexter** | Solana, Base | free | about **$0.00125** on Solana, about **$0.00145** on Base | Base settlement uses Permit2, so the payer must approve first. **Mainnet default for Solana.** |
| PayAI | Base, Solana | 1,000 **lifetime** free credits, then gas × 1.3, about **$0.0015–0.0022** per settlement | | |
| CDP (Coinbase) | Base, Solana | 1,000 free per **month**, then **$0.001** per transaction | | needs a CDP API key; the only route into the CDP Bazaar |

The defaults (`payments/__init__.py`, `DEFAULT_FACILITATORS`) are therefore Heurist then
Dexter on mainnet: both are free and keyless.

## Why the floor is $0.002

| price | fee (PayAI, after credits) | fee (CDP, after 1,000/month) | free facilitator | verdict |
|---:|---:|---:|---:|---|
| $0.001 | −$0.0005 to −$0.0012 net | $0.000 net | rejected (below Dexter's minimum) | unsettleable or loss-making |
| **$0.002** | −$0.0002 to +$0.0005 net | +$0.001 net | +$0.002 net | **the floor**: clears every minimum, and is profitable on free and CDP routes |
| $0.005 | +$0.0028 to +$0.0035 | +$0.004 | +$0.005 | comfortable |
| $0.01 | +$0.0078 to +$0.0085 | +$0.009 | +$0.01 | comfortable |
| **$0.02** | +$0.0178 to +$0.0185 | +$0.019 | +$0.02 | **convert's price**: raised from $0.01 after a buyer panel put every persona's maximum at $0.03–$0.10 |

`inspect` and `preview` sit at the floor. `Payments._check_price_floors` logs an error at
startup for any configured price below a facilitator's published `minPaymentAmountAtomic`.
Even at the floor, PayAI after its credits can lose money on a $0.002 call. That is why PayAI
is not in the defaults.

## Compute cost

Cloud Run's free tier is about **50 hours of 1 vCPU request time a month** (180,000
vCPU-seconds and 360,000 GiB-seconds: 50 hours of a 1 vCPU / 2 GiB instance). It also includes
2 million requests. The service scales to zero, so idle time costs nothing. A hard budget cap
is part of the deployment ([deploy/README.md](../deploy/README.md),
[ADR 0004](adr/0004-hosting-cloud-run.md)).

The figures below are **assumptions**, not measurements of production traffic:

| unit | assumed CPU time | calls inside the free tier per month | cost beyond the free tier* | revenue |
|---|---:|---:|---:|---:|
| classical session: convert + inspect + preview + 2 queries | 4 vCPU-s | about 45,000 | about $0.00012 | $0.024 |
| one premium call (QAOA on 14 assets took 11 s in our re-run) | 10 vCPU-s | about 18,000 | about $0.0003 | $0.50–$1.00 |

\*At Cloud Run's 2025 list prices ($0.000024 per vCPU-second, $0.0000025 per GiB-second,
2 GiB). Check current pricing before relying on these figures. Compute is not the binding
cost at these prices. Settlement fees and demand are.

## Revenue: assumptions, not forecasts

The product discussion projected **about $96k in year 3 from the classical tools**, and
**about $1.23M with 12% premium adoption**. Treat both as **assumptions, not forecasts**. No
traffic data exists yet, and **agent demand for paid data tools is the key unknown**. Neither
the pace of x402 adoption nor the share of agents allowed to spend money can be estimated from
inside this repo.

To make the assumptions inspectable, here is a transparent model. Monthly revenue =
sessions × ($0.024 + premium share × $0.75), where $0.75 is the mean of the two premium
prices and "premium share" is the fraction of sessions that buy one premium call. Prices are
the defaults. There are no fees (free facilitators) and no compute cost (free tier) in the
model.

| sessions per month | 0% premium | 1% premium | 5% premium | 12% premium |
|---|---:|---:|---:|---:|
| 1,000 (conservative) | $24/mo · $288/yr | $32/mo · $378/yr | $62/mo · $738/yr | $114/mo · $1,368/yr |
| 10,000 | $240/mo · $2,880/yr | $315/mo · $3,780/yr | $615/mo · $7,380/yr | $1,140/mo · $13,680/yr |
| 100,000 (base) | $2,400/mo · $28,800/yr | $3,150/mo · $37,800/yr | $6,150/mo · $73,800/yr | $11,400/mo · $136,800/yr |
| 333,000 (optimistic) | $7,992/mo · $95,904/yr | $10,490/mo · $125,874/yr | $20,480/mo · $245,754/yr | $37,962/mo · $455,544/yr |

Reading the table:

- The discussion's $96k a year matches about **333,000 sessions a month** with no premium.
  That is roughly 11,000 agent sessions every day.
- At that same volume, 12% premium adoption gives about **$456k**, not $1.23M. Reaching
  $1.23M at 12% needs about **900,000 sessions a month**, or premium sessions that buy several
  premium calls each. The $1.23M figure therefore rests on stronger assumptions than the $96k.
- Premium share moves revenue more than any other lever. It is also the least certain
  number: the tools give no quantum advantage, so their value has to come from convenience and
  the side-by-side comparison ([quantum.md](quantum.md)).
- The conservative row is a hobby project's numbers. Nothing in this repo makes the base or
  optimistic rows more likely. Only real agent traffic will.

What would change these numbers: a facilitator starting to charge fees (re-run the floor
table), Cloud Run free-tier changes, directory placement ([discovery.md](discovery.md)), and
whether major agent runtimes start paying x402 over MCP by default.
