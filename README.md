# AImart Parquet

<!-- mcp-name: io.github.balogvn/aimart-parquet -->

Messy data in, clean Parquet out, paid per call by AI agents.

AImart Parquet is an MCP server and REST API that turns messy CSV/TSV, JSON/NDJSON, Excel,
SPSS, Stata and SAS files, and tables in digital PDFs, into clean, typed, zstd-compressed
Parquet. You can then inspect the result (footer only), preview rows, and query it with
sandboxed, read-only DuckDB SQL, including joins across several files. Agents pay per call in
USDC with [x402 v2](https://www.x402.org/). There is no signup and no API key.

**Your data:** inputs are deleted when the call ends. Converted files become unreachable after
60 minutes and are normally deleted within minutes after that; with Cloud Storage the worst case
is about a day (the bucket's 1-day lifecycle rule). File contents are never logged or used for
training. Details: [docs/privacy.md](docs/privacy.md).

Why it exists: an agent that reads a 100,000-row CSV as text spends a large share of its
context on raw rows. With this service it reads a compact schema report and then asks targeted
SQL questions. Every conversion reports two rough numbers so you can judge the trade-off for
your own data: `estimated_tokens_raw` (input text bytes / 4) and `estimated_tokens_schema`
(a column digest of name, type and null count, / 4). The ratio depends on rows × columns, so
there is no fixed saving:

| input (measured with `aimart-parquet convert`) | `estimated_tokens_raw` | `estimated_tokens_schema` |
|---|---:|---:|
| 3-row, 2-column CSV | 9 | 18 |
| 100,000-row, 6-column synthetic CSV (4.2 MB → 0.6 MB Parquet) | 1,044,271 | 59 |

For tiny files the report costs more than the file, so just read those directly. The schema
estimate covers only the column digest. The full JSON response is larger: it adds per-column
notes and, on the hosted service, a provenance block with a 3,309-byte ML-DSA-65 signature
(4,412 base64 characters).

> **Status:** v0.1.0. It is not yet deployed or published to PyPI. Every
> `https://YOUR-SERVICE.run.app` below is a **placeholder** for your own deployment's URL
> ([self-hosting](#self-hosting-on-cloud-run)). Until the PyPI release, run the local server
> from a checkout with `make mcp`.

## Contents

- [Quickstart for agents](#quickstart-for-agents)
- [Pricing](#pricing)
- [Supported formats](#supported-formats)
- [Limits and errors](#limits-and-errors)
- [Premium quantum tier](#premium-quantum-tier)
- [Post-quantum provenance](#post-quantum-provenance)
- [Security model](#security-model)
- [Self-hosting on Cloud Run](#self-hosting-on-cloud-run)
- [Development](#development)
- [Documentation](#documentation)

## Quickstart for agents

The typical flow is: `convert_to_parquet` returns an `artifact_id` and a schema report. Then
`inspect_parquet` or `preview_parquet` lets you look, and `query_parquet` answers questions
with SQL. Pass the `artifact_id` between calls instead of re-sending the file. Inputs are a
public `url` (best for large files), `content_base64` plus `filename` (under about 3 MB), or the
`artifact_id` of a file converted earlier (convert takes one too, to convert it again with new
options). Over MCP only `convert_to_parquet` takes `content_base64`; on REST, convert, inspect,
preview and query take all three.

### REST: 402 → decode → pay → retry

An unpaid call returns HTTP **402** with an empty body. The quote is in the base64
`PAYMENT-REQUIRED` header:

```bash
URL=https://YOUR-SERVICE.run.app   # placeholder

curl -s -D - -o /dev/null -X POST "$URL/v1/convert" \
  -H 'content-type: application/json' \
  -d '{"url": "https://example.com/sales.csv"}' \
  | awk 'tolower($1) == "payment-required:" {print $2}' | tr -d '\r' | base64 --decode | jq .
```

```json
{
  "x402Version": 2,
  "error": "Payment required",
  "resource": {"url": "https://YOUR-SERVICE.run.app/v1/convert", "mimeType": "application/json", "...": "..."},
  "accepts": [{
    "scheme": "exact",
    "network": "eip155:84532",
    "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    "amount": "20000",
    "payTo": "0x…",
    "maxTimeoutSeconds": 300,
    "extra": {"name": "USDC", "version": "2"}
  }]
}
```

`amount` is in USDC atomic units (6 decimals): `20000` is $0.02. `curl` cannot sign. An x402
client signs the quote with your wallet and retries with a `PAYMENT-SIGNATURE` header. The
settlement comes back in `PAYMENT-RESPONSE`. With the x402 Python SDK
(`pip install "x402[evm,httpx]"`):

```python
import os

import anyio
import httpx
from eth_account import Account
from x402 import max_amount, x402Client
from x402.http.clients.httpx import x402AsyncTransport
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact import ExactEvmClientScheme

URL = "https://YOUR-SERVICE.run.app"  # placeholder
TEST_KEY = os.environ["PAYER_PRIVATE_KEY"]  # a Base Sepolia test wallet; stays on your side

payer = x402Client()
payer.register("eip155:84532", ExactEvmClientScheme(EthAccountSigner(Account.from_key(TEST_KEY))))
payer.register_policy(max_amount(20_000))  # never pay more than $0.02 per call


async def main() -> None:
    transport = x402AsyncTransport(payer, httpx.AsyncHTTPTransport())
    async with httpx.AsyncClient(base_url=URL, transport=transport, timeout=180) as http:
        r = await http.post("/v1/convert", json={"url": "https://example.com/sales.csv"})
        artifact_id = r.json()["artifact"]["id"]
        r = await http.post(
            "/v1/query",
            json={
                "artifact_id": artifact_id,
                "sql": "SELECT region, sum(units) FROM data GROUP BY 1",
            },
        )
        print(r.json()["rows"])


anyio.run(main)
```

A complete, tested script for REST, MCP and provenance checks is in
[docs/examples/agent_pays.py](docs/examples/agent_pays.py). The step-by-step guide is in
[docs/agents-guide.md](docs/agents-guide.md). If a paid retry still returns 402, the body is
again `{}` and the reason is in the `PAYMENT-REQUIRED` header's `error` field.

### MCP

The remote server speaks Streamable HTTP at `/mcp`. `initialize` and `tools/list` are free.
Each paid `tools/call` is charged through x402's MCP transport: an unpaid call returns
`isError: true` with the PaymentRequired object in `structuredContent`, and the client retries
with the signed payload in `params._meta["x402/payment"]`.

**Claude Code:**

```bash
claude mcp add --transport http aimart https://YOUR-SERVICE.run.app/mcp
```

**Cursor, or any client that takes a remote URL** (`.cursor/mcp.json` or equivalent):

```json
{ "mcpServers": { "aimart": { "url": "https://YOUR-SERVICE.run.app/mcp" } } }
```

**Free local server** over stdio, for files on your machine. It needs no payments, no wallet
and no network. After the PyPI release:

```bash
claude mcp add aimart-local -- uvx aimart-parquet mcp
# with the quantum tools:
claude mcp add aimart-local -e AIMART_ENABLE_QUANTUM=true -- uvx --from 'aimart-parquet[quantum]' aimart-parquet mcp
```

```json
{ "mcpServers": { "aimart-local": { "command": "uvx", "args": ["aimart-parquet", "mcp"] } } }
```

Local mode takes `path` inputs and writes the output next to the input, or to `output_path`.
If `AIMART_HOSTED_URL` is set, convert results include a one-line `hint` that points at the
hosted service, which adds signed provenance.

> **Paying from an MCP host.** Today most MCP hosts do not sign x402 payments by themselves.
> In those hosts, `tools/list` works and paid tools return the PaymentRequired error result.
> To pay over MCP you need an x402-capable MCP client, such as the Python client in
> [docs/agents-guide.md](docs/agents-guide.md). The local server never asks for payment.

### Joins

One query reads up to 8 tables in total: the top-level input is table `data`, and `tables` adds
up to 7 more by name, for one query price. REST takes an object per table (`artifact_id`, or
`url` of a Parquet file); the MCP tool takes the `artifact_id` string itself (a local path in
local mode). With
`sql` = `SELECT d.*, o.status FROM data d JOIN orders o USING (order_id)`:

```text
REST  POST /v1/query  {"artifact_id": "<invoices_id>", "sql": "...", "tables": {"orders": {"artifact_id": "<orders_id>"}}}
MCP   query_parquet   {"artifact_id": "<invoices_id>", "sql": "...", "tables": {"orders": "<orders_id>"}}
```

## Pricing

Prices are the `Settings` defaults, in USDC per **successful** call. A call that fails (HTTP
≥ 400, or an MCP `isError` result) is never settled, so you are not charged for errors. A
deployment can override each price with `AIMART_PRICE_*`. Its live list is at `GET /pricing`.

| operation | REST | MCP tool | price |
|---|---|---|---:|
| convert | `POST /v1/convert`, `POST /v1/convert/upload` (multipart) | `convert_to_parquet` | $0.02 |
| inspect (footer only) | `POST /v1/inspect` | `inspect_parquet` | $0.002 |
| preview | `POST /v1/preview` | `preview_parquet` | $0.002 |
| query (one read-only SELECT; up to 8 tables: `data` plus up to 7 more in `tables`) | `POST /v1/query` | `query_parquet` | $0.005 |
| premium (off by default): quantum-kernel PCA | `POST /v1/quantum/compress` | `quantum_compress_features` | $0.50 |
| premium (off by default): QAOA portfolio | `POST /v1/quantum/portfolio` | `quantum_optimize_portfolio` | $1.00 |

**Free:** `GET /`, `/health`, `/pricing`, `/llms.txt`, `/openapi.json`, `/docs`,
`/.well-known/x402`, `/.well-known/mcp/server-card.json`, `/.well-known/aimart-signing-key`,
`POST /v1/verify` and the `verify_provenance` tool, `POST /v1/feedback` and the
`request_capability` tool (tell us what you needed and would pay for), signed artifact downloads
(`GET /v1/artifacts/{id}/{filename}?exp&sig`), MCP `initialize` and `tools/list`, and the whole
local stdio server.

The floor is $0.002 because facilitators reject payments below about $0.0011–0.0015, and some
charge up to about $0.002 per settlement. Below that floor a call is either unsettleable or
loss-making. See [docs/economics.md](docs/economics.md).

Networks: testnet by default (Base Sepolia `eip155:84532`, facilitator
`https://x402.org/facilitator`). Mainnet (Base `eip155:8453` via Heurist, Solana via Dexter)
must be switched on explicitly by the operator. See [docs/payments.md](docs/payments.md).

## Supported formats

| family | extensions | notes |
|---|---|---|
| delimited text | `.csv` `.tsv` `.tab` `.txt` | sniffs encoding (UTF-8, UTF-16, cp1252…), delimiter and header; skips title rows; `1.234,56` and `$1,234` numbers; IDs with leading zeros stay text |
| JSON | `.json` `.jsonl` `.ndjson` `.geojson` | arrays, NDJSON, nested records (`json_records_path`, `flatten_nested`); repairs broken JSON up to 5 MB |
| spreadsheets | `.xlsx` `.xlsm` `.xlsb` `.xls` `.ods` | pick a `sheet` by name or index; the report lists `available_sheets` |
| SPSS | `.sav` `.zsav` `.por` | variable and value labels kept (Parquet metadata, applied, or both) |
| Stata | `.dta` | labels kept |
| SAS | `.sas7bdat` `.xpt` | labels kept |
| Parquet | `.parquet` `.pq` | re-typed and re-compressed |
| PDF (digital) | `.pdf` | tables with or without borders; tables continuing across pages are merged; pick pages with `pdf_pages` (`"1-3,7"`) and a table with `pdf_table`; the report lists every table found. Scanned PDFs (images, no text layer) are rejected: no OCR. Password-protected PDFs are rejected with an error that says so |

Any of these can also arrive gzip-, bz2- or zip-compressed, once or twice (a zip of
`data.csv.gz` is read as the CSV); xz, zstd, lz4, 7z and rar, and a third layer, are refused
with 415 `unsupported_format`. Detection uses magic bytes before extensions. Every column in the report carries its type, null count, rejected values with
examples, and notes on anything the service assumed: "assumed day-first", "ambiguous
thousands/decimal separator; assumed a decimal comma, so '1.234' is 1234" (the column's own
values decide, then the file's other number columns when they agree and one writes a thousands group with a
decimal separator, else the delimiter), "values without a
UTC offset were read as UTC (k of n)", "values with a UTC offset were rejected (k of n)" or
"converted to UTC", and "zone 'BST' was not read as a zone (k of n)" for a zone abbreviation
with several meanings (BST, IST, CST, ...; only offsets, UTC, IANA names and GMT, EST, MST,
HST and JST are read). Report warnings say when nested JSON keys that differ only by case were
renamed (`a.K is now a.K_2`), title lines above a PDF table (or atop each of its pages) were
dropped, or a PDF table has no header row (its columns are then `column_1`, `column_2`, ...;
`header=true` reads its first row as names). `expected_schema`
with `strict` turns the conversion into a validation step. The full option list is in the
`ConvertOptions` schema (`/openapi.json`, or the tool's input schema), and the notes are listed
in the [agents guide](docs/agents-guide.md#3-convert).

## Limits and errors

A failed call is never charged: every refusal is HTTP ≥ 400 (or an MCP `isError` result) with
a `code`, a `message` and a `hint`. A call that can only fail, decided from the request alone,
is refused before any price is quoted: a body or arguments that fail the schema, not exactly one
input, `content_base64` that decodes to no file (or, for inspect, preview and query, to no
Parquet file), an upload whose file part is empty, an MCP argument the tool does not take, a
URL that can never be fetched, or SQL of up to 256 characters that is not one `SELECT` (400
`invalid_request`, 422 `invalid_input`, 422 `url_not_allowed` or 400 `query_rejected`, or an
MCP error result; the full list is in the
[agents guide](docs/agents-guide.md#6-handle-errors)). The defaults, which an operator can change:

| limit | default | setting | over it |
|---|---|---|---|
| input size (`url`, `content_base64` or upload) | 50 MB per call, all inputs of the call together; stored `artifact_id`s do not count | `AIMART_MAX_INPUT_BYTES` | 413 `input_too_large` |
| decompressed size | 256 MB for a gzip, bz2 or zip stream; 512 MB for the XML of all the sheets of an xlsx or ods | `AIMART_MAX_DECOMPRESSED_BYTES`, `AIMART_MAX_WORKBOOK_XML_BYTES` | 413 `input_too_large` |
| memory per job | 700 MB, counting its resident memory plus its temporary files | `AIMART_JOB_MEMORY_LIMIT_MB` | 413 `input_too_large` ("The job exceeded its memory budget") |
| stored outputs | 512 MiB for all outputs together (256 MiB on the Cloud Run deploy without a bucket) | `AIMART_ARTIFACT_MAX_TOTAL_BYTES` | 413 for one output larger than the whole budget; 503 `busy` while the space is held by outputs that have not expired (kept until their `expires_at`) or outputs still settling; the hint says when to retry |
| preview and query results | 500 preview rows, 1,000 query rows, cells cut to 2,000 characters, 64 MB in all | `AIMART_MAX_PREVIEW_ROWS`, `AIMART_MAX_QUERY_ROWS`, `AIMART_MAX_CELL_CHARS` | more rows: `truncated: true`; over 64 MB: 413 |
| time | 120 s to convert, 15 s to inspect, preview or query | `AIMART_CONVERT_TIMEOUT_S`, `AIMART_QUERY_TIMEOUT_S` | 504 `timeout` |
| downloads | 60 s of downloading for all of a call's `url` inputs together | `AIMART_FETCH_TIMEOUT_S` | 422 `fetch_failed` |
| concurrency | 2 jobs; a call waits for a free one as long as its payment window leaves it (60 s to convert, 165 s to inspect or preview, 160 s to query), with up to 4 calls per job waiting | `AIMART_MAX_CONCURRENT_JOBS`, `AIMART_QUEUE_WAIT_S` | 503 `busy` |

**CSV memory no longer grows with the file.** The text is staged as compressed Parquet instead of
loaded into a table, so a conversion's process stays at about 150-330 MiB whatever the file's size
(it took about 3.5 times the file, and CSVs over about 120 MB were refused). The job's files still
grow with it, and they count toward the 700 MB a job may use because `/tmp` is RAM on Cloud Run:
the upload, the staged text and the output (those two about 0.4-0.65 times the CSV), and no input
outlives its read. A gzip of CSV or TSV is not unpacked: DuckDB reads it where it lies, once one
pass has counted all of it against the 256 MB cap and checked that DuckDB reads it as Python does,
and text that is not UTF-8 is then transcoded straight from it, its UTF-8 version on disk while it
is read. A gzip DuckDB would read otherwise is unpacked, as before: one of more than one member
(concatenated, appended to a row at a time, or bgzip's blocks: DuckDB can stop before the last
member, with no error), or with a header field DuckDB refuses (FTEXT, FHCRC, FCOMMENT) or zero
padding after it. What gzip, pigz, Python and pandas write is one member. A bz2 or zip is unpacked
too, and one that is not UTF-8 also holds its UTF-8 version while it is transcoded, which then
replaces the unpacked text. Measured with the defaults, the input caps bind first: a 255 MiB CSV
sent as 37 MiB of gzip converted at 245 MiB in all, and 230 MiB of orders in cp1252 sent as 45 MiB
of gzip at 533 MiB; the same orders sent as bz2 or zip peaked at 574-664 MiB, close to the cap,
with the unpacked text and its UTF-8 version both on disk. With the input caps raised, a 446 MiB
CSV sent as 159 MiB of gzip converts (506 MiB; it used to be stopped at the cap), and 450 MB sent
uncompressed is stopped there (157 MiB in memory plus 544 MiB of files). `sort_by` sorts the whole
file and spills to disk past 400 MiB; the spill counts too, and DuckDB may spill only the 300 MiB
the cap leaves it: a 100 MB CSV sorts (468 MiB), and so does 198 MiB sent as gzip (654 MiB), while
200 MB sent uncompressed is refused. Over the cap the call fails with 413, unpaid: split such
files, or convert them with the free local server, which applies the same defaults, with
`AIMART_MAX_INPUT_BYTES`, `AIMART_MAX_DECOMPRESSED_BYTES` and `AIMART_JOB_MEMORY_LIMIT_MB` raised
to suit your machine.
[deploy/README.md](deploy/README.md#memory-capacity-what-one-2-gib-instance-converts) has the
numbers.

Password-protected and scanned PDFs (422 `invalid_input`), a column preview or query cannot show,
such as a time past year 9999 (422; cast it to text), and a storage failure (503
`storage_unavailable`) are refused the same way. Every code, and what to do about it, is in the
[agents guide](docs/agents-guide.md#6-handle-errors).

## Premium quantum tier

**Off by default.** A deployment serves these tools only with `AIMART_ENABLE_QUANTUM=true` (and
the `quantum` extra installed). A skeptical buyer panel of AI agents valued them at $0 and said
they distract from the core product, so the hosted service leads with conversion.

> **Honesty first.** Both premium tools run on a quantum circuit **simulator** (PennyLane),
> not on quantum hardware. At these sizes a simulator gives **no speed or accuracy advantage**
> over classical methods. Every result therefore includes a classical baseline computed in the
> same call, with a comparable metric, and the first entry of `notes` always says so. We do not
> claim quantum advantage.

- **`quantum_compress_features`** ($0.50) runs quantum-kernel PCA. Rows are embedded with an
  entangling `IQPEmbedding` feature map and compared by state fidelity, and the kernel is
  decomposed (Nyström, on sampled landmarks). The bandwidth is chosen automatically to avoid
  kernel concentration. It writes a Parquet artifact with `qpc_1..k` (quantum) and `pc_1..k`
  (classical PCA) side by side, plus a `neighbor_preservation_*` score for each, so you can use
  whichever scores higher.
- **`quantum_optimize_portfolio`** ($1.00) picks `budget` of up to 14 assets using QAOA with an
  XY mixer started from the uniform feasible state. It returns the result together with the
  exact optimum from exhaustive classical search and the approximation ratio.

What we measured, and when to use which output: [docs/quantum.md](docs/quantum.md). The spec
and honesty contract: [docs/specs/quantum-premium.md](docs/specs/quantum-premium.md).

## Post-quantum provenance

Every artifact the hosted service produces carries a `provenance` block: a manifest
(`artifact_id`, `sha256`, `size_bytes`, `filename`, `operation`, `created_at`, `service`,
`version`), an **ML-DSA-65** signature (NIST FIPS 204, a post-quantum signature scheme), and a
`public_key_url`. Signing proves the file came unaltered from this service. It does not prove
the data is correct.

To verify:

- **Free endpoint:** `POST /v1/verify` with `{"manifest": …, "signature": …, "artifact_id": …}`
  (or the free `verify_provenance` MCP tool). With `artifact_id`, the server also re-checks the
  stored file's hash and size.
- **Offline:** fetch `GET /.well-known/aimart-signing-key` once. It gives the base64 public key
  and the context string. Verify the signature over the canonical JSON of the manifest (sorted
  keys, separators `(",", ":")`, UTF-8), then compare the downloaded file's SHA-256 with
  `manifest.sha256`:

```python
import base64, hashlib, json
from cryptography.hazmat.primitives.asymmetric.mldsa import MLDSA65PublicKey  # cryptography >= 48

key = MLDSA65PublicKey.from_public_bytes(base64.b64decode(key_doc["public_key"]))
message = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
key.verify(base64.b64decode(signature), message, key_doc["context"].encode())  # raises if forged
assert hashlib.sha256(parquet_bytes).hexdigest() == manifest["sha256"]
```

The operator sets the key with `AIMART_SIGNING_KEY_MLDSA` (base64 of a 32-byte seed). If it is
unset, the server uses an ephemeral key, logs a warning, and reports `"ephemeral": true`.
Signatures from an ephemeral key stop verifying after a restart.

## Security model

In short:

- URL fetches are SSRF-guarded at connect time: every resolved address must be public, every
  redirect is re-checked, only web ports are allowed, and size and time are capped.
- DuckDB is locked down: no external access, file access only to the job's own directory,
  configuration locked. Queries are exactly one `SELECT`, with a row cap and an interrupt at
  the deadline.
- Every parse of a caller's file (convert, inspect, preview, query) runs in a killable child
  process with a deadline and a memory cap, so a parser crash or a memory bomb fails one
  unpaid call.
- Decompression is streamed against hard caps.
- Request bodies are size-limited and clients are rate-limited.
- The server holds no private keys, only receive (`payTo`) addresses.
- Downloads use HMAC-signed, expiring links.

The details and how to report a vulnerability are in [SECURITY.md](SECURITY.md).

## Self-hosting on Cloud Run

The container image (`Dockerfile`: multi-stage, digest-pinned bases, non-root) serves REST and
MCP on `$PORT`. The minimum configuration is `AIMART_PAY_TO_EVM` and/or `AIMART_PAY_TO_SVM`
(receive addresses), `AIMART_PUBLIC_BASE_URL`, and `AIMART_SIGNING_KEY_MLDSA` from Secret
Manager. Deployment steps, the spend cap and the free-tier budget are covered in
[deploy/README.md](deploy/README.md), with the rationale in
[ADR 0004](docs/adr/0004-hosting-cloud-run.md).

```bash
make docker
docker run --rm -p 8080:8080 -e AIMART_PAY_TO_EVM=0xYourReceiveAddress aimart-parquet:dev
```

The image will be published as `ghcr.io/balogvn/aimart-parquet` once release automation exists.

## Development

Python 3.12+ (the repo pins 3.13), managed with [uv](https://docs.astral.sh/uv/).

| command | does |
|---|---|
| `make setup` | install locked dependencies, every extra, and the dev group |
| `make check` | lint + type + test: the definition of green |
| `make fmt` / `make lint` / `make type` / `make test` | the parts of `check` |
| `make serve` | REST + MCP over HTTP on `$PORT` or 8000 (testnet unless configured) |
| `make mcp` | the free local stdio MCP server |
| `make docker` | build the image |
| `make smoke URL=https://host` | decode a live deployment's 402s and check prices, payTo and network |
| `make mutate` | mutation-test the money and security paths |

The CLI converts single files too: `aimart-parquet convert data.sav -o data.parquet` and
`aimart-parquet inspect data.parquet`. The contributor guide is
[CONTRIBUTING.md](CONTRIBUTING.md). AI coding agents should start with [AGENTS.md](AGENTS.md).

## Documentation

- [Architecture](docs/architecture.md): request path from agent to signed artifact
- [Payments](docs/payments.md): x402 over REST and MCP, facilitators, testnet and mainnet
- [Agents guide](docs/agents-guide.md): step by step, with a runnable example
- [Quantum tier](docs/quantum.md): what the premium tools do and measured behaviour
- [Discovery](docs/discovery.md): registries, directories and catalogs
- [Economics](docs/economics.md): fees against margins, and honest revenue scenarios
- [Privacy](docs/privacy.md): what happens to your data, stated from the code
- [Decision records](docs/adr/README.md)
- [Changelog](CHANGELOG.md), [Security](SECURITY.md), [Code of conduct](CODE_OF_CONDUCT.md)

## License

[Apache-2.0](LICENSE). Copyright Kayode Balogun.
