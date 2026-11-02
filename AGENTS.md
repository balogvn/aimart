# AGENTS.md

Guide for any AI coding agent (and human) working in this repo. Claude Code reads it through
`CLAUDE.md` (staged in `proposed-agent-config/` until the owner installs it; see "Agent
tooling"); other agents read it directly. Keep it short enough to read every session and
accurate enough to act on: if you change a command, path or rule below, change it here too.

## What this is

**AImart Parquet** (`aimart-parquet` on PyPI, import `aimart_parquet`): a pay-per-call service
that AI agents use to turn messy CSV/TSV, JSON/NDJSON, Excel (xlsx/xls/ods), SPSS (sav/zsav/por),
Stata (dta) and SAS (sas7bdat/xpt) files, and tables in digital (not scanned) PDFs, into clean,
typed, zstd Parquet, then inspect (footer only), preview, and query them with sandboxed
read-only DuckDB SQL (joins across up to 8 artifacts per query). Every hosted artifact carries an
ML-DSA-65 (post-quantum) provenance signature. An optional premium tier, **off by default**
(`AIMART_ENABLE_QUANTUM=true` plus the `quantum` extra), adds simulated-quantum kernel PCA and
QAOA portfolio selection, each with a classical baseline; treat it as opt-in everywhere (docs,
discovery, smoke checks), not as part of the default product.

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
  Two decompression caps: `max_decompressed_bytes` (256 MB, a gzip/bz2/zip stream, counted as
  it inflates) and `max_workbook_xml_bytes` (512 MB, all the XML parts of an xlsx/ods, every
  sheet counted though one is parsed); `engine/limits.py`'s `Limits` defaults equal them.
- `errors.py`: `AimartError` subclasses, each with `code`, `http_status` (always >= 400) and a
  concrete `hint`. Raising one is what guarantees the caller is not charged. `InternalError.of()`
  is the one 500 for an unexpected exception (the isolated child, REST's
  `InternalErrorMiddleware` and MCP's `_guard`): its type only, hint "Retry; if it persists,
  report it. You were not charged.". `without_paths()` strips server paths from any message
  built from a library's exception text.
- `engine/`: the pure conversion engine. `detect.py` (magic bytes over extensions; a `%PDF-`
  header anywhere in the first 1 KB), `safety.py` (gzip/zip bomb caps), `text.py` (UTF-8
  transcoding), `duck.py` (sandboxed DuckDB; `NOWHERE` for every `$HOME`-derived setting),
  `readers/` (delimited, jsonish, excel, statistical, columnar, pdf; the `READERS` map is in
  `convert.py`), `infer.py` (type ladder in SQL; the number convention is decided per column,
  then by the file's other number columns, then by the dialect; text with a zone only ever
  becomes TIMESTAMPTZ; constant lists go through `_one_of`, never `IN`), `names.py`,
  `convert.py` (pipeline, `expected_schema`, report; CSV/TSV jobs run DuckDB on one thread),
  `writer.py` (DuckDB `COPY` by default, pyarrow when labels exist), `inspect.py` (footer-only
  stats), `tabular.py` (preview and SELECT-only query: inputs are views over DuckDB's own
  Parquet reader (`_native_view`), long cells are cut inside DuckDB, rows are built by
  position, results over `MAX_RESULT_BYTES` are refused), `isolate.py` (forkserver child per
  job: deadline, a watchdog on RSS plus the job's temporary files (its temp directory and the
  `job_dirs` the service passes) for `job_memory_limit_mb`, a per-job temp directory the
  parent deletes; a crash is logged by type and frames, never its text), `limits.py` (per-job
  limits; under the watchdog DuckDB's memory limit is the cap minus 300 MB and its spill cap
  the rest of the cap, `within_job_cap`), `models.py` (the pydantic contract agents read).
  Delimited text is staged once as zstd Parquet and `raw` is a view over it. DuckDB running out
  of memory is a 413 (`read_csv` in `delimited.py`; any other step in `convert_file`), never a
  500 that says retry.
- `quantum/`: premium tools (`kernel_pca.py`, `portfolio.py`, `data.py`, `models.py`). Served only
  when the `quantum` extra is installed AND `AIMART_ENABLE_QUANTUM=true` (off by default; a buyer
  panel valued them at $0). `pricing.active_operations(settings)` decides. Spec:
  `docs/specs/quantum-premium.md`.
- `demand.py`: demand signals. Unsupported file extensions are counted, and agents can describe
  unmet needs via the free `request_capability` tool / `POST /v1/feedback`. Logged as
  `demand_signal`; `GET /v1/demand` behind `AIMART_ADMIN_TOKEN`. This decides the roadmap.
- `provenance.py`: ML-DSA-65 manifests and signatures for every hosted artifact.
- `sources.py`: SSRF-safe URL fetch (validated IPs at connect time, re-checked redirects, port
  allow-list, size cap; spaces, controls and non-ASCII in a path, query or Location
  percent-encoded, since h11 takes only 0x21-0x7E in a request target; `downloads_deadline`,
  opened by `Service._job`, gives all of a call's URL downloads one `fetch_timeout_s`),
  `check_base64` (exact: the standard or URL-safe alphabet, whitespace ignored, padding
  completed, a `data:` URL only with a `base64` parameter, every other character, or an empty
  file, refused before the quote), `safe_filename`.
- `artifacts.py`: `LocalArtifactStore` (TTL disk, HMAC-signed links, `delete()`, and a
  request-path sweep at most once a minute) and `GcsArtifactStore` (also deletes expired blobs;
  a bucket-listing sweep at most every 6 h catches blobs a restart orphaned, and the bucket's
  1-day lifecycle rule is the last resort). The TTL makes an artifact unreachable; the sweeps
  delete it. `discovery.retention_policy()` publishes exactly that, so change both together.
  The budget: `put()` makes room before storing and refuses instead (413 over the whole budget,
  503 `busy` when room needs a protected artifact); `track_puts`/`outputs_of` keep a paid call's
  outputs provisional until `commit()` or `discard_withheld()`; `hold()` pins what a running
  call reads. A local output lives until its `expires_at`: capacity evicts only expired ones,
  and a `busy` refusal's hint (and `details.retry_after_s`) says when enough will have expired
  (`MIN_RESIDENCY_S` is gone). On GCS eviction drops only the cache.
- `service.py`: `Service`, the **one** implementation of every operation, used by REST and MCP.
  `_job()` holds a job slot and `store.hold()` from before any input is fetched; all URL and
  base64 inputs of a call share one `max_input_bytes` budget; a queued call gives up when
  its payment window would not fit the wait (`Settings.queue_wait_for`: the 300 s window minus
  60 s to settle, `fetch_timeout_s` and the operation's timeout; or `queue_wait_s` when set);
  `convert_upload` copies an upload into the convert's own work directory, inside
  the slot (an empty one is refused); `_execute` runs every engine call (inspect and preview
  too) in the child, its work directory counted toward the cap. `query` takes a map of table
  names (`data` + up to 7 joined artifacts) for one query price. Before the quote:
  `check_input`, `check_parquet_input` (inline bytes that cannot be Parquet, for inspect and
  preview), `check_query` (with `check_tables`, which `query` runs too), `check_convert` (with
  `check_options`: `pdf_pages`, `encoding` or `delimiter` the reader of the format the request
  names can never use, as `ConvertOptions.refusal`; any other format ignores them).
- `payments/`: `pricing.py` (`OPERATIONS`, the single source of prices, routes and tool names;
  `usd_to_atomic`), `__init__.py` (`Payments`: x402 resource server, REST route configs with the
  Bazaar extension, lazy facilitator init, price-floor check; `http_middleware(prevalidate=,
  store=)` commits or deletes a paid call's outputs from its final status), `mcp_gate.py` (x402
  for MCP tool calls, same commit or delete), `bazaar.py` (catalog examples).
- `mcp/server.py`: `build_mcp(...)`, the tools and their descriptions (the whole interface an
  agent sees). Remote: `convert_to_parquet` takes `url`/`content_base64`/`artifact_id`, every
  other tool `url`/`artifact_id`. Local: convert takes `path`/`url`/`content_base64`, the others
  `path`/`url`. A refusal's hint names only the calling tool's inputs (`SourceRef.takes`).
  `argument_checker` validates tool arguments before the gate quotes a price, and refuses an
  argument the tool does not declare (the SDK would drop it silently), except `filename`, which
  only names the input.
- `api/`: `app.py` (`create_app`; routes, OpenAPI `x-payment-info`, middleware order;
  `_body_check` and `_form_check` refuse a request that can only fail before the 402;
  `InternalErrorMiddleware`, inside the body limit and outside the x402 middleware, makes any
  other exception the typed 500, never settled), `middleware.py` (access log, rate limit, body
  limit, `FormPeekMiddleware`: reads up to 1 MiB of an upload form before the payment
  middleware, and a field still open there to its end, applying Starlette's form limits (1 MiB a
  field, 1000 fields, 1000 files) and noting empty file parts, then replays it unchanged),
  `discovery.py`
  (`/llms.txt`, server card, pricing, `retention_policy()`: the one data-retention statement),
  `schemas.py`.
- `cli.py`: `serve`, `mcp`, `convert`, `inspect`.

Outside `src/`: `tests/` (`aimart_testkit.py` helpers, `unit/`, `integration/`, `quantum/`),
`scripts/smoke.py` (live 402 decoder), `Dockerfile`, `.github/workflows/` (`ci.yml`,
`codeql.yml`, `release.yml`, `deploy.yml`, `synthetic.yml`, `mutation.yml`, `scorecard.yml`,
`claude-review.yml`), `deploy/` (Cloud Run: `setup-gcp.sh`, `smoke-flags.sh` shared by the
deploy and synthetic smoke steps), `server.json` (MCP Registry manifest; prices and tools there
must match `OPERATIONS`), `docs/examples/agent_pays.py` (paying client example). `deploy.yml`
overwrites the service's env vars and secrets on every deploy: a runtime setting it does not
pass (from a GitHub variable) does not exist in production.

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
7. **Never quote what can only fail.** A paid request with an invalid body, form or tool
   arguments, or an unserved tool, is refused (400 / `isError` / free "Unknown tool") before
   any 402 or PaymentRequired. An empty or `{}` unpaid body, and an unpaid form without its
   file, stay price probes (catalogs replay them).
8. **An output nobody paid for does not stay.** Outputs stored during a paid call are
   provisional until settlement: committed when settled, deleted otherwise (error status,
   `isError`, failed settle, exception, or cancellation, which is a `BaseException`).

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
- **Isolated-job tests assert the error type, not wall-clock time** (the forkserver's first
  start took over 15 s under load); memory-cap tests keep touching their allocation (macOS pages
  idle memory out before the poll sees it). Never give DuckDB ~1,500-level JSON in-process.
- **A test that encoded the bug is updated, never deleted or loosened**, with a comment saying
  what it asserted before and why that changed. Some such values look arbitrary and are not:
  `unit/test_convert.py::TestShaping::test_row_group_size` needs 5000 rows, not 2500, because
  without the `IN` join DuckDB's order-preserving COPY writes a final batch under about 3000
  rows as one group; the spill caps in `unit/test_fix_csv_memory.py` are the rest of the job
  cap, not the cap.
- A streamed paid upload needs `io.BytesIO` through the plain client with an explicit payment
  header (ASGITransport and x402's transport send one message). The shared fake GCS client is
  `tests/fixtures/money/fake_gcs.py`.
- No real network in default tests (`allow_private_urls=True` only to fetch from a local test
  server). Mark anything else `network` or `testnet`.

## Security rules

- **Remote callers never name server paths.** `path` inputs exist only in local stdio mode;
  `output_path` likewise. Every caller-supplied filename goes through `safe_filename`.
- **SSRF guard stays at connect time** (`sources.GuardedBackend`): resolve, check every address
  with `ip_is_public`, connect to the checked address; re-check every redirect hop.
  `allow_private_urls` is for tests only.
- **DuckDB lockdown on every connection** (`engine/duck.connect`): `allowed_directories` limited
  to the job's own directories, `enable_external_access=false`, no extension autoload, no
  setting that names a host path (SQL can read every setting), then `lock_configuration=true`
  last. The query path runs exactly one SELECT with a row cap and an interrupt at the deadline;
  its inputs are hard links (or copies) in its own spill directory, the only one it may read.
- **Decompression is streamed against caps**; zips are checked from the central directory
  before inflating. Every parse of a caller's file, caller Parquet included (inspect, preview,
  query), runs in the forkserver child with a deadline and a memory cap. The cap is the parent's
  watchdog on RSS plus the job's files (on Cloud Run `/tmp` is RAM): never
  `RLIMIT_AS`/`RLIMIT_DATA`, as a Linux child starts at about 2 GB virtual. A child's unexpected
  exception is logged by type and frames only (never its text: `retention_policy()` says logs
  hold no file contents); the caller sees only its type.
- **No server path in a reply.** Every caller-visible message built from exception text goes
  through `errors.without_paths`.
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
- **Text that promised more than the code did.** The published retention said "deleted 60
  minutes after creation" while the code only made artifacts unreachable; the README's decoded
  402 kept a `$0.01` amount after convert moved to `$0.02`; discovery summaries never mentioned
  PDFs or joins. Docs, samples and discovery text are interface: `tests/*/test_fix_docs_*.py`
  compare them with the wire and the code, so update both in one change.
- **Two callers of one script, one input short.** The synthetic smoke ran `smoke-flags.sh`
  without `AIMART_ENABLE_QUANTUM`, so it never required the quantum tools the deploy smoke did.
  A test now checks every workflow that calls it passes every input it reads.

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

Found by profiling CSV memory (a 200 MB CSV peaked at the 700 MB job cap; all of it DuckDB),
each pinned in `tests/unit/test_fix_csv_memory.py`:

- **The whole CSV as a VARCHAR table**, read through 256 MB of buffers (DuckDB's default is
  16 x `max_line_size`). Delimited text is now staged as zstd Parquet and `raw` is a view.
- **`IN (...)` with 5+ constants is a hash join** in DuckDB, one per occurrence: ~600 MB for a
  40-column file's null-token lists (the same join as in "No `IN (...)`" below). Use
  `infer._one_of`.
- **An ordered COPY on 2 threads holds every batch a faster thread finished early** (400-650 MB
  for 40 columns). CSV jobs run DuckDB on one thread; production has one vCPU anyway.
- **DuckDB's 1 GB limit sat above the 700 MB cap**, so a sort was killed before it could spill.
  `Limits.from_settings` holds DuckDB at the cap minus 300 MB and its spill files at the rest
  of the cap: the watchdog counts spill files with the job's memory (they are RAM on Cloud
  Run), so the two together never pass it.

## Bug classes guarded since the review of 6f99145

Each was a 200 for wrong data, a charge for nothing, or a bare 500. Paths are under `tests/`;
each test's docstring has the incident.

- **Charged for a link that dies, or for nothing.** An output over the whole artifact budget is
  a 413 before it is stored; eviction spares unsettled outputs, unexpired local ones and what a
  running call reads (`hold`; on GCS a join's second table evicted its first), and when room
  needs one of those, `put()` is a 503 `busy` that evicts nothing. Never add an eviction path that ignores `_provisional` or `_pins`. Withheld results
  delete their outputs (invariant 8). (`unit/test_fix_money_artifacts.py`,
  `{unit,integration}/test_fix_money_capacity.py`, `integration/test_fix_money_withheld.py`)
- **Quoting what cannot succeed** (invariant 7), and **capacity spent before the job slot** (the
  slot is taken before any download or upload copy; one input budget per call). Refused before
  the quote: undecodable or empty `content_base64`, inline bytes that cannot be Parquet sent to
  inspect, preview or query, URLs that can never be fetched, short SQL that is not one SELECT,
  forms past Starlette's limits or with an empty file part, MCP arguments a tool does not
  declare (but `filename`), an option value the named format's reader can never use. (`integration/test_fix_money_gate.py`, `integration/test_fix_money_upload_check.py`,
  `unit/test_fix_money_form_peek.py`, `unit/test_fix_money_service.py`,
  `integration/test_final_validate_*.py`, `integration/test_final_integrate_empty_upload.py`,
  `integration/test_final_integrate_mcp_unknown_args.py`)
- **Host paths readable from SQL** through DuckDB settings. (`unit/test_fix_money_duck.py`)
- **Numbers in the wrong convention** ('1.5' as 15 in an EU column, '1.234' as 1.234 in a
  semicolon file). Casts are gated on the style's own pattern (`_style_fit`; never `TRY_CAST` an
  unmatched value: DuckDB reads '1_000' as 1000), the convention is decided per column
  (`TypeInferrer._text_style`), then by the file's other number columns
  (`TypeInferrer._decimal_comma`: '398.53' in a semicolon file is a decimal point, not 398,530),
  then by the dialect, and every ambiguous choice adds a note. (`unit/test_fix_infer_numbers.py`,
  `unit/test_roundtrip_property.py`, `unit/test_final_data_decimals.py`)
