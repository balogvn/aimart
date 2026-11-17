# Discovery: how agents find us

The customers are programs, so discovery documents do the marketing. Each document below is
generated from the same pricing table the paywall uses (`payments/pricing.py`), so what is
advertised is what is charged. Directory behaviour described here was checked on
2026-11-16 and changes often. Re-check it before relying on it.

## Where the service is listed

The hosted service is `https://balogvn--aimart-parquet.us-east.modal.run` (MCP at `/mcp`).
Status on 2026-11-16:

| where | status | how it got there, or what is left |
|---|---|---|
| Official MCP Registry, `io.github.balogvn/aimart-parquet` | listed (0.1.1, latest) | `release.yml` publishes every release (section 1) |
| Glama | listed: a connector page (healthy, tool-definition score A) and a server page from the repository | copied from the registry; claiming it needs the owner's Glama sign-in |
| x402scan | registered, 9 routes (5 paid) | the origin, entered once at <https://www.x402scan.com/resources/register> (section 3) |
| Agent402 | registered, routable, 9 tools | one unpaid request to its register endpoint (section 3) |
| 402 Index | the 5 paid routes submitted, pending review | one request per route (section 3) |
| Circle Agent Marketplace | not listed | the owner submits Circle's intake form; reviewed by hand (section 4) |
| mcpservers.org (the only way into wong2/awesome-mcp-servers) | not listed | the owner's form, which asks for a contact email; free review within about 2 weeks |
| punkpeye/awesome-mcp-servers and awesome-remote-mcp-servers | not listed | a pull request from the owner's GitHub account; the remote list asks contributors to star it first and wants the Glama badge, which we have |
| GitHub MCP Registry (github.com/mcp) | not listed | curated: the owner emails partnerships@github.com |
| PulseMCP | not listed | submissions paused since 2026-09-03; it says it will copy the official registry when it reopens |

