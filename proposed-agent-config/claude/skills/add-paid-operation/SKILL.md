---
name: add-paid-operation
description: Single-source-of-truth checklist for adding or repricing a paid operation (pricing.OPERATIONS row, price setting, REST route, MCP tool, Bazaar example, discovery docs, wire-level tests). Use whenever a change adds a REST endpoint or MCP tool that should charge, changes a price, renames a paid route or tool, or makes something free.
---

# Add (or reprice) a paid operation

Every price is read from one table, `src/aimart_parquet/payments/pricing.py::OPERATIONS`. The
REST paywall, the MCP paywall, `/pricing`, `/.well-known/x402`, `/llms.txt`, the MCP server
card and the tool descriptions all derive from it. The failure modes this checklist exists for:

- **Delivery without charge.** The REST middleware gates `(method, path)` pairs taken from
  `OPERATIONS`, and the MCP gate looks tools up by `Operation.tool`. A route whose path differs
  from its row by one character, or a tool registered under a different name, is silently
  **free**. Nothing crashes.
- **Charge without delivery.** Settlement happens only for HTTP < 400 or an MCP result with
  `isError: false`. Any failure that returns success (a 200 with an error body, an MCP result
  without `is_error=True`) charges the caller for nothing.
- **Price drift.** A price typed into a docstring, README or test instead of read from settings.

## Steps

1. **Price setting** in `src/aimart_parquet/config.py` (`price_<name>: str = "$0.05"`).
   `Settings._validate` checks price strings by an explicit list of names: add the new one
   there (or derive that list from `OPERATIONS`). Never price below the $0.002 floor
   ([docs/economics.md](../../../docs/economics.md)): facilitators reject or eat the payment.
2. **Row** in `OPERATIONS`: `Operation(key, tool, method, path, price_setting, summary)`. The
   summary is what catalogs and agents read: say what it does and for whom, in one sentence.
3. **Bazaar example** in `src/aimart_parquet/payments/bazaar.py::EXAMPLES[key]`: `input`,
   `input_schema` (self-contained, no `$ref`), `output`. Catalogs validate `input` against
   `input_schema` and silently skip entries that fail. `Payments.routes()` indexes this dict,
   so a missing key fails app startup (good: loud).
4. **One implementation** in `src/aimart_parquet/service.py`. REST and MCP both call it. Raise
   `AimartError` subclasses for every failure: their `http_status` is >= 400, which is what
   cancels payment.
5. **REST route** in `src/aimart_parquet/api/app.py`, declared before the `app.mount("/", ...)`
   line (a `/` mount swallows anything declared after it). The decorator path must equal
   `Operation.path` exactly.
6. **MCP tool** in `src/aimart_parquet/mcp/server.py` with `name=` equal to `Operation.tool`,
   body wrapped in `_guard(...)` so errors become `isError` results, and the description ending
   with `note(settings.price_<name>)`. Remote mode must never accept `path` inputs.
7. **Discovery**: nothing to hand-edit if steps 1-6 are right; check `/pricing`,
   `/.well-known/x402`, `/llms.txt` and `/.well-known/mcp/server-card.json` show it.
8. **Docs**: README pricing table, [docs/payments.md](../../../docs/payments.md),
   [docs/agents-guide.md](../../../docs/agents-guide.md), and `server.json`'s `_meta` prices.

## Tests that must exist (assert on the wire)

Decode what a client actually receives; never read the settings object back.

- REST unpaid -> HTTP 402, body `{}`, and the base64 `PAYMENT-REQUIRED` header decodes to
  `x402Version: 2` with `accepts[].amount == str(usd_to_atomic(price))` (atomic units: "$0.01"
  is "10000"), the configured `payTo`, the expected `network`, and `resource.url` ending in the
  route path. `extensions.bazaar.info.input` validates against `extensions.bazaar.schema`.
- REST probe with an **empty** body still gets 402 (x402scan probes before sending a valid body).
- MCP unpaid `tools/call` -> `isError: true` and `structuredContent.accepts[].amount` equal to
  the REST amount.
- Paid success settles exactly once (count calls on the fake facilitator); paid failure
  (bad input) settles zero times and returns no `PAYMENT-RESPONSE` header /
  `_meta["x402/payment-response"]`.
- Parity: REST header amount == MCP amount == `/pricing` == server card == tool description.

## Prove the tests can fail

A green test that cannot catch the bug is decoration. Before you call it done, break it on
purpose and watch a test go red, then revert:

- change the route path by one character (expect: "unpaid call returns 402" fails);
- rename the MCP tool (expect: "MCP unpaid returns isError" fails);
- make the service return a result instead of raising on bad input (expect: "zero settles" fails).

`make mutate` runs mutmut over `payments/` and the security modules for the systematic version.

## Done when

`make check` passes, the three deliberate breaks each turned a test red, and a local
`make serve` answers an unpaid call with the decoded price you expect (see the
verify-payments skill for the exact commands).