- **Offsets dropped, zones carried over.** DuckDB's TIMESTAMP cast drops offsets; its
  TIMESTAMPTZ cast carries a zone name ('09:00 EST') to later naive values in the same 2048-row
  vector. Zoned text is read only as TIMESTAMPTZ, in CASE branches no naive row takes
  (`_has_zone`, `_zoned_ts`, `_instant`). A time past year 9999 is an unknown range in inspect
  and a 422 in preview and query. (`unit/test_fix_infer_timestamps.py`, `unit/test_inspect.py`)
- **Zones misread by ICU.** ICU resolves an abbreviation through legacy Java IDs: 'BST' is
  Asia/Dhaka, 'MST' Phoenix (summer time in 1967), and a UK log came back with every summer
  instant five hours early, a 200 with no note. A zone is only an
  offset or `Z`, UTC with whole hours, an IANA name (US POSIX forms such as `EST5EDT`
  included), or a `ZONE_ABBREVIATIONS` entry (GMT, EST, MST, HST, JST), rewritten to its offset
  before the cast. BST, IST, CST, PST, CET and the like are counted rejects, or the column stays
  text, with the `unread_zone_note`. (`unit/test_final_data_zones.py`)
- **No `IN (...)` over constants in generated SQL.** DuckDB plans `IN` over five or more
  constants as a MARK hash join: 6-12 MB each, so a 100-row CSV with 30 date columns ran out of
  memory (a 500), and past one 122,880-row group rows were written out of source order. Use
  `infer._one_of` (`list_contains`). (`unit/test_final_data_wide_and_order.py`, whose EXPLAIN
  test fails on any join in a column expression)
