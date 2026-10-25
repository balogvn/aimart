# Architecture

One ASGI process serves three things: the REST API (FastAPI), a remote MCP server
(Streamable HTTP at `/mcp`), and free discovery documents. REST and MCP are two front doors to
the same `Service`. Every operation has exactly one implementation, so both doors always agree
on what counts as success, and success is what triggers settlement.

```mermaid
flowchart LR
    agent(["AI agent with an x402 client"])

    subgraph app["ASGI app (api/app.py)"]
        direction TB
        mw["AccessLog → RateLimit → BodyLimit<br/>(api/middleware.py)"]
        rest["REST routes /v1/*<br/>behind the x402 HTTP middleware"]
        mcp["MCP /mcp (mcp/server.py)<br/>initialize + tools/list free<br/>tools/call via X402McpGate"]
        disc["free: /pricing /llms.txt /.well-known/*<br/>/openapi.json /v1/verify downloads"]
    end

    fac[("x402 facilitator<br/>verify and settle")]
    svc["Service (service.py)<br/>one implementation per operation"]
    src["sources.py<br/>SSRF-safe fetch, base64, artifact_id"]
    job["isolated job (engine/isolate.py)<br/>forkserver child, deadline, BLAS=1 thread<br/>convert / query / quantum"]
    store[("artifact store (artifacts.py)<br/>Local TTL disk or GCS")]
    sign["provenance.py<br/>ML-DSA-65 manifest + signature"]

    agent -- "HTTP 402 → PAYMENT-SIGNATURE" --> mw
    agent -- "tools/call + _meta x402/payment" --> mw
    mw --> rest & mcp & disc
    rest -- "step 1: verify" --> fac
    mcp -- "step 1: verify" --> fac
    rest -- "step 2: run" --> svc
    mcp -- "step 2: run" --> svc
    svc --> src
    svc --> job
    job -- "out.parquet" --> svc
    svc -- "put" --> store
    svc -- "sign manifest" --> sign
    svc -- "result + artifact ref + provenance" --> rest & mcp
    rest -- "step 3: settle only if HTTP status is below 400" --> fac
    mcp -- "step 3: settle only if isError is false" --> fac
```

## Request path

1. **Edge middleware** (pure ASGI, outermost first). `AccessLogMiddleware` assigns a request
   ID and logs whether a payment settled. `RateLimitMiddleware` runs a token bucket per client
   IP. `BodyLimitMiddleware` enforces 16 MB for JSON and MCP requests and the input cap for
   multipart uploads, while the body streams.
2. **Payment gate.**
   - REST: the x402 SDK's FastAPI middleware sits in front of the paid `/v1/*` routes only. We
     wrap it so the facilitator is initialized lazily on the first paid request (see
     [payments.md](payments.md)).
   - MCP: the x402 HTTP middleware is deliberately **not** in front of `/mcp`, because
     `initialize` and `tools/list` must stay free for discovery. `payments/mcp_gate.py`
     charges per `tools/call` instead.
3. **`Service`** (`service.py`). It materializes the input into a private work directory:
   exactly one of `url`, `content_base64`, `artifact_id` or an upload, plus `path` in local
   mode only. It then runs the operation under a capacity limiter. A full queue answers 503
   `busy`, and nothing is charged.
4. **Isolated job.** Convert, query and the quantum tools run in a child forked from a
   `forkserver` that has already imported the engine, so a job costs a fork, not a fresh
   interpreter. A crash or timeout becomes a typed `AimartError`. Inspect and preview read
   Parquet footers and slices in-process. See
   [ADR 0005](adr/0005-job-isolation-forkserver.md).
5. **Engine** (`engine/`). Detection by magic bytes (`detect.py`) is followed by bomb-safe
   decompression (`safety.py`) and UTF-8 transcoding (`text.py`). A reader (`readers/`)
   produces a raw table in a locked-down DuckDB connection (`duck.py`). Next come type
   inference in SQL (`infer.py`), name normalization (`names.py`), and the writer
   (`writer.py`): DuckDB `COPY` to zstd Parquet, or pyarrow when labels must be embedded
   ([ADR 0006](adr/0006-duckdb-copy-writer.md)).
6. **Artifact store.** The output goes to `LocalArtifactStore` (TTL disk, HMAC-signed links)
   or `GcsArtifactStore` (V4 signed URLs). The caller gets a reference (`artifact_id` and
   `download_url`), not the bytes ([ADR 0007](adr/0007-artifact-references-not-bytes.md)).
   At the TTL an artifact becomes unreachable; a sweep on the request path deletes it soon
   after ([privacy.md](privacy.md) has the retention promise and its worst case).
7. **Provenance.** `Service._publish`, the single place every output passes through, signs a
   manifest with ML-DSA-65 ([ADR 0009](adr/0009-post-quantum-provenance.md)).
8. **Settlement.** REST settles only after a response below 400. MCP settles only for a
   result with `isError=false`. If settlement itself fails, the result is withheld.

## Single sources of truth

| question | the one answer |
|---|---|
| what costs what, at which route or tool | `payments/pricing.py` `OPERATIONS`, `active_operations()` |
| what this deployment serves (quantum extra installed and `AIMART_ENABLE_QUANTUM=true`?) | `pricing.active_operations(settings)` |
| the configuration | `config.Settings` (`AIMART_*` environment variables) |
| how an operation behaves | the `Service` method used by both REST and MCP |
| the public contract (fields agents read) | `engine/models.py`, `quantum/models.py` |
| discovery documents | `api/discovery.py`, generated from `OPERATIONS` and `Settings` |

## Local mode

`aimart-parquet mcp` builds the same `Service` with `local_mode=True` and no payments. Tools
accept `path` and `output_path`, outputs are written next to the input, and nothing is signed.
If `AIMART_HOSTED_URL` is set, convert results carry a one-line `hint` pointing at the hosted
service.

## Code map

The code map, with what each module is for, is in [AGENTS.md](../AGENTS.md#code-map).
Decision records are in [adr/](adr/README.md).
