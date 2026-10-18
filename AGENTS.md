# AGENTS.md

Guide for any AI coding agent (and human) working in this repo. Claude Code reads it through
`CLAUDE.md` (staged in `proposed-agent-config/` until the owner installs it; see "Agent
tooling"); other agents read it directly. Keep it short enough to read every session and
accurate enough to act on: if you change a command, path or rule below, change it here too.

## What this is

**AImart Parquet** (`aimart-parquet` on PyPI, import `aimart_parquet`): a pay-per-call service
that AI agents use to turn messy CSV/TSV, JSON/NDJSON, Excel (xlsx/xls/ods), SPSS (sav/zsav/por),
Stata (dta) and SAS (sas7bdat/xpt) files into clean, typed, zstd Parquet, then inspect (footer
only), preview, and query them with sandboxed read-only DuckDB SQL. A premium tier adds
simulated-quantum kernel PCA and QAOA portfolio selection, each with a classical baseline, and
every hosted artifact carries an ML-DSA-65 (post-quantum) provenance signature.

One ASGI app serves the REST API (FastAPI) and a remote MCP server (Streamable HTTP at `/mcp`).
Callers pay in USDC with x402 v2 on Base or Solana; testnet (Base Sepolia, `eip155:84532`) is the
default. `aimart-parquet mcp` is a free local stdio MCP server for local files.

**The money is real on mainnet.** A bug in the payment path does not produce a stack trace: it
produces a caller charged for nothing, or work given away. The server holds no private keys,
only receive (`payTo`) addresses.

## Setup and commands

Python 3.13 venv at `.venv`, managed by uv. If `uv` is not on PATH, pass `UV=/path/to/uv`.

| command | does |
|---|---|
| `make setup` | `uv sync --all-extras --frozen` (all extras: `gcs`, `quantum`; plus the dev group) |
| `make fmt` | `ruff format .` then `ruff check --fix .` (rewrites files) |
| `make lint` | `ruff format --check .` and `ruff check .` |
| `make type` | `mypy src` (strict) |
| `make test` | `pytest -q -m "not slow and not network and not testnet" --cov --cov-report=term-missing` (coverage gate 90%) |
| `make check` | lint + type + test: the definition of green; CI runs the same commands |
| `make serve` | `aimart-parquet serve`: REST + MCP on `$PORT` or 8000 (needs `AIMART_PAY_TO_EVM` and/or `AIMART_PAY_TO_SVM`, or `AIMART_PAYMENTS_ENABLED=false`) |
| `make mcp` | `aimart-parquet mcp`: free local stdio MCP |
| `make docker` | `docker build -t aimart-parquet:dev .` |
| `make smoke URL=https://host` | `scripts/smoke.py`: decodes live 402s and checks prices, payTo, network (`SMOKE_ARGS="--expect-pay-to 0x..."`) |
| `make mutate` | `mutmut run` over `payments/`, `sources.py`, `engine/safety.py` (see `[tool.mutmut]`) |

Make targets run through `uv run --frozen`, which never rewrites `uv.lock`. For a single test,
call the venv directly: `.venv/bin/pytest tests/unit/test_detect.py -q -k magic`. The CLI also
converts one file: `.venv/bin/aimart-parquet convert data.sav -o out.parquet`, and
`.venv/bin/aimart-parquet inspect out.parquet`.

Pytest runs with `filterwarnings = error`, `--strict-markers`, and markers `slow`, `network`,
`testnet` (spends testnet USDC against a live facilitator; never in default runs).

## Code map

Module docstrings explain the why; read the one you are about to change.

- `config.py`: `Settings` from `AIMART_*` env vars: network mode, payTo addresses, facilitators,
  prices, limits, artifact storage, signing keys. There is no private-key setting, on purpose.
- `errors.py`: `AimartError` subclasses, each with `code`, `http_status` (always >= 400) and a
  concrete `hint`. Raising one is what guarantees the caller is not charged.
- `engine/`: the pure conversion engine. `detect.py` (magic bytes over extensions), `safety.py`
  (gzip/zip bomb caps), `text.py` (UTF-8 transcoding), `duck.py` (sandboxed DuckDB),
  `readers/` (delimited, jsonish, excel, statistical, columnar, pdf; the `READERS` map is in
  `convert.py`), `infer.py` (type ladder in SQL), `names.py`, `convert.py` (pipeline and
  report), `writer.py` (DuckDB `COPY` by default, pyarrow when labels exist), `inspect.py`
  (footer-only stats), `tabular.py` (preview and SELECT-only query), `isolate.py` (forkserver
  child per heavy job), `models.py` (the pydantic contract agents read).
- `quantum/`: premium tools (`kernel_pca.py`, `portfolio.py`, `data.py`, `models.py`). Served only
  when the `quantum` extra is installed AND `AIMART_ENABLE_QUANTUM=true` (off by default; a buyer
  panel valued them at $0). `pricing.active_operations(settings)` decides. Spec:
  `docs/specs/quantum-premium.md`.
- `demand.py`: demand signals. Unsupported file extensions are counted, and agents can describe
  unmet needs via the free `request_capability` tool / `POST /v1/feedback`. Logged as
  `demand_signal`; `GET /v1/demand` behind `AIMART_ADMIN_TOKEN`. This decides the roadmap.
- `provenance.py`: ML-DSA-65 manifests and signatures for every hosted artifact.
- `sources.py`: SSRF-safe URL fetch (validated IPs at connect time, re-checked redirects, port
  allow-list, size cap, deadline), base64 decode, `safe_filename`.
- `artifacts.py`: `LocalArtifactStore` (TTL disk, HMAC-signed links) and `GcsArtifactStore`.
- `service.py`: `Service`, the **one** implementation of every operation, used by REST and MCP.
  `query` takes a map of table names (`data` + up to 7 joined artifacts) for one query price.
- `payments/`: `pricing.py` (`OPERATIONS`, the single source of prices, routes and tool names;
  `usd_to_atomic`), `__init__.py` (`Payments`: x402 resource server, REST route configs with the
  Bazaar extension, lazy facilitator init, price-floor check), `mcp_gate.py` (x402 for MCP tool
  calls), `bazaar.py` (catalog examples).
- `mcp/server.py`: `build_mcp(...)`, the tools and their descriptions (the whole interface an
  agent sees). Remote mode takes `url`/`content_base64`/`artifact_id`; local mode adds `path`.
- `api/`: `app.py` (`create_app`; routes, OpenAPI `x-payment-info`, middleware order),
  `middleware.py` (access log, rate limit, body limit), `discovery.py` (`/llms.txt`, server card,
  pricing), `schemas.py`.
- `cli.py`: `serve`, `mcp`, `convert`, `inspect`.

Outside `src/`: `tests/` (`aimart_testkit.py` helpers, `unit/`, `integration/`, `quantum/`),
`scripts/smoke.py` (live 402 decoder), `Dockerfile`, `.github/workflows/` (`ci.yml`,
`codeql.yml`, `release.yml`, `scorecard.yml`), `deploy/` (Cloud Run), `server.json` (MCP
Registry manifest; prices and tools there must match `OPERATIONS`),
`docs/examples/agent_pays.py` (paying client example).

Docs: [architecture](docs/architecture.md), [payments](docs/payments.md), [privacy](docs/privacy.md),
[agents guide](docs/agents-guide.md), [quantum tier](docs/quantum.md),
[discovery](docs/discovery.md), [economics](docs/economics.md), [ADRs](docs/adr/README.md),
[security](SECURITY.md), [contributing](CONTRIBUTING.md).

## Conventions

- **Comments explain why**, and the best ones carry the incident that caused them. A fix
  without its reason invites the same bug back. Do not narrate what the code does.
- **One implementation per question.** REST and MCP call the same `Service` method; every price,
  route and tool name comes from `pricing.OPERATIONS`; discovery documents are generated from
  the same table. If you need "is this paid" or "what does this cost" somewhere new, import
  the existing answer. A second copy that agrees today is a disagreement later.
- **Text is interface.** Tool descriptions, field descriptions and error hints are what calling
  agents read to decide what to do. Make hints concrete ("pass `sheet`", not "check input").
- **Errors are typed.** Raise an `AimartError` subclass, never a bare exception, from anything a
  caller can trigger.
- Line length 100; ruff with many rule sets; mypy strict. `make fmt` before committing.
- Conventional Commits (`feat:`, `fix:`, `docs:`...): release-please builds the changelog.

## Money invariants

1. **verify -> work -> settle only on success.** REST settles only for HTTP < 400 (the x402
   middleware); MCP settles only when the tool result has `is_error=False` (`mcp_gate.py`). A
   failure must therefore always surface as >= 400 or `isError`, never as a 200 with an error.
2. **Paid means listed.** A paid route's path and a paid tool's name must match its
   `OPERATIONS` row exactly; a mismatch makes it silently free.
3. **Integer money.** Prices are strings like `"$0.01"`; `usd_to_atomic` converts exactly
   ("$0.01" -> 10000). No floats near money.
4. **Price floor $0.002.** Dexter rejects payments below about $0.00125 (Solana) / $0.00145
   (Base); fee-charging facilitators cost $0.001 (CDP) to ~$0.0022 (PayAI) per settlement;
   below the floor a call is unsettleable or loss-making ([economics](docs/economics.md)).
5. **Receive-only server.** Only `payTo` addresses in config. Never add a private key setting,
   never log a payment payload in full.
6. **Testnet by default.** Mainnet only by explicit owner configuration.

## Quantum honesty contract

From `docs/specs/quantum-premium.md`, non-negotiable: every quantum result says it ran on a
**simulator** and names the device; ships an exact or standard **classical baseline** computed
in the same call with a comparable metric; and never claims speed-ups, "quantum advantage" or
security properties it does not have. ML-DSA-65 proves origin and integrity of a file, nothing
more; "tamper-proof via quantum kernels" is not a real technique and must not be implied.

## Testing rules

- **Assert on the wire.** Decode the base64 `PAYMENT-REQUIRED` header (or MCP
  `structuredContent`) and compare `amount`, `payTo`, `network`, `asset`, `resource`. A test that
  reads the settings object back proves only that the config agrees with itself.
- **Mutation-test money paths.** For every money test, break the code on purpose (rename the
  route, settle on error, return success from a failure) and confirm the test fails. A test
  that cannot fail when the bug is reintroduced is decoration.
- **Fields load-bearing for discovery but inert for function get omitted** by anyone reasoning
  from function. Bazaar `extensions`, `x-payment-info`, `resource.url`, server-card fields: each
  needs a test that fails when it is missing.
- **Fake the facilitator, not the middleware.** Build the real app with
  `create_app(settings, payments=Payments(settings, [fake]))`; the fake has a sync
  `get_supported()` and async `verify()`/`settle()` that count calls. Run lifespans with
  `async with app.router.lifespan_context(app):`.
- **`Settings(isolate_jobs=False)` in unit tests** (fast, coverage-visible). Keep a few tests
  that exercise the isolated child for real. Any script or test module that triggers isolated
  jobs as `__main__` needs an `if __name__ == "__main__":` guard: forkserver re-imports `__main__`.
- No real network in default tests (`allow_private_urls=True` only to fetch from a local test
  server). Mark anything else `network` or `testnet`.

## Security rules

- **Remote callers never name server paths.** `path` inputs exist only in local stdio mode;
  `output_path` likewise. Every caller-supplied filename goes through `safe_filename`.
- **SSRF guard stays at connect time** (`sources.GuardedBackend`): resolve, check every address
  with `ip_is_public`, connect to the checked address; re-check every redirect hop.
  `allow_private_urls` is for tests only.
- **DuckDB lockdown on every connection** (`engine/duck.connect`): `allowed_directories` limited
  to the job's own directories, `enable_external_access=false`, no extension autoload, then
  `lock_configuration=true` last. The query path runs exactly one SELECT with a row cap and an
  interrupt at the deadline.
- **Decompression is streamed against caps**; zips are checked from the central directory
  before inflating; heavy parsing runs in the forkserver child with a hard deadline.
- **Secrets**: never read, print or commit `.env*` (except `.env.example`), `.wallets/`,
  `.local/`, `*.pem`, `*.key`. Check a secret by length (`${#NAME}`), never by value. The server
  holds no private keys; a paying test wallet's key stays on the client side with the user.
- Report vulnerabilities per [SECURITY.md](SECURITY.md).

## Definition of done

- `make check` is green (lint, type, tests, coverage >= 90%).
- New behaviour has tests that assert values a real client receives, and money or security
  tests were shown to fail when the bug is reintroduced.
- Docs that describe the changed behaviour are updated in the same change (README tables,
  `docs/`, tool descriptions, `server.json` when prices or tools change).
- Payments, security or quantum changes were reviewed by the matching reviewer
  (`payments-reviewer`, `security-reviewer`, `quantum-honesty-reviewer`; see below).
- Nothing was committed, pushed, tagged, published or deployed without the owner asking.

## Bugs already paid for: do not reintroduce

From this repo and the owner's previous x402 project (x402 Mesh); each one shipped past a green
test suite.

- **The passing test that protects the bug.** A test used the one path the client would call as
  its example of an *unknown* path, and passed while every real request 404'd. Assert what a
  real consumer receives, not what the code contains.
- **"Deployed" is not a value.** A secret listed as deployed was an empty string in the
  container; payouts failed for two hours while the run was reported clean. Verify by
  decoding a live 402 (`make smoke`) and checking secrets by length.
- **Two fields called "tags".** A leaderboard filtered on `accepts[].extra.tag`, not the route's
  `tags` array, and also required `extra.asset`. Discovery fields that do nothing for function
  get dropped; pin them with tests (here: `bazaar_tag` -> `accepts[].extra.tag`).
- **Fee versus margin.** A flat settlement fee equal to the margin made every request net zero.
  Compare the fee to the margin, not to the payment; hence the $0.002 floor.
- **A truncated list is worse than no list.** A catalog query with `limit=100` returned an
  arbitrary slice and reported "rank 4 of 54" for rank 14 of 831. Paginate to the reported
  total, and fail rather than silently cap.
- **Two implementations of one question** disagreed (a readiness check versus the router) and
  hid an outage. Import the one answer; do not re-derive it.
- **State outlives its assumptions.** Records written on testnet kept routing traffic after the
  switch to mainnet. Anything keyed by network must check the network.
- **The error that says nothing.** On a failed paid retry the body is `{}`; the reason is in the
  `PAYMENT-REQUIRED` header. Clients and docs must read the header.
- **The "charge before capacity" bug does not exist.** The x402 middleware buffers the response
  and settles only below 400; a 503 `busy` cancels payment. Read the middleware before filing it.
- **Native parsers crash, not just raise.** DuckDB `read_json` segfaults on JSON nested ~21,600
  levels (a 50 KB file). Heavy jobs run in a forkserver child so a crash is one failed, unpaid
  call ([ADR 0005](docs/adr/0005-job-isolation-forkserver.md)).
- **BLAS thread pools do not survive fork.** On macOS (Accelerate) quantum kernel PCA segfaulted
  in a worker; BLAS is pinned to one thread before the forkserver starts (`engine/isolate.py`).
- **The SDK's MCP helpers target mcp 1.x.** `x402.mcp` imports the removed
  `mcp.server.fastmcp`; `payments/mcp_gate.py` implements the transport instead
  ([ADR 0002](docs/adr/0002-x402-v2-rest-middleware-and-mcp-extension.md)).
- **`sync_facilitator_on_start=False` means "never", not "later".** With that flag the SDK's
  FastAPI middleware never initializes the resource server (its lazy path is behind the same
  flag), so every paid request would fail. We keep the flag (a facilitator blip at cold start
  must not crash-loop the server) and initialize lazily ourselves in
  `Payments.ensure_initialized()`, called by `Payments.http_middleware()` before paid paths.
- **The forkserver re-imports `__main__`.** A script that triggers isolated jobs
  (`Settings(isolate_jobs=True)`, the default) without an `if __name__ == "__main__":` guard
  re-runs itself in the forkserver. Under pytest it works; in ad-hoc scripts, guard.

Found by the engine unit suite (commit 0fea70d), each with a regression test:

- **JSON fast path accepted `[]` and `[{}, {}]`** and wrote a junk column instead of rejecting
  input with no fields (`engine/readers/jsonish.py`).
- **An SPSS/Stata/SAS variable that is missing in every row** crashed the conversion with a
  500 instead of producing an all-null column (`engine/readers/statistical.py`).
- **Integer `SUM()` is HUGEINT** (decimal128(38, 0) in Arrow) and rendered as a string; it is
  now a JSON number, kept as a string only beyond 2^53 where JSON readers lose digits
  (`engine/tabular.py`).
- **The query sandbox leaked the server's cwd.** DuckDB's default spill directory is
  `<cwd>/.tmp`, which it also adds to `allowed_directories`, readable via
  `current_setting()`. Queries now spill to a private throwaway directory; connections
  without one set `temp_directory = ''` (`engine/duck.py`, `engine/tabular.py`).
- **Short cp1252/Latin-1 files were misdetected as cp437/cp1250**, giving silent mojibake.
  cp1252 is preferred when its decoding reads as Latin text (`engine/text.py`).

## Agent tooling

> Pending install: the Claude Code files below are staged in `proposed-agent-config/` for the
> owner's review (see its README) and are not active yet. Delete this note once installed.

- Skills (`.claude/skills/<name>/SKILL.md`, plain-markdown checklists any agent can follow):
  `add-input-format`, `add-paid-operation`, `release-and-deploy`, `verify-payments`.
- Reviewer subagents (`.claude/agents/`): `payments-reviewer`, `security-reviewer`,
  `quantum-honesty-reviewer`. Other agents: use their files as review checklists.
- Hooks (`.claude/settings.json`): a Bash guard that blocks secret printing, real-money spends
  and destructive commands; ruff fix + format after every Python edit.
- `.mcp.json`: this repo's own free stdio MCP server, for dogfooding.

**Real money:** any script that can spend mainnet USDC must refuse unless
`AIMART_MAINNET_CONFIRM` is the exact sentence `I_UNDERSTAND_THIS_SPENDS_REAL_USDC` (`--dry-run`
exempt). Only a human types it. An agent prepares the command and hands it to the user.