- **`expected_schema` rewriting values.** Scale 0 is not "unset" (check `is None`), a decimal
  never rounds and a bool is only 0 or 1 (else a counted reject), a column declared string keeps
  its text, and `strict` counts values inference nulled. (`unit/test_fix_infer_schema.py`)
- **Readers losing values.** Nested keys that differ only by case (DuckDB matches STRUCT fields
  ignoring ASCII case and silently dropped every `K`) are renamed (`K_2`) with a warning, only
  where DuckDB reads a STRUCT: MAP keys and JSON text are data. `read_csv` skips exactly the blank
  lines the sniffer skipped. (`unit/test_fix_readers_json.py`,
  `integration/test_fix_readers_json_api.py`, `unit/test_fix_readers_delimited.py`)
- **Results and inputs outside DuckDB's `memory_limit`.** Rows are built by position, never
  through `to_pylist()` dicts (joins repeat names); long cells are cut inside DuckDB at any depth;
  results are read 256 rows at a time and refused over 64 MiB; preview batches are sized from the
  footer. Query inputs are `read_parquet` views checked against the Arrow scan's names and types
  (`_native_view`, `_widens`). The Arrow scan decodes whole row groups (3.3 GB for `count(*)` on
  a 260-byte dictionary bomb) and is kept only for INT96, decimals over 38 digits (DuckDB reads
  them as a wrong DOUBLE), UUID, INTERVAL and column chunks over 64 MiB. A non-Parquet table is a
  422. (`unit/test_fix_readers_tabular.py`, `unit/test_fix_readers_preview.py`,
  `integration/test_fix_readers_query.py`)
