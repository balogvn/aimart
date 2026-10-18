# Discovery: how agents find us

The customers are programs, so discovery documents do the marketing. Each document below is
generated from the same pricing table the paywall uses (`payments/pricing.py`), so what is
advertised is what is charged. Directory behaviour described here was checked in
October 2026 and changes often. Re-check it before relying on it.

> The hosted URL is not live yet. Everything below that says `https://YOUR-SERVICE.run.app`
> is a placeholder, including `remotes[0].url` in [`server.json`](../server.json).
> **`release.yml` must write the real URL** (and the release version) into `server.json`
> before publishing, or the registry will advertise a dead endpoint.

## What we serve (free)

| path | read by | generated from |
|---|---|---|
| `GET /.well-known/x402` | x402scan and x402 crawlers | `Payments.discovery_document()` |
| `GET /openapi.json` (`x-payment-info` on each paid operation) | x402scan, agents | `api/app.py` `paid()` |
| `GET /.well-known/mcp/server-card.json` | Smithery and other MCP scanners | `discovery.mcp_server_card()` |
| `GET /llms.txt` | LLM agents, crawlers | `discovery.llms_txt()` |
| `GET /pricing` | agents, `scripts/smoke.py` | `discovery.pricing()` |
| `/mcp` `initialize` and `tools/list` | every MCP client and directory | `mcp/server.py` |
| `extensions.bazaar` in every 402 | facilitator catalogs (Bazaar) | `payments/bazaar.py` `EXAMPLES` |

Some fields matter for discovery even though they do nothing for function: `extensions.bazaar`,
`x-payment-info`, `resource.url`, `serviceName` (at most 32 ASCII characters), at most 5
`tags`, `accepts[].extra.tag` (from `AIMART_BAZAAR_TAG`), and the server-card fields.
Developers who reason only from function tend to drop such fields, so each one needs a test
that fails when it goes missing.

## Channels

### 1. Official MCP Registry (registry.modelcontextprotocol.io)

This is the root listing: several directories ingest it.

- [`server.json`](../server.json) in the repo root. It validates against
  `https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json`. The name is
  `io.github.balogvn/aimart-parquet`. It lists the PyPI package (`uvx aimart-parquet mcp`,
  stdio) and the remote (`streamable-http`). x402 prices go under
  `_meta["io.modelcontextprotocol.registry/publisher-provided"]`, which must stay under 4 KB.
- **Ownership proof for PyPI:** the registry checks that the PyPI package description contains
  `<!-- mcp-name: io.github.balogvn/aimart-parquet -->`. That line is in the README, which is
  the package's long description. Do not remove it.
- **Publishing:** in `release.yml`, after the PyPI upload succeeds, run `mcp-publisher` with
  GitHub OIDC (`mcp-publisher login github-oidc`, then `mcp-publisher publish`). This needs
  `permissions: id-token: write`, and the workflow must run in the `balogvn` account, because
  the `io.github.balogvn/*` namespace is proven by GitHub identity. No token is stored.
- Bump `version` in `server.json` (both the top-level field and `packages[0].version`) on every
  release. The registry rejects a republish of an existing version.

### 2. Smithery

List the server by URL (`https://YOUR-SERVICE.run.app/mcp`). When Smithery cannot fully
introspect a server, it reads the static card at `/.well-known/mcp/server-card.json`, which
carries the tools and their prices.

### 3. Glama

Glama ingests the official registry, so publishing there is enough. Check the listing after
the first release and claim it if needed.

### 4. mcp.so

Submit manually: the repository URL plus the remote URL.

### 5. PulseMCP

Manual submissions were paused when last checked. PulseMCP ingests the official registry, so
it should pick the server up from there.

### 6. x402scan

Register the origin on x402scan. It reads `x-payment-info` from `/openapi.json` and then
`/.well-known/x402`. After that it probes the listed resources for live 402s, so the
deployment must answer each one with a valid `PAYMENT-REQUIRED`. `make smoke` checks exactly
that.

### 7. Facilitator catalogs (Bazaar)

Facilitators that settle our payments can list the resources in their catalogs, using the
`bazaar` extension in our 402s (input example, input schema, output example). Catalogs
validate the example against the schema strictly and **silently skip** entries that fail. That
is why the schemas in `payments/bazaar.py` are self-contained (no `$ref`) and a test validates
them.

- **Dexter** and **PayAI** expose catalogs at `/discovery/resources`.
- **CDP Bazaar** only lists resources settled through Coinbase's CDP facilitator, which needs
  a CDP API key. The first 1,000 transactions each month are free, then $0.001 per
  transaction. That fee is half the margin of a $0.002 call, so it is not in the defaults.
  See [economics.md](economics.md).

### 8. The free local funnel

`uvx aimart-parquet mcp` is free and open source, and it is listed in the registry next to the
remote. When `AIMART_HOSTED_URL` is set, local convert results carry a one-line `hint`
pointing at the hosted service (no install, works from any agent platform, signed
provenance). This is the "free local, paid hosted" funnel from the spec. The hint appears once
per result and never blocks anything.

### 9. Let real traffic decide the roadmap: demand signals

Two free, zero-cost signals tell you what agents tried to buy that you do not sell yet:

- **Unsupported formats.** Every conversion rejected for its file type logs the *extension*
  only (never the content or full name).
- **`request_capability`** (MCP tool) / **`POST /v1/feedback`** (REST). Its description invites
  any agent to say what it needed, the formats involved and the most it would pay per call.

Read them from the logs (Cloud Logging keeps them under its free tier):

```bash
gcloud logging read 'jsonPayload.msg="demand_signal"' --limit 100 --format json
```

or, with `AIMART_ADMIN_TOKEN` set, `GET /v1/demand` with `Authorization: Bearer <token>` for
the counters since the last restart. After a few weeks live, build the most requested
format or tool next.

### 10. Future work

- Tool wrappers for **LangChain** and **CrewAI** that pay through x402 (an x402 httpx
  transport under a `StructuredTool`), published as a small separate package.
- Listing in agent marketplaces as their x402 support matures.

## Launch checklist

1. Deploy, then run `make smoke URL=… SMOKE_ARGS="--expect-pay-to … --expect-network … --require-provenance"`.
2. Set the real remote URL in `server.json`, publish to PyPI, then publish to the registry
   (`release.yml`).
3. Register on x402scan, list on Smithery, submit to mcp.so.
4. A day later, confirm the Glama and PulseMCP listings, and that facilitator catalogs show
   the resources. Read them as a client would, and paginate to the reported total. A
   truncated catalog page is not proof that you are absent.
