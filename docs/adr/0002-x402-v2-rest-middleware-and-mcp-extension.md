# 0002: x402 v2: SDK middleware for REST, our own MCP extension, lazy facilitator init

- Status: accepted
- Date: 2026-10-17 (built in commit 40b6380; recorded 2026-10-18)

## Context and problem statement

Every paid operation must be payable over REST and over MCP, with one invariant: verify,
then run, then settle only on success. While building it we found three problems:

1. The SDK's REST middleware (`x402.http.middleware.fastapi.payment_middleware`) does the
   right thing: it verifies before the route, buffers the response, and settles only when the
   status is below 400. But by default it fetches the facilitator's `/supported` at
   construction. On a scale-to-zero host, a facilitator blip during a cold start would then
   crash-loop the server.
2. Passing `sync_facilitator_on_start=False` avoids that, but then **the SDK never initializes
   the resource server at all**. Its lazy path is guarded by the same flag, so every paid
   request would fail.
3. The SDK's MCP helpers (`x402.mcp`) import `mcp.server.fastmcp`, which **mcp 2.x removed**
   (FastMCP became `mcp.server.mcpserver.MCPServer`). They do not work with the MCP SDK we
   use.

## Considered options

- REST: put the SDK middleware in front of everything, including `/mcp`. Rejected: that would
  make `initialize` and `tools/list` paid, and directories and agents could no longer see the
  tools.
- REST: write our own HTTP middleware. Rejected: the SDK's version is correct, and a second
  implementation of the money path is a second place for bugs.
- MCP: pin mcp 1.x to use `x402.mcp`. Rejected: it gives up protocol versions and
  `MCPServer` features, and still ties us to an unmaintained bridge.
- MCP: implement the x402 v2 MCP transport ourselves, over the SDK's core resource-server
  API, as an mcp 2.x `Extension`.

## Decision outcome

- **REST:** we use the SDK middleware with `sync_facilitator_on_start=False`, wrapped by
  `Payments.http_middleware()`. For paid paths, the wrapper first calls
  `Payments.ensure_initialized()`: a lazy, double-checked, lock-protected
  `server.initialize()` plus a price-floor check. If initialization fails, the wrapper
  answers `503 payments_unavailable` ("you were not charged"). The middleware covers the
  paid `/v1/*` routes only.
- **MCP:** `payments/mcp_gate.py` (`X402McpGate`) intercepts `tools/call` for paid tools only.
  An unpaid call returns `isError: true` with `structuredContent` = PaymentRequired. A paid
  call reads `params._meta["x402/payment"]` and verifies it, runs the tool, and settles only if
  `isError` is false. It then returns the settlement in `_meta["x402/payment-response"]`. If
  settlement fails, the output is withheld. x402 v1 payloads are refused.
- Prices, routes and tool names come only from `payments/pricing.py` `OPERATIONS`. A paid
  route or tool that is missing from that table is silently free, and wire tests guard
  against it.

### Consequences

- Good: discovery stays free, and the money invariant holds on both doors. A facilitator
  outage costs failed, unpaid requests, not a crash loop.
- Good: tests drive the bridge with the official x402 client (`create_payment_payload`), so
  the wire format matches what real clients send.
- Bad: we maintain the MCP bridge until the SDK supports mcp 2.x. Re-check `x402.mcp` on each
  SDK upgrade, and delete the bridge when it works.
- Bad: we rely on SDK behaviour (`sync_facilitator_on_start` semantics, response buffering)
  that could change in an update. Tests assert the behaviour, not the code.
