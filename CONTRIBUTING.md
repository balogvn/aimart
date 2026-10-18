# Contributing

Thanks for helping. This service moves real money on mainnet, so the bar for tests is higher
than usual. This page is the short version. [AGENTS.md](AGENTS.md) is the full working guide,
for AI coding agents and humans alike.

## Setup

You need Python 3.12+ (the repo pins 3.13 in `.python-version`) and
[uv](https://docs.astral.sh/uv/).

```bash
make setup     # uv sync --all-extras --frozen: every extra (gcs, quantum) plus the dev group
make check     # lint + type + test; this is what CI runs, and the definition of green
```

`make` runs everything through `uv run --frozen`, which never rewrites `uv.lock`. Changing the
lockfile is a decision for its own pull request. If `uv` is not on your `PATH`, pass
`UV=/path/to/uv`. To run one test file, call the venv directly:
`.venv/bin/pytest tests/unit/test_detect.py -q`.

`make check` must pass: ruff format and lint, `mypy --strict` over `src/`, and pytest with
branch coverage of at least 90%. Pytest turns warnings into errors.

## Commits and releases

Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`,
`test:`, `refactor:`, `chore:`, and `feat!:` or a `BREAKING CHANGE:` footer for breaking
changes. release-please reads them to choose the next version and write
[CHANGELOG.md](CHANGELOG.md), so do not edit the changelog by hand. Keep each commit to one
logical change. If a commit fixes a bug, the message says what the bug was.

## Test rules

These rules come from bugs that shipped past a green test suite. The full list is in
AGENTS.md, under "Bugs already paid for".

- **Assert on the wire.** For payments, decode the base64 `PAYMENT-REQUIRED` header (over
  MCP, the `structuredContent`) and compare `amount`, `payTo`, `network`, `asset` and
  `resource.url` with expected literals. Never read the `Settings` object back: that proves
  only that the config agrees with itself.
- **Fake the facilitator, not the middleware.** Build the real app with
  `create_app(settings, payments=Payments(settings, [fake]))` and run its lifespan with
  `async with app.router.lifespan_context(app):`. `tests/aimart_testkit.py` provides a
  recording `FakeFacilitator`, app harnesses, and an MCP-over-HTTP client.
  `tests/quantum/test_quantum_wire.py` is a complete paid-flow example.
- **Mutation-test money paths.** For every test that guards money, break the code on purpose
  (rename a paid route, settle on error, drop a discovery field) and confirm the test fails.
  `make mutate` runs mutmut over the payment and security modules.
- **Discovery fields need tests.** Fields that matter for discovery but do nothing for
  function (Bazaar `extensions`, OpenAPI `x-payment-info`, `resource.url`, server-card
  fields) each need a test that fails when the field is missing.
- **`Settings(isolate_jobs=False)` in unit tests.** It keeps them fast and visible to
  coverage. A few tests still exercise the isolated child for real. Any script that triggers
  isolated jobs while running as `__main__` needs an `if __name__ == "__main__":` guard,
  because the forkserver re-imports `__main__`.
- **No real network** in default runs. Mark such tests `network`, or `testnet` for tests that
  spend testnet USDC.

## Adding things

The step-by-step checklists live in `proposed-agent-config/claude/skills/`. They are staged
for the owner's review and become `.claude/skills/` once installed, but anyone can follow
them as plain markdown:

- **A new input format:** `add-input-format/SKILL.md`. In short: detection by magic bytes in
  `engine/detect.py`, a reader in `engine/readers/` registered in the `READERS` map in
  `engine/convert.py`, bomb caps when the format is compressed, golden and property tests,
  and updates to the format tables in the README and the tool description.
- **A new paid operation:** `add-paid-operation/SKILL.md`. In short: one row in
  `payments/pricing.py` `OPERATIONS` (prices, routes and tool names come only from there), a
  price setting in `config.py` at or above the $0.002 floor, one `Service` method used by
  both the REST route and the MCP tool, Bazaar examples in `payments/bazaar.py`, wire tests
  for the 402 quote, settlement on success and none on failure, and updates to the README
  pricing table and `server.json`.

Changes to payments, security or the quantum tier get a focused review against the matching
checklist in `proposed-agent-config/claude/agents/` (`payments-reviewer`, `security-reviewer`,
`quantum-honesty-reviewer`).

## Pull requests

- Describe what changed and why, and how you verified it. Paste the commands.
- Update the docs that describe the changed behaviour in the same pull request: the README
  tables, `docs/`, tool descriptions, and `server.json` when prices or tools change.
- Never commit secrets, wallets or `.env` files. Report vulnerabilities privately as
  described in [SECURITY.md](SECURITY.md).

By contributing you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), and you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
