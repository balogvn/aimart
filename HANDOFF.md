# AImart: Parquet MCP — build handoff

Status: **design done, no code written yet.** Work stopped at the usage limit on 2026-10-11.

## What we're building (from the Google AI Mode conversation, 8 Oct 2026)

An MCP server and REST API that AI agents pay for per call with x402 (USDC). It turns messy
files into clean, schema-validated, token-efficient Parquet:

- Inputs: CSV/TSV, JSON/NDJSON (nested or broken), Excel, and SPSS/Stata/SAS with value and
  variable labels kept. The SPSS/Stata/SAS support is what sets it apart from existing tools.
- Tools:
  - `convert_to_parquet` ($0.01)
  - `inspect_parquet` (metadata only, $0.001)
  - `preview_parquet` (slice back to JSON/Markdown/CSV, $0.001)
  - `query_parquet` (read-only, sandboxed DuckDB SQL, $0.005)
- Goal: zero running cost, best-in-class CI/CD, current agentic-engineering practice.

## Versions checked live on 2026-10-11

| Package | Version | Package | Version |
|---|---|---|---|
| x402 (PyPI) | 2.25.0 | @x402/* (npm) | 2.28.0 (incl. @x402/mcp) |
| mcp (PyPI) | 2.3.0 (protocol 2026-07-28) | fastmcp | 4.1.0 |
| pyarrow | 26.0.0 | duckdb | 1.5.6 |
| polars | 2.0.0 | pyreadstat | 1.3.6 |
| python-calamine | 0.8.3 | json-repair | 0.64.0 |
| fastapi | 0.143.0 | starlette | 1.7.0 |
| uv | 0.13.0 | ruff | 0.17.0 |
| mypy | 2.4.0 | pytest | 9.1.1 |
| hypothesis | 6.169.0 | mutmut | 3.8.0 |
| schemathesis | 4.30.1 | | |

Unused names on PyPI: `aimart-parquet`, `parquet-mcp`. Planned: distribution `aimart-parquet`,
import name `aimart_parquet`, MCP registry name `io.github.balogvn/aimart-parquet`.

## Findings from reading the SDK source

- **x402 FastAPI middleware** (`x402.http.middleware.fastapi.payment_middleware`) checks the
  payment, runs the handler, and settles only if the status is below 400. If the handler throws
  or returns 4xx/5xx, the payment is cancelled. It buffers the whole response body before
  settling.
- At startup the middleware calls the facilitator's `/supported` endpoint. Tests must inject a
  fake facilitator client.
- The Python SDK supports EVM (Base) and SVM (Solana) payment networks. It has **no
  Algorand/AVM mechanism**, so the GoPlausible setup from `x402/` does not port to Python.
- **The x402 SDK's own MCP server wrapper (`x402.mcp.server`) is broken with mcp 2.x.** It
  imports `mcp.server.fastmcp`, which mcp 2.x removed (`FastMCP` became
  `mcp.server.mcpserver.MCPServer`).
- Planned fix: keep mcp 2.x and write a thin bridge that copies the wire format of
  `x402/mcp/server.py`:
  - **Request:** the payment arrives in `_meta["x402/payment"]`.
  - **Unpaid call:** return `CallToolResult(isError=True, structuredContent={x402Version: 2,
    accepts, error, resource, extensions})`.
  - **Successful call:** put the settle result in `_meta["x402/payment-response"]`.
  - **Test:** call the bridge with x402's official MCP client.

## Architecture decisions

- Python 3.12+, managed with uv. One FastAPI app serves the REST API, the paid
  `/mcp` endpoint (MCPServer over Streamable HTTP) and the discovery files (`/.well-known/x402`,
  `/llms.txt`, OpenAPI, `server.json`).
- Local stdio mode is free, open-source, and accepts file paths. Remote mode is paid and must
  **never** accept file paths.
- Results are passed by reference. `convert` returns an `artifact_id` plus a signed, expiring
  download link (ephemeral disk with a TTL), not base64 Parquet that would fill the agent's
  context. Later tools accept `artifact_id` or `url`.
- The server holds **only payTo addresses, never private keys**. It starts on testnet (Base
  Sepolia via the x402.org facilitator). It refuses to start in mainnet mode without valid payTo
  addresses.
- Security:
  - URL fetching is SSRF-safe: pin the resolved IP, re-check on every redirect, cap the size.
  - Guard against decompression bombs.
  - Lock down DuckDB: `enable_external_access=false`, `lock_configuration`, a memory limit,
    SELECT statements only, and an interrupt-based timeout.
  - A concurrency semaphore returns 503 when busy (the payment is not settled).
- Hosting: planned primary is a Hugging Face Spaces Docker space on the free CPU tier
  (2 vCPU / 16 GB), with the image kept portable. **Not yet confirmed:** the research run was
  stopped before checking HF's terms for commercial use and comparing alternatives.
- Facilitator for mainnet (zero cost) and how Bazaar listing works: **not yet confirmed**
  (candidates: PayAI, CDP, x402.org).

## Lessons from `../Antigravity/x402` to apply

- Write tests against what the client actually receives: decode the base64 `PAYMENT-REQUIRED`
  header, don't read our own config back.
- Mutation-test the payment and security modules.
- Get an adversarial review before shipping payment code.
- After deploy, the smoke test checks real values (price and payTo in the decoded 402).
- Bazaar `accepts[].extra` needs both `asset` and `tag`.

## Next steps

1. Re-run research for the unchecked items (hosting terms, facilitators, CI action SHAs). The
   stopped run's script can be resumed with
   `resumeFromRunId: wf_a3848f20-42c`.
2. Scaffold the repo: pyproject, the package layout above, Makefile, AGENTS.md + CLAUDE.md,
   `.claude/` (settings, hooks, skills, reviewer subagents), `.mcp.json`.
3. Build the engine, then payments and the MCP bridge, then the API and discovery files.
4. Tests: golden fixtures, hypothesis, wire-level payment tests, schemathesis.
5. GitHub Actions, with every action pinned to a commit SHA:
   - `ci`: lint, type-check, tests, coverage, docker build + Trivy scan, zizmor, gitleaks,
     dependency review.
   - Separate workflows: CodeQL, OpenSSF Scorecard, release-please.
   - Publishing: PyPI trusted publishing, GHCR image with provenance + SBOM, MCP registry.
   - Deployment: deploy to HF, then smoke-test, plus a scheduled synthetic check.
   - Dependabot.
6. Ask the user before creating the GitHub repo, pushing, or deploying. These are public
   actions, and the HF token and wallet address must come from the user.
