---
name: release-and-deploy
description: How a change becomes a release and a running deployment - conventional commits, the release-please PR, version agreement across pyproject/__version__/server.json, publishing, and proving a deploy worked by decoding a live HTTP 402. Use when writing commit messages, cutting or checking a release, deploying to Cloud Run, flipping to mainnet, or when someone asks "is the deploy OK?".
---

# Release and deploy

Never push, tag, publish or deploy on your own initiative: these are public, and the owner
does them or explicitly asks for them. This skill is how to do them correctly when asked.

## 1. Commits drive releases

Conventional Commits, because release-please reads them to pick the next version and write
`CHANGELOG.md` (do not hand-edit released sections):

| prefix | release effect | example |
|---|---|---|
| `feat:` | minor | `feat: add Feather input format` |
| `fix:` | patch | `fix: settle MCP payments only after a non-error result` |
| `feat!:` or a `BREAKING CHANGE:` footer | major | `feat!: rename query_parquet input sql -> query` |
| `docs:` `test:` `refactor:` `perf:` `build:` `ci:` `chore:` | none (listed or hidden) | `docs: worked MCP payment example` |

One logical change per commit; the body says why. A price change is a `feat:` or `fix:` with
the old and new price in the body, because agents and catalogs cache prices.

## 2. The release PR

release-please keeps a release PR open on `main`. Merging it tags `vX.Y.Z` and the publish
workflows run (see `.github/workflows/` for the exact jobs: PyPI trusted publishing, the GHCR
image `ghcr.io/balogvn/aimart-parquet`, and the MCP registry entry).

Before merging, check that every version string agrees, because each is read by a different
consumer. `__version__` (served by `/health` and the MCP server info) comes from the installed
package metadata, so `pyproject.toml` is its only source; `server.json` is the other copy:

```bash
grep -n '^version' pyproject.toml
python3 -c "import json; d=json.load(open('server.json')); print(d['version'], [p.get('version') for p in d.get('packages', [])])"
```

`server.json` must also still validate against its `$schema` and keep `description` <= 100
characters (see [docs/discovery.md](../../../docs/discovery.md#official-mcp-registry)). PyPI
ownership for the registry is proven by the `<!-- mcp-name: io.github.balogvn/aimart-parquet -->`
line in `README.md`: never remove it.

## 3. Deploy

Cloud Run is the host ([ADR 0004](../../../docs/adr/0004-hosting-cloud-run.md)); the runbook is
`deploy/README.md`. Set `AIMART_PUBLIC_BASE_URL` to the real https origin, or resource URLs and
download links in every 402 will point somewhere else.

## 4. Verify values, not acknowledgements

"Deployed", "success" and "Ready" are claims about a write. A deploy is verified only when a
live unpaid call returns the price and payee you meant to charge:

```bash
HOST=https://<service-url>
# 1. the decoded challenge: amount in atomic USDC units, network, payTo, asset
curl -s -D - -o /dev/null -X POST "$HOST/v1/inspect" -H 'content-type: application/json' -d '{}' \
  | grep -i '^payment-required:' | cut -d' ' -f2 | tr -d '\r' | base64 -d \
  | jq '.accepts[] | {network, amount, payTo, asset}'
# 2. the version you released is the version running
curl -s "$HOST/health"
# 3. discovery agrees with the paywall
curl -s "$HOST/pricing" | jq -c '[.operations[] | {mcp_tool, price}]'
curl -s -X POST "$HOST/mcp" -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | jq -c '[.result.tools[].name]'
```

Expected on mainnet: `network` is `eip155:8453` (Base) and/or
`solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp`, `amount` is `"2000"` for inspect at $0.002 (`"10000"`
per $0.01), and `payTo` is exactly the configured receive address. On testnet the network is
`eip155:84532`. `make smoke URL=$HOST` automates the same checks (`scripts/smoke.py`).

If any value differs, the deploy failed, whatever the console says. Check secrets by length,
never by value: `${#AIMART_SIGNING_KEY_MLDSA}` should be 44 (base64 of a 32-byte seed).

## 5. Flipping to mainnet

Mainnet changes who pays, not just a setting. In order:

1. `AIMART_NETWORK_MODE=mainnet` and `AIMART_PAY_TO_EVM` / `AIMART_PAY_TO_SVM` set to
   receive-only addresses the owner controls. The server holds no private key; there is no
   setting for one and there must never be.
2. Startup logs show no `price below facilitator minimum` error (the price-floor check runs on
   the first paid request, against each facilitator's `/supported`).
3. The step-4 decode shows mainnet networks and the right `payTo`.
4. One real paid call, made by the owner, following the verify-payments skill's mainnet rules.

## 6. Roll back

Route traffic to the previous revision (`gcloud run services update-traffic <service>
--to-revisions=<previous>=100 --region <region>`), then repeat step 4 against it.