Checked and not worth it on 2026-11-16: **Smithery** (accounts, API and CLI shut down on
2026-10-31; now a curated list with no submissions), **mcp.so** (a $39 paid submission is the
only option shown), **MCP Market** (free queue of 4 to 6 weeks; little traffic),
**Docker MCP Catalog** (a long pull request queue, and Docker's MCP clients cannot pay x402),
**Cline's marketplace** (untouched since mid-2025), **xpaysh/awesome-x402** and
**Merit-Systems/awesome-agentic-commerce** (nothing merged since late July 2026),
**x402.org's ecosystem page** (gone since x402 moved to the Linux Foundation), A2A agent-card
registries (they need a real A2A endpoint), and ERC-8004 registries (a wallet signature, and no
sign that buyers search them).

## Why settling payments does not list us

Facilitator catalogs (Coinbase CDP's Bazaar, PayAI, Dexter) list the resources that settled
through that facilitator. The service settles through xpay, then Primer, and neither publishes
a catalog (`/discovery/resources` answers 404 on both). The indexes that crawl "the x402
ecosystem" mostly read those catalogs (Agent402 reads CDP, PayAI, GoPlausible and Solvador), so
real payments did not put the service in them: the origin is registered with each index
instead (section 3).

CDP's facilitator lists automatically, and the service can now use it: with a CDP Secret API
key in the `production` environment (`AIMART_CDP_API_KEY_ID`, `AIMART_CDP_API_KEY_SECRET`),
CDP settles Base first, ahead of xpay and Primer ([payments.md](payments.md)). It is not a
default because it needs that key, which only the owner can create, and charges $0.001 per
settlement after the first 1,000 a month, half the margin of a $0.002 call
([economics.md](economics.md)). What lists the service, per CDP's documentation and the x402
Bazaar spec: a payment for a route that **settles through CDP**, whose payload echoes the
`bazaar` extension of that route's 402. Every paid REST route here declares one, and the x402
SDKs' clients echo it. Each route is listed after its own first such payment, asynchronously
(CDP says within about 15 minutes); a route nobody paid for through CDP is not listed, and
settlements through xpay or Primer do not count. agentic.market shows what the Bazaar holds.
The steps, and how to check the listing, are in
[deploy/README.md](../deploy/README.md), "List the service in the CDP Bazaar".

## What we serve (free)

| path | read by | generated from |
|---|---|---|
| `GET /openapi.json` (`x-payment-info` on each paid operation, `security: []` on each free one, `info.x-guidance`) | x402scan, Agent402, x402gle, agents | `api/app.py` `paid()`, `_openapi_with_guidance()` |
| `GET /.well-known/x402` | x402scan (after OpenAPI), Agent402, x402 crawlers | `Payments.discovery_document()` |
| `GET /.well-known/mcp/server-card.json` | MCP scanners (Smithery read it until it closed) | `discovery.mcp_server_card()` |
| `GET /llms.txt` | LLM agents, crawlers; linked from `/openapi.json` | `discovery.llms_txt()` |
| `GET /pricing` | agents, `scripts/smoke.py` | `discovery.pricing()` |
| `/mcp` `initialize` and `tools/list` | every MCP client and directory | `mcp/server.py` |
| `extensions.bazaar` in every 402 | facilitator catalogs (Bazaar), indexes that replay examples | `payments/bazaar.py` `EXAMPLES` |

Some fields matter for discovery even though they do nothing for function: `extensions.bazaar`,
`x-payment-info`, `security: []` on free operations (an operation with neither that nor
`x-payment-info` is one x402scan probes for a 402, and lists as failed when it answers 200),
`resource.url`, `serviceName` (at most 32 ASCII characters), at most 5 `tags`,
`accepts[].extra.tag` (from `AIMART_BAZAAR_TAG`), and the server-card fields. Developers who
reason only from function tend to drop such fields, so each one needs a test that fails when it
goes missing.

The bazaar examples are real calls. Their inputs are sample files in
[`docs/examples`](examples/) fetched from GitHub, and their outputs are what the service
answers for them; a test pays for each one and compares
(`tests/integration/test_bazaar_examples_are_real.py`). Catalogs replay examples (x402gle pays
for each route and grades the answer) and agents copy them, so an example that cannot run is a
route that looks broken.

## Channels

### 1. Official MCP Registry (registry.modelcontextprotocol.io)

This is the root listing: Glama copies it, and PulseMCP will.

- [`server.json`](../server.json) in the repo root. It validates against
  `https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json`. The name is
  `io.github.balogvn/aimart-parquet`. It lists the PyPI package (`uvx aimart-parquet mcp`,
  stdio) and the remote (`streamable-http`). x402 prices go under
  `_meta["io.modelcontextprotocol.registry/publisher-provided"]`, which must stay under 4 KB,
  and its `networks` name only what the hosted remote takes (USDC on Base mainnet).
- **Ownership proof for PyPI:** the registry checks that the PyPI package description contains
  `<!-- mcp-name: io.github.balogvn/aimart-parquet -->`. That line is in the README, which is
  the package's long description. Do not remove it.
- **Publishing:** `release.yml`, after the PyPI upload succeeds, runs `mcp-publisher` with
  GitHub OIDC (`mcp-publisher login github-oidc`, then `mcp-publisher publish`). It needs
  `permissions: id-token: write`, and the workflow must run in the `balogvn` account, because
  the `io.github.balogvn/*` namespace is proven by GitHub identity. No token is stored. It also
  stamps the release version and the `AIMART_PUBLIC_BASE_URL` variable's address into
  `server.json`, and refuses to publish a placeholder remote.
- release-please bumps `version` in `server.json` (the top-level field and the PyPI package's)
  on every release. The registry rejects a republish of an existing version.
- **The 0.1.2 entry is wrong until the next release.** The 0.1.2 release commit carried
  `server.json` from before #32, so the registry's 0.1.2 entry still lists testnet and Solana
  networks, which the hosted remote does not take. `server.json` on main is fixed; the next
  release publishes the right networks, and a version cannot be republished. Since then the
  release guard (`scripts/check_release_diff.py`, run by `release.yml` before anything is
  published) stops a release that changes anything but version numbers.

### 2. Glama

Glama copied the server from the registry about 90 minutes after the first publish: a
connector page (`glama.ai/mcp/connectors/io.github.balogvn/aimart-parquet`, health-checked,
with the prices that are written in each tool's description) and a server page built from the
repository README, which is why the README must describe the live service. The owner can
sign in with GitHub and claim both pages; nothing else is needed.

### 3. x402 indexes: x402scan, Agent402, 402 Index

None of them finds a service by itself: register the origin once, and again after routes or
prices change. Each sends only public facts (the address, the descriptions and prices the
service already publishes); none needs an account, a key or a signature.

- **x402scan** (<https://www.x402scan.com/resources/register>): enter the host. It reads
  `/openapi.json` (then `/.well-known/x402`), probes each route that is not declared free for a
  402, and registers the ones that answer one. Server page:
  <https://www.x402scan.com/server/f9a7740c-6797-438e-97fe-e0ed58b456c8>. It suggests
  `info.contact.email` for verified ownership: that would publish an email address, so it is
  left out.
- **Agent402** (agents find tools through its `/api/find` and its router): one unpaid request,
  ```bash
  curl -X POST https://agent402.tools/api/index/register \
    -H 'content-type: application/json' \
    -d '{"origin": "https://balogvn--aimart-parquet.us-east.modal.run"}'
  ```
  then `GET https://agent402.tools/api/index?seller=balogvn--aimart-parquet.us-east.modal.run`
  shows `routable: true`. It re-crawls every 30 minutes, without paying.
- **402 Index** (<https://402index.io>, about 52,000 paid endpoints): one
  `POST https://402index.io/api/v1/register` per paid route (`url`, `name`, `protocol: x402`,
  `http_method: POST`, `probe_body: "{}"`, `price_usd`, category
  `developer-tools/data-conversion`). Each waits for review; claiming the domain
  (`/api/v1/claim`) would approve them at once, and is not done.

### 4. Circle Agent Marketplace

A curated catalog (about 100 sellers) that agents query through Circle's keyless Discovery API.
The owner submits Circle's intake form (linked from <https://agents.circle.com/services>) with
the service URL, `/openapi.json`, the public payout address and a short description. Review is
by hand, with no published criteria or timeline; listed services are health-checked.

### 5. x402gle

x402gle lists an API after an "audition": its CLI
(`npx @dexterai/opendexter audition <origin> --json`) makes a real paid call to each route,
paid by x402gle, grades the answer, and lists the routes that pass. The bazaar examples are
what it sends, which is why they must be real calls. Not run yet: it executes a third-party
npm package, so it is the owner's decision.

### 6. Facilitator catalogs (Bazaar)

Facilitators that settle our payments can list the resources in their catalogs, using the
`bazaar` extension in our 402s (input example, input schema, output example). Catalogs
validate the example against the schema strictly and **silently skip** entries that fail. That
is why the schemas in `payments/bazaar.py` are self-contained (no `$ref`) and a test validates
them. Neither xpay nor Primer publishes a catalog today (see "Why settling payments does not
list us"); Dexter's and PayAI's are at `/discovery/resources`, and CDP's Bazaar lists only
resources settled through CDP.

**CDP Bazaar and agentic.market.** Set up by the owner with a CDP key
([deploy/README.md](../deploy/README.md), "List the service in the CDP Bazaar"); status: not
listed until the owner adds the key and one paid call settles through CDP. CDP's discovery
endpoints are public (no key): `GET
https://api.cdp.coinbase.com/platform/v2/x402/discovery/merchant?payTo=<payee>` lists what
CDP holds for our payee, `.../discovery/resources` and `.../discovery/search?query=parquet`
the whole catalog. A settle answer may carry an `EXTENSION-RESPONSES` header whose `bazaar`
entry says `success`, `processing` or `rejected` (with a reason); its absence says nothing
(x402 Bazaar spec). CDP also has a public `POST .../v2/x402/validate` that probes a route's
402 for a usable `bazaar` declaration without paying or listing anything (cdp-sdk 1.53.0,
`validate_x402_resource`); worth a run on `/v1/convert` before the first paid call. Reported
by other sellers on GitHub, not seen here: a resource whose first CDP settlement carried a
malformed declaration may not be re-listed by later clean ones, and a changed price can stay
stale in the merchant list; the declarations here are validated by a test before any
release (`integration/test_bazaar_examples_are_real.py`).

### 7. The free local funnel

`uvx aimart-parquet mcp` is free and open source, and it is listed in the registry next to the
remote. When `AIMART_HOSTED_URL` is set, local convert results carry a one-line `hint`
pointing at the hosted service (no install, works from any agent platform, signed
provenance). This is the "free local, paid hosted" funnel from the spec. The hint appears once
per result and never blocks anything.

### 8. Let real traffic decide the roadmap: demand signals

Two free, zero-cost signals tell you what agents tried to buy that you do not sell yet:

- **Unsupported formats.** Every conversion rejected for its file type logs the *extension*
  only (never the content or full name).
- **`request_capability`** (MCP tool) / **`POST /v1/feedback`** (REST). Its description invites
  any agent to say what it needed, the formats involved and the most it would pay per call.

Both are logged as `demand_signal` lines. Without an admin token, those log lines are the
interface. On Modal (the hosted service) they are in the app's logs: modal.com, Apps,
`aimart-parquet`, Logs, search for `demand_signal` (the Starter plan keeps a day of logs).
`GET /v1/demand` cannot be switched on there yet: `deploy/modal_app.py` passes no
`AIMART_ADMIN_TOKEN`. On Cloud Run, Cloud Logging keeps the lines under its free tier:

```bash
gcloud logging read 'jsonPayload.msg="demand_signal"' --limit 100 --format json
```

With `AIMART_ADMIN_TOKEN` set, `GET /v1/demand` with `Authorization: Bearer <token>` also
returns the counters since the last restart (without it, the endpoint answers 404). On Cloud
Run, `deploy.yml` overwrites the service's secrets on every deploy, so a token set by hand does
not survive: name a Secret Manager secret in the GitHub variable `GCP_SECRET_ADMIN_TOKEN`
(`deploy/setup-gcp.sh` creates one with `SECRET_ADMIN_TOKEN=aimart-admin-token`) and deploy
([deploy/README.md](../deploy/README.md#demand-report-get-v1demand)). After a few weeks live,
build the most requested format or tool next.

### 9. Future work

- The official MCP Server Card (SEP-2127, final): served at `<mcp url>/server-card`, in a
  different shape from `/.well-known/mcp/server-card.json`, and listed from
  `/.well-known/ai-catalog.json`. Not served yet; no crawler that matters was seen reading it.
- Tool wrappers for **LangChain** and **CrewAI** that pay through x402 (an x402 httpx
  transport under a `StructuredTool`), published as a small separate package.

## After each release

1. Run `make smoke URL=… SMOKE_ARGS="--expect-pay-to … --expect-network … --require-provenance"`.
2. `release.yml` publishes to PyPI, GHCR and the registry; check that Glama shows the version.
3. If routes or prices changed, register the origin again on x402scan and Agent402, and the
   changed routes on 402 Index (section 3).
4. Read each index as a client would, and paginate to the reported total. A truncated page is
   not proof that you are absent.
