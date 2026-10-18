# Security policy

## Reporting a vulnerability

Email **balogvn@gmail.com** with "SECURITY: aimart-parquet" in the subject. Please include
what you found, how to reproduce it, and what an attacker gains. Do not open a public issue
for anything that could cost a payer or the operator money, leak data, or reach the host's
network.

You will get an acknowledgement within 5 working days. Fixes for confirmed issues go out as a
patch release, and the advisory credits you unless you prefer otherwise. Please give us a
reasonable window, normally 90 days, before public disclosure.

**Supported versions:** only the latest release. Before 1.0, fixes are not backported.

**In scope:** this repository's code, its container image, and the hosted service when it is
deployed. **Out of scope:** the x402 facilitators, the chains, wallets, and third-party MCP
hosts. Report those to their maintainers.

## Threat model

AImart Parquet is a public, paid, anonymous service. Anyone with a wallet can:

1. send **untrusted bytes** (CSV, JSON, Excel, SPSS/Stata/SAS, Parquet, PDF, or a compressed
   form of any of them) to native parsers;
2. make the server **fetch a URL** of their choosing;
3. run **their own SQL** against the converted data;
4. send **payment payloads** that may be malformed, replayed, underpriced, or aimed at the
   wrong network or asset.

What we protect, in priority order:

- **The money flow.** No caller is charged for failed work, no work is given away unpaid, and
  payment can only go to the configured `payTo` address.
- **The host.** No reads of server files or secrets, no reach into the private network or
  cloud metadata, and no crash or exhaustion that takes down other callers' paid requests.
- **Other callers' data.** Artifacts are addressed by unguessable IDs and expire.
- **Provenance.** A signature that verifies means the file came unaltered from this service.

Non-goals: the service does not judge whether data is true, and it does not filter prompt
injection hidden inside cell values. Treat converted data as untrusted input in your own agent.

## Controls

### Payments: receive-only, verify → work → settle

- **The server holds no private keys.** Configuration names only receive addresses
  (`AIMART_PAY_TO_EVM`, `AIMART_PAY_TO_SVM`). There is deliberately no private-key setting. A
  leaked environment can redirect future revenue, but it cannot move funds already received.
- Every paid call is **verified** with the facilitator before any work, and **settled** only
  if the work succeeded: HTTP < 400 over REST, `isError=false` over MCP. Errors are typed
  (`AimartError`, status ≥ 400), so a failure can never look like a success. A settlement
  failure after the work withholds the result.
- A payment must match a quoted requirement exactly (amount, `payTo`, network, asset).
  Amounts are integers. Floats never touch money.
- Testnet is the default. Mainnet requires explicit operator configuration. Repository
  scripts that could spend mainnet USDC refuse to run without a typed confirmation sentence.
- `scripts/smoke.py` decodes a live deployment's 402 challenges and checks the price, `payTo`,
  network and USDC contract against independent expectations.

### URL fetching: SSRF guard at connect time

`sources.py` resolves every hostname and requires **every** resolved address to be globally
routable. It unwraps IPv4-mapped, NAT64, 6to4 and IPv4-compatible IPv6, and rejects Teredo.
It then connects to the address it checked, so DNS rebinding between check and connect is
impossible. Each redirect (at most 5) goes through the same check. Only ports 80, 443, 8080
and 8443 are allowed. Downloads are capped by size (`AIMART_MAX_INPUT_BYTES`, 50 MB by
default) and by a deadline (`AIMART_FETCH_TIMEOUT_S`, 60 s). `AIMART_ALLOW_PRIVATE_URLS`
exists only for tests.

### Untrusted files: caps and process isolation

- **Decompression bombs.** gzip, bz2 and zip are inflated as streams and counted against
  `AIMART_MAX_DECOMPRESSED_BYTES` (512 MB). Zip archives are checked from the central
  directory before any member is inflated: at most 10,000 members, and a ratio above 250× is
  refused. Excel files are zips and get the same check.
- **Parser crashes.** Conversion, queries and the quantum tools run in a short-lived child
  forked from a `forkserver` (`engine/isolate.py`). Native code can crash rather than raise:
  DuckDB's `read_json` segfaults on JSON nested about 21,600 levels deep. In the child, that
  becomes one failed and unpaid call, and the server keeps running. The child is killed at
  its deadline (120 s for convert, 15 s for query), and its memory goes back to the OS.