- **Hostile PDFs.** Glyphs and shapes are budgeted (100k a page, 2M a document), and so is the
  table search (it grows with the square of a page's line crossings); reference chains stop at
  32 hops (`PDFObjRef.resolve`, patched at import); metadata and page labels are never resolved;
  `pdf_pages` is clamped to the page count before iterating. (`unit/test_fix_pdf_reader.py`)
- **PDF tables read wrong.** The header is the row naming the most typed columns (the topmost of
  equals: "widest row" and "narrower than the body" both shipped bugs), and never a data row: a
  first row written like the body is data, the columns are `column_1`.. and a warning says to
  pass `header=true`. Lines above it are dropped and reported (a running title once a page,
  each line shown once); continuations are merged before the usable-table filter; a BOM or junk
  before `%PDF-` is still a PDF. A line atop a continuation page is dropped as a running title
  only on evidence that it is this table's: it says 'continued', it repeats the line dropped
  above page 1, or it names its page under a header row. It is body when it is a pending row or
  a section row (text in at most half the cells; see `_continuation`). A line that may be a new
  table's header (it names every typed column or fills more than half the cells) is a running
  title only when it repeats the line dropped above page 1: page labels ('P02', 'P5') and
  'Cont.' inside a header are column names. Anything else splits the page off ('found N
  tables'): a split is a reported partial result, a different table merged in is silent wrong
  data. The last one or two rows of a borderless table, alone on its last page, are read at the
  previous page's column edges and kept only when they look like body.
  (`unit/test_fix_pdf_reader.py`, `unit/test_fix_pdf_detect.py`, `unit/test_final_data_pdf.py`,
  `unit/test_final_data_pdf_next_page.py`, `unit/test_final_data_pdf_page_tops.py`,
  `unit/test_final_data_pdf_header_lines.py`)
- **One job taking the instance down** (a 1.6 KB PDF reached 2 GB in 15 s): the RSS watchdog,
  the per-job temp directory (it holds a query's linked inputs: RAM on Cloud Run), and crash text
  kept out of responses. (`integration/test_isolate.py`) The cap counts the job's temporary
  files: a 1.5 MB gzip CSV wrote 500 MB into the work directory while RSS alone stayed under it.
  (`integration/test_final_isolate_disk.py`, `integration/test_final_integrate_stream_cap.py`)

Known gaps: ISO date formats accept 1-digit years ('1/2/3' becomes 0001-02-03); JSON timestamps
that all carry offsets become naive UTC with no note. In PDFs, a different table after a
table's last row on a continuation page is appended to it (strict xfail
`unit/test_final_data_pdf_next_page.py::test_a_different_table_after_the_last_row_is_not_appended`:
its only sign, a row naming every typed column, is also what a first suppressed row such as
'Area5', 'x', 'x' looks like), and a short one-cell title above a numeric header printed atop
every page is taken as the header, the numeric header then a data row on each page (123 rows
for 120; at 07fa450 too). (The 422 for a time past 9999 in preview and query and the 413 for
`read_csv` running out of memory are tested in `integration/test_final_isolate_gaps.py`;
DuckDB's `memory_limit` no longer sits above the job cap, `unit/test_fix_csv_memory.py`.)

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
