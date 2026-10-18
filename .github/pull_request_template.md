<!-- Title: a Conventional Commit, e.g. "fix(payments): settle only after a successful job".
     release-please builds the changelog and the next version from it. -->

## What and why

<!-- What changes for a caller (an agent paying for a call), and why. Link the issue. -->

## How it was verified

<!-- Commands run and what they showed. "Tests pass" is not a value: name the test that fails
     without this change, or the decoded 402 / response that proves the behaviour. -->

- [ ] `make check` is green locally (lint, mypy --strict, tests, coverage >= 90%)
- [ ] New behaviour is asserted on the wire (decoded `PAYMENT-REQUIRED`, MCP `structuredContent`,
      response bodies), not by reading settings back

## Risk checklist (tick what applies, explain below)

- [ ] **Money path** (prices, `OPERATIONS`, x402 gate, settlement): each money test was broken on
      purpose and seen to fail; prices stay at or above the $0.002 floor
- [ ] **Security boundary** (SSRF guard, DuckDB lockdown, decompression/parsing limits, paths)
- [ ] **Quantum tier / provenance**: simulator disclosed, classical baseline included, no
      advantage claims (docs/specs/quantum-premium.md)
- [ ] **Discovery fields** (`x-payment-info`, Bazaar extensions, server card, `server.json`,
      `llms.txt`): pinned by a test that fails when the field is missing
- [ ] **Deploy / CI** (`.github/`, `deploy/`, `Dockerfile`): actions pinned by SHA, least
      privilege, no secret in a log or an argument
- [ ] Docs, README tables and `server.json` updated for any tool, price or behaviour change

## Notes for the reviewer

<!-- Anything you are unsure of, deliberately left out, or want a second opinion on. -->