- Broken-JSON repair only runs on documents under `AIMART_MAX_JSON_REPAIR_BYTES` (5 MB).
- Caller-supplied filenames pass through `safe_filename`. Remote callers can never name a
  server path: `path` and `output_path` inputs exist only in the local stdio mode.

### SQL: DuckDB lockdown, SELECT only

Every DuckDB connection (`engine/duck.py`) disables extension autoload and autoinstall, sets
`allowed_directories` to the job's own directories, turns `enable_external_access` off, and
sets `lock_configuration` **last**, so SQL cannot undo any of it with `SET`. The query path
is stricter still. Data arrives only as registered Arrow datasets, so there is no filesystem
access. Spill files go to a private throwaway directory (the default `<cwd>/.tmp` leaked the
server's path through `current_setting()`). Exactly one statement is allowed, and it must be a
`SELECT`. Results are capped (`AIMART_MAX_QUERY_ROWS`), long cells are truncated, a watchdog
interrupts DuckDB at the deadline, and file paths are scrubbed from error messages.

### HTTP surface

- Body limits: 16 MB for JSON and MCP requests, and the input cap plus 1 MB for multipart
  uploads. These are enforced while streaming, whether or not `Content-Length` is declared.
- Rate limit: a per-client token bucket (`AIMART_RATE_LIMIT_PER_MINUTE`, 120 by default),
  held in a bounded LRU so a flood of IPs cannot grow memory. Behind a proxy,
  `AIMART_TRUST_FORWARDED_FOR=true` reads the client address counted from the **right** of
  `X-Forwarded-For` (the entries the proxies appended), because the left side is whatever the
  client chose to send.
- Concurrency: a fixed number of job slots (`AIMART_MAX_CONCURRENT_JOBS`) and a bounded
  queue. Beyond that the server answers 503 `busy`, and nothing is charged.
- The MCP endpoint checks `Host` and `Origin` against the public base URL, which protects
  against DNS rebinding.
- The container runs as a non-root user (uid 1000). Base images are pinned by digest.

### Artifacts and downloads

Artifacts have random 144-bit IDs (`secrets.token_urlsafe(18)`) and become unreachable after
`AIMART_ARTIFACT_TTL_S` (1 hour). A sweep that runs at most once a minute while the service has
traffic then deletes them, normally within minutes, and a size budget evicts old ones. Local
downloads need an HMAC-SHA256-signed, expiring link (`exp` and `sig`). Set
`AIMART_SIGNING_SECRET`, or a random per-boot secret is used. With `AIMART_GCS_BUCKET`,
downloads are GCS V4 signed URLs, the sweep deletes the stored copies too, and a copy orphaned
by an instance restart is deleted in about a day at worst (a bucket-listing sweep, then the
bucket's 1-day lifecycle rule as the last resort; see [docs/privacy.md](docs/privacy.md)).

### Provenance: ML-DSA-65

Every hosted artifact carries a manifest (ID, SHA-256, size, filename, operation, time,
version) signed with **ML-DSA-65** (NIST FIPS 204, a post-quantum lattice signature). The
manifest is serialized as canonical JSON, and the signature uses a domain-separation context
(`aimart-parquet/provenance/v1`). The public key is at `/.well-known/aimart-signing-key`.
`POST /v1/verify` and the `verify_provenance` tool are free. A valid signature proves origin
and integrity. It does not prove the data is correct, and it involves no quantum hardware.
If `AIMART_SIGNING_KEY_MLDSA` is unset, the key is ephemeral and the server logs a warning.

### Secrets

Production secrets live in **Google Secret Manager** and are injected into Cloud Run as
environment variables (see [deploy/README.md](deploy/README.md)): `AIMART_SIGNING_KEY_MLDSA`
and `AIMART_SIGNING_SECRET`, plus `AIMART_ADMIN_TOKEN` if the operator switches on the demand
report. Receive addresses are not secrets. The
repository ignores `.env*` (except `.env.example`), `.wallets/`, `.local/`, `*.pem` and
`*.key`. The Docker build context is an allow-list, so none of them can reach an image layer.
Check a deployed secret by its length, never by printing it. Payment payloads are never
logged in full.

### Supply chain

Dependencies are locked with hashes (`uv.lock`) and installed with `--frozen`. Container base
images are pinned by digest. The money and security modules are mutation-tested
(`make mutate`), so their tests are known to fail when a bug is reintroduced.
