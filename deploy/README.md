# Deploying AImart Parquet

The hosted service runs on **Google Cloud Run** (us-central1, request-based billing, scale to
zero), pulled straight from the public image `ghcr.io/balogvn/aimart-parquet`. GitHub Actions
deploys it with no stored credential: Workload Identity Federation (WIF) exchanges the job's OIDC
token for a short-lived Google token. The secrets live in Secret Manager and never pass
through GitHub.

```
merge release PR ─► release.yml ─► PyPI, GHCR (+ provenance, SBOM), MCP Registry
                                └► deploy.yml (image by digest) ─► Cloud Run
                                      └► scripts/smoke.py decodes live 402s ─► pass, or roll back
synthetic.yml (every 6 h) ─► scripts/smoke.py against production ─► issue on failure
```

| File | What it is |
|---|---|
| `setup-gcp.sh` | One-time, idempotent Google Cloud setup (APIs, service accounts, WIF, secrets, optional bucket and admin token). Prints the GitHub variables to set. |
| `smoke-flags.sh` | Turns the deploy configuration into `scripts/smoke.py` expectations; shared by deploy and synthetic checks. |
| `modal_app.py` | Untested sketch for the Modal fallback. |
| `../.github/workflows/deploy.yml` | The deploy: digest in, verified revision out, rollback on failure. |

Service shape (set by `deploy.yml`): 1 vCPU, 2 GiB, `concurrency=4`, `max-instances=1`,
`min-instances=0`, 300 s request timeout, startup CPU boost, CPU billed only during requests.
The request timeout is the payment window (`PAYMENT_WINDOW_S`, the 300 s a payment authorization
is valid for): a paid call may wait for a job slot, download its inputs and run its job inside
it. A shorter timeout answered 504 to calls still running, and one that finished afterwards
could still be settled: charged for a response the caller never received.

Memory on that instance is shared by the API (about 250 MB), the forkserver (about 250 MB), two
jobs (`AIMART_MAX_CONCURRENT_JOBS=2`) and, without a bucket, the artifacts in RAM-backed `/tmp`.
Each job is a child process that the parent kills once its resident memory plus its temporary
files pass `AIMART_JOB_MEMORY_LIMIT_MB` (700 by default; `0` disables the cap), and the call
fails with 413 `input_too_large`, unpaid ([ADR 0005](../docs/adr/0005-job-isolation-forkserver.md)).
The files count because `/tmp` is RAM here: a 1.5 MB gzip CSV once decompressed to 500 MB there
while the job's resident memory alone stayed under the cap. A CSV conversion's process stays at
about 150-330 MiB whatever the file's size, and its files grow with it (see "Memory capacity"
below), so with the defaults the input caps bind before the job cap. A gzip/bz2/zip stream is
also stopped as it inflates past `AIMART_MAX_DECOMPRESSED_BYTES` (256 MB), before it fills
`/tmp`; a workbook's XML parts are capped apart, by `AIMART_MAX_WORKBOOK_XML_BYTES` (512 MB),
because every sheet is counted though only one is parsed. Raising the cap without more
memory per instance trades those refusals for an out-of-memory restart that fails every
in-flight call.

---

## 1. First deploy, step by step

Do these in order. Budgets come before anything that can bill.

### 1.1 Google Cloud project and billing

1. Create a project (console: **IAM & Admin → Create project**), note its **project ID**.
2. Link a billing account (**Billing → Link a billing account**). A card is required even when
   usage stays inside the free tier.

### 1.2 Budgets and the spend cap (before the first deploy)

Two separate controls. Set both.

**A. The Cloud Run spend cap (stops spending).** In the console, open **Billing → Budgets &
alerts → Create budget**:

1. Scope: this project, service **Cloud Run** only.
2. Amount: **$5 per month**.
3. Action: choose the option that **pauses the Cloud Run service when the budget is reached**
   (the spend cap). Without that action a budget only sends email.
4. Save, then confirm on the Cloud Run service page that the cap is shown as attached.

Two properties to plan around:

- **The cap can be computed on gross cost**, i.e. usage at list price *before* the free tier is
  subtracted. The free tier itself is worth about $6 a month at list price (180k vCPU-s ≈ $4.32,
  360k GiB-s ≈ $0.90, 2M requests ≈ $0.80). A $5 gross cap therefore pauses the service at
  roughly 80% of the free tier: you never pay for Cloud Run, but a busy month can pause it
  early. Raise the cap if you would rather pay a little than go dark.
- **It is not instantaneous.** Billing data arrives with a delay of hours, so real spend can run
  past the cap before the pause lands. `max-instances=1` bounds how fast: at full load one
  instance costs about $0.10 an hour in CPU and memory.

**B. An alert-only billing budget (tells you).** **Billing → Budgets & alerts → Create budget**
again: scope **all services** of this project, amount **$5**, threshold rules at 50%, 90% and
100% of actual spend plus 100% of forecasted spend, email to billing admins. This one catches
anything outside Cloud Run (Secret Manager, Storage, egress) and spend the cap has not caught up
with yet. The CLI equivalent (needs the Billing Budgets API):

```sh
gcloud billing budgets create --billing-account=XXXXXX-XXXXXX-XXXXXX \
  --display-name="aimart alert" --budget-amount=5USD \
  --filter-projects="projects/$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')" \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 \
  --threshold-rule=percent=1.0,basis=forecasted-spend
```

### 1.3 Run the setup script

```sh
gcloud auth login
PROJECT_ID=my-project deploy/setup-gcp.sh
# or, with a bucket for artifacts (recommended once downloads get large; see the cost model):
PROJECT_ID=my-project BUCKET=my-project-aimart-artifacts deploy/setup-gcp.sh
# to keep an ML-DSA key you already publish, paste existing values instead of generating:
PROJECT_ID=my-project SECRET_SOURCE=prompt deploy/setup-gcp.sh
# to switch on GET /v1/demand, also create an admin token secret (section 2, "Demand report"):
PROJECT_ID=my-project SECRET_ADMIN_TOKEN=aimart-admin-token deploy/setup-gcp.sh
```

It is safe to re-run: it creates only what is missing and never adds a new secret version. It
prints the GitHub variables, including `AIMART_PUBLIC_BASE_URL`, which is Cloud Run's
deterministic URL `https://<service>-<project-number>.<region>.run.app` and is known before the
first deploy.

The WIF provider accepts tokens only from `balogvn/aimart` **and** the GitHub account id
`20478656` (balogvn). The id matters: a repository *name* can be re-registered by someone else
after a rename or deletion, the numeric id cannot. Override with `GITHUB_REPO=...` and
`GITHUB_OWNER_ID=...` for a fork.

### 1.4 GitHub configuration

Repository **Settings → Secrets and variables → Actions → Variables** (repository variables,
so both the deploy and the synthetic check can read them; every value here is public anyway):

| Variable | Required | Value |
|---|---|---|
| `GCP_PROJECT_ID` | yes | from the script |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | yes | from the script |
| `GCP_SERVICE_ACCOUNT` | yes | the deployer SA, from the script |
| `GCP_RUNTIME_SERVICE_ACCOUNT` | yes | the runtime SA, from the script |
| `GCP_REGION` | no | `us-central1` (default) |
| `GCP_SERVICE` | no | `aimart-parquet` (default) |
| `GCP_SECRET_SIGNING_KEY`, `GCP_SECRET_SIGNING_SECRET` | no | secret names, if you changed them; a secret kept in another project is `projects/<project number>/secrets/<name>` (the runtime SA needs `secretAccessor` on it there) |
| `GCP_SECRET_ADMIN_TOKEN` | no | name of the Secret Manager secret holding `AIMART_ADMIN_TOKEN`, e.g. `aimart-admin-token` (or its `projects/...` path, as above); unset: `GET /v1/demand` is off |
| `AIMART_PUBLIC_BASE_URL` | yes | the run.app URL, or your custom domain |
| `PRODUCTION_URL` | yes, for the synthetic check | same as above |
| `AIMART_NETWORK_MODE` | no | `testnet` (default) or `mainnet` |
| `AIMART_PAY_TO_EVM` | one of the two | Base address that receives USDC (receive-only) |
| `AIMART_PAY_TO_SVM` | one of the two | Solana address that receives USDC (receive-only) |
| `AIMART_ARTIFACT_MAX_TOTAL_BYTES` | no | `268435456` (the default applied when no bucket is set) |
| `AIMART_GCS_BUCKET` | no | bucket name; artifacts then leave the instance's RAM |
| `AIMART_FACILITATOR_URLS` | no | comma-separated override of the default facilitators |
| `AIMART_ENABLE_QUANTUM` | no | `true` serves the premium quantum tools (off by default); both smoke checks then require them |
| `AIMART_JOB_MEMORY_LIMIT_MB` | no | `700` (the application default when unset): memory, in MB, one isolated job may use, counting its resident memory plus its temporary files in `/tmp` (RAM on Cloud Run), before the parent kills it and the call fails with 413 `input_too_large`, unpaid; `0` disables the cap. Sized for the 2 GiB instance (see the service shape above) |
| `SMOKE_SIGNING_KEY_SHA256` | no, recommended | pins the public signing key (section 1.7) |

`deploy.yml` always sets `AIMART_TRUST_FORWARDED_FOR=true` (Google's front end sets
`X-Forwarded-For`) and omits any optional variable that is empty rather than sending `""`.

**Every runtime setting goes through these variables.** `deploy.yml` deploys with
`env_vars_update_strategy: overwrite` and `secrets_update_strategy: overwrite`: the variables and
secret references it passes are the service's whole configuration. A setting changed by hand
(`gcloud run services update --update-env-vars ...`, or the console) is removed by the next
deploy, and a setting `deploy.yml` does not pass cannot be set at all; add it to the workflow
and to this table first. The synthetic check reads the same variables, so a changed variable
also changes what it expects.

**Secrets** (all optional): `RELEASE_PLEASE_TOKEN` (fine-grained PAT or GitHub App token with
contents + pull requests read/write on this repository, so the release PR triggers CI),
`CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`; runs PR reviews on your subscription) or
`ANTHROPIC_API_KEY`.

**Environments** (**Settings → Environments**):

- `production`: deployment branches **main only**; add yourself as a required reviewer if you
  want to approve each deploy. `deploy.yml` runs in it.
- `pypi`: deployment branches **main only**. PyPI Trusted Publishing is bound to it.

**Branch protection** (a ruleset on `main`): require a pull request, require the status check
**`ci-ok`** (it aggregates every CI job), block force pushes. Keep CodeQL *default setup* off:
`codeql.yml` is the advanced setup and GitHub rejects both at once.

### 1.5 PyPI Trusted Publishing

On PyPI: **Your account → Publishing → Add a new pending publisher**: project `aimart-parquet`,
owner `balogvn`, repository `aimart`, workflow `release.yml`, environment `pypi`. No API token
is ever created.

### 1.6 The first release

1. Push to `main`. release-please opens a release PR. The manifest says 0.1.0 is already
   released, so the PR proposes the next version from the Conventional Commits (with
   `bump-minor-pre-major`, a `feat:` makes 0.2.0). To publish **0.1.0** first instead, create a
   GitHub release `v0.1.0` on the current commit before release-please runs, or add
   `"release-as": "0.1.0"` to `release-please-config.json` for one release and remove it after.
2. Merge the release PR. `release.yml` publishes to PyPI and pushes the image.
3. **GHCR packages start private, and Cloud Run and the MCP registry can only pull public
   images.** The first run's `mcp-registry` and `deploy` jobs therefore fail with a pull error.
   Make the package public once: **github.com/balogvn → Packages → aimart-parquet → Package
   settings → Change visibility → Public**. Then **Re-run failed jobs** on that workflow run.
4. The deploy job verifies the new revision serves the digest it deployed with 100% of
   traffic, then runs `scripts/smoke.py` against `AIMART_PUBLIC_BASE_URL`: every paid route
   and MCP tool is called unpaid, the 402 challenge decoded and compared with `/pricing`, the
   configured payee and network, canonical USDC, and the signing key.

### 1.7 Pin the signing key (trust on first use)

After the first successful deploy, record the public key's fingerprint so every later deploy and
synthetic run fails if it changes unexpectedly:

```sh
curl -s "$URL/.well-known/aimart-signing-key" | jq -r .public_key | base64 -d | shasum -a 256
gh variable set SMOKE_SIGNING_KEY_SHA256 --repo balogvn/aimart --body '<the hex digest>'
```

---

## 2. Operating it

### Deploying by hand

**Actions → Deploy → Run workflow**, with an image digest (from a release run's summary, or
`docker buildx imagetools inspect ghcr.io/balogvn/aimart-parquet:<version>`). Optionally give
the version so `/health` is checked too. Deploys are by digest only: a tag can move, a digest
cannot.

### Rollback

- **Automatic.** If anything after the deploy step fails (traffic switch, digest check, smoke
  test), `deploy.yml` restores the exact previous traffic split and the job fails.
- **To a known-good release (preferred).** Run the Deploy workflow with that release's digest.
  It goes through the same verification as any deploy.
- **Emergency, from a terminal:**

  ```sh
  gcloud run revisions list --service aimart-parquet --region us-central1 --project "$PROJECT_ID"
  gcloud run services update-traffic aimart-parquet --to-revisions=<revision>=100 \
    --region us-central1 --project "$PROJECT_ID"
  ```

  Traffic then stays pinned to that revision; the next successful deploy moves it back to the
  latest revision (`deploy.yml` always runs `update-traffic --to-latest`).

### Rotating the ML-DSA signing key

Every artifact's provenance is signed with this key. After a rotation, **signatures made with
the old key no longer verify** against `/.well-known/aimart-signing-key` or `POST /v1/verify`,
which only know the current key. Rotate when the key may have leaked, not on a schedule.

1. Save the current public key document, for the record and for anyone holding old artifacts:
   `curl -s "$URL/.well-known/aimart-signing-key" > signing-key-$(date +%F).json`. If old
   signatures must stay verifiable, publish these old public keys (for example in `docs/keys/`
   with the dates they were valid); ML-DSA-65 verification needs only the public key.
2. Add a new version, generated and piped without echo:

   ```sh
   python3 -c 'import base64, secrets, sys; sys.stdout.write(base64.b64encode(secrets.token_bytes(32)).decode())' \
     | gcloud secrets versions add aimart-signing-key-mldsa --data-file=- --project "$PROJECT_ID"
   ```

3. Deploy (Run workflow with the current digest). The service reads `:latest` at instance
   start, so any cold start would also pick it up; a deploy makes the moment explicit.
4. Update `SMOKE_SIGNING_KEY_SHA256` (section 1.7), or the next smoke test fails, by design.
5. Disable the old version: `gcloud secrets versions disable <n> --secret aimart-signing-key-mldsa`.

The HMAC secret (`aimart-signing-secret`) signs download links; rotating it the same way only
invalidates links already handed out, which expire within an hour anyway.

### Switching to mainnet

Real money. Do it deliberately, and on a quiet day.

1. Decide the payees: `AIMART_PAY_TO_EVM` (Base mainnet) and/or `AIMART_PAY_TO_SVM` (Solana
   mainnet). These are **receive-only** addresses you control; the server never holds a key.
   Send yourself a tiny amount on each chain first to prove you can see and move funds there.
2. Set `AIMART_NETWORK_MODE=mainnet`. The default facilitators become Heurist (Base, EIP-3009)
   then Dexter (Solana); override with `AIMART_FACILITATOR_URLS` only if you have a reason.
3. Deploy. The smoke test expectations follow automatically (`smoke-flags.sh` maps mainnet to
   `eip155:8453` and Solana mainnet), and canonical mainnet USDC is checked.
4. Read the logs for price-floor warnings ("price below facilitator minimum"): a price under a
   facilitator's minimum is unsettleable, and settlement fees of $0.001-0.002 can exceed the
   margin on the cheapest operations (AGENTS.md, money invariant 4).
5. Pay for one cheap call yourself (e.g. `inspect`) and confirm the USDC arrives at the payee.

Mainnet is reversible: set `AIMART_NETWORK_MODE=testnet` and deploy.

### Logs

`gcloud run services logs read aimart-parquet --region us-central1 --project "$PROJECT_ID"`, or
Logs Explorer with `resource.type="cloud_run_revision"`. Logs are JSON (`AIMART_LOG_JSON` is on
by default), so Cloud Logging indexes the fields.

### Demand report (`GET /v1/demand`)

Demand signals (rejected file extensions, `request_capability` requests) are always logged as
`demand_signal` lines; without a token, those log lines are the interface
([docs/discovery.md](../docs/discovery.md)). To also serve the counters at `GET /v1/demand`:

1. Create the secret: `PROJECT_ID=... SECRET_ADMIN_TOKEN=aimart-admin-token deploy/setup-gcp.sh`
   generates a random token, stores it in Secret Manager and grants the runtime SA access.
2. `gh variable set GCP_SECRET_ADMIN_TOKEN --repo balogvn/aimart --body aimart-admin-token`.
3. Deploy. `deploy.yml` then adds `AIMART_ADMIN_TOKEN=aimart-admin-token:latest` to the service's
   secrets; setting the variable by hand on the service would not survive the next deploy.
4. Read it without echoing the token:

   ```sh
   TOKEN="$(gcloud secrets versions access latest --secret aimart-admin-token --project "$PROJECT_ID")"
   curl -s -H "Authorization: Bearer $TOKEN" "$URL/v1/demand"; unset TOKEN
   ```

The counters cover the time since the instance started (one instance at most); the log lines
cover everything Cloud Logging keeps. Without the token the endpoint answers 404.

---

## 3. Cost model

List prices for us-central1 with request-based billing at the time of writing; check the Cloud
Run pricing page before relying on them.

| Resource | Free each month | Price beyond |
|---|---|---|
| CPU | 180,000 vCPU-seconds | $0.000024 per vCPU-second |
| Memory | 360,000 GiB-seconds | $0.0000025 per GiB-second |
| Requests | 2 million | $0.40 per million |
| Secret Manager | 6 active versions, 10,000 accesses | we use 2 versions (3 with the admin token), one access per secret per instance start |
| Cloud Storage (optional) | 5 GB-months, 100 GB egress from North America | expired artifacts are deleted within minutes; a delete-after-1-day rule is the backstop |
| GHCR (public), GitHub Actions (public repo) | free | |

- **Idle: $0.** Scale to zero; CPU is billed only while a request is in flight.
- **A paid call is two HTTP requests** (the unpaid 402, then the paid retry).
- **A typical `convert`** (about 1.5 s on 1 vCPU, 2 GiB) uses 1.5 vCPU-s and 3 GiB-s: the free
  tier covers roughly 120,000 of them a month. Beyond it, one costs about $0.00004, under 0.5% of
  its $0.02 price. A quantum call of a minute costs about $0.002 against $0.50-1.00. Concurrent
  requests share instance time, so these are upper bounds.
- **Worst case is a flood, not a heavy customer.** Every request is billed, including the cheap
  402s an attacker can generate, and egress beyond the small free allowance is billed too. With
  `max-instances=1` CPU and memory top out near $70 a month at sustained full load, but request
  and egress charges for millions of tiny requests are not bounded by the instance count. The
  spend cap (1.2 A) is the control that bounds them; the alert budget (1.2 B) is how you hear
  about it.
- **Artifacts:** without a bucket they sit in `/tmp`, which on Cloud Run is the instance's RAM
  (counted inside the 2 GiB, wiped when the instance stops), hence the 256 MiB budget. Every
  output there is kept until its `expires_at` (the TTL), never evicted sooner, so once an hour's
  outputs fill the budget a new convert is refused with 503 `busy` (unpaid, with when to retry)
  until the oldest expire. With `AIMART_GCS_BUCKET`, the local copy is only a cache that can be
  evicted at any time, downloads are V4 signed GCS URLs, and they leave Cloud Run's 1 GiB egress
  allowance alone.
- **Retention** (what `/pricing` publishes as `data_retention`): an artifact becomes unreachable
  at `AIMART_ARTIFACT_TTL_S` (60 minutes) and is normally deleted within minutes after that, by a
  sweep that runs at most once a minute while the service has traffic (local files and their
  GCS copies). A GCS copy whose local record was lost in an instance restart is caught by a
  bucket-listing sweep (at most every 6 hours) or, worst case, by the bucket's 1-day lifecycle
  rule from `setup-gcp.sh`: about a day. Without that rule there is no worst-case bound, so keep
  it on any bucket you use.

### Memory capacity: what one 2 GiB instance converts

On Cloud Run `/tmp` is the instance's RAM, so every file a job writes counts against the same
2 GiB as its process. The job watchdog (`AIMART_JOB_MEMORY_LIMIT_MB`, 700 MB) therefore counts
both: a job whose resident memory plus its temporary files (the call's work directory and the
job's own temp directory) pass it is killed and answered 413 `input_too_large`, unpaid. It
polls every 0.1 s, so a job allocating fast passes the cap by what it gains in one poll.

| holds memory | how much |
|---|---|
| API process, forkserver | ~250 MB each |
| a job, process and files together | ≤ 700 MB (`AIMART_JOB_MEMORY_LIMIT_MB`) |
| a job's process | a CSV conversion measured 150-330 MiB whatever the file size; with `sort_by`, up to ~500 MiB |
| DuckDB inside a job | limit = the job cap minus 300 MB (400 MiB), so sorts spill instead of being killed; spill cap = the rest of the job cap (300 MiB), so DuckDB's memory and its spill together never pass the cap |
| a job's files in `/tmp` | the upload (≤ 50 MB) + the staged text (18-31% of the CSV) + the output (20-33%) + spill (`sort_by` only, ≤ 300 MiB). A gzip of CSV or TSV is read by DuckDB where it lies: DuckDB reads UTF-8 from it, and other text is transcoded from it once all of it has been counted against the cap, its UTF-8 version on disk while it is read. A gzip DuckDB would read differently from Python is unpacked instead, as at d83c738: more than one member (DuckDB can stop before the last of them, and refuses a later member's header of 32,760 bytes or more), FTEXT, FHCRC, FCOMMENT or zero padding. That gzip, a bz2 and a zip are unpacked (≤ 256 MB, `AIMART_MAX_DECOMPRESSED_BYTES`), plus a UTF-8 version while one that is not UTF-8 is transcoded, which then replaces the unpacked text. Whatever the reader read is deleted once it returns |
| stored artifacts without a bucket | ≤ `AIMART_ARTIFACT_MAX_TOTAL_BYTES` (256 MiB) |

Measured at faaeaed in the isolated child with the defaults, beside d83c738 (before a gzip was
read where it lies and the unpacked text deleted once read), on macOS on Apple silicon with
other work running: the watchdog's own peak readings, in MiB. Linux, where production runs, was
not measured; there the child's resident memory also counts the ~160 MB of pages it shares
with the forkserver, which macOS counts elsewhere. The first five ran with the input caps as
they are, the rest with `AIMART_MAX_INPUT_BYTES` and `AIMART_MAX_DECOMPRESSED_BYTES` raised so
the file reached the engine. Each input was written into the call's work directory, as an
upload is, and counted. Each gzip is of one member, as Python's gzip module, gzip and pigz write
one; a gzip of more members is unpacked, as at d83c738, and holds its text as a bz2 does.

| CSV (decompressed), as sent | result | process | files | in all | at d83c738 (files) | seconds |
|---|---|---|---|---|---|---|
| 255 MiB, 37 MiB of gzip | converted | 198 | 47 | 245 | 492 (301) | 14 |
| 230 MiB of orders, 45 MiB of gzip | converted | 204 | 51 | 256 | 487 (286) | 10 |
| the same orders in cp1252, 45 MiB of gzip | converted | 284 | 282 | 533 | 581 (411) | 10 |
| the same in cp1252, 23 MiB of bz2 | converted | 285 | 451 | 622 | 565 (395) | 12 |
| the same in cp1252, 45 MiB of zip | converted | 282 | 438 | 609 | 603 (432) | 10 |
| 198 MiB, 71 MiB of gzip | converted | 222 | 145 | 367 | 553 (343) | 18 |
| 198 MiB in cp1252, 71 MiB of gzip | converted | 230 | 305 | 496 | 571 (421) | 17 |
| 198 MiB, 51 MiB of bz2 | converted | 207 | 298 | 460 | 548 (336) | 19 |
| 198 MiB, 71 MiB of zip | converted | 205 | 304 | 463 | 553 (343) | 16 |
| 446 MiB, 159 MiB of gzip | converted | 178 | 336 | 506 | 413 at the cap: 738 (606) | 38 |
| 429 MiB, uncompressed | 413 at the cap | 172 | 544 | 701 | 413 at the cap: 700 (550) | 26 |
| 95 MiB, uncompressed, with `sort_by` | converted | 336 | 132 | 468 | 471 (134) | 8 |
| 191 MiB, uncompressed, with `sort_by` | 413 at the cap | 473 | 229 | 702 | 413 at the cap: 704 (229) | 14 |
| 198 MiB as gzip, with `sort_by` | converted | 485 | 168 | 654 | 413 at the cap: 710 (304) | 17 |
| 446 MiB as gzip, with `sort_by` | 413 at the cap | 467 | 239 | 707 | 413 at the cap: 703 (605) | 34 |

The bz2 and zip peaks last an instant, at the end of the transcode, so the 0.1 s poll catches
them unevenly: over three runs each, 436-456 MiB of files for the bz2 and 395-484 for the zip
at faaeaed, 395-489 and 389-432 at d83c738, the same work in both (the upload, the unpacked
text and its whole UTF-8 version). Without the job cap the 446 MiB gzip needs 177 MiB of
process and 336 MiB of files (211 + 782 at d83c738, its unpacked text on disk to the end).

Before the staged reader (`main` at e8d228e, in process, files not counted), the 191 MiB CSV
peaked at 622-815 MiB resident, a 446 MiB one at 1.17-1.19 GiB and the 40-column one at
901-963 MiB.

What that means for the default deploy (1 vCPU, 2 GiB, `AIMART_MAX_CONCURRENT_JOBS=2`):

- **The input caps bind first.** A CSV reaches the engine only if it is under 50 MB on the wire
  and 256 MB decompressed, and every such file measured converted. Sent as gzip, its files are
  the upload, the staged text and the output (about 50 MiB here; 245-256 MiB in all), and text
  that is not UTF-8 adds its UTF-8 version while it is read (533 MiB). A bz2 or zip is unpacked
  first, so the same cp1252 orders, their unpacked text and its UTF-8 version both on disk while
  it is transcoded, peaked at 574-664 MiB, close to the cap, as at d83c738: with Linux's shared
  pages counted such a file may be a 413, unpaid. The 6-column test CSV compressed 3.3:1, so as
  gzip it is refused at the 50 MB cap from about 165 MB.
- **A job's files, not its process, decide what converts:** for a gzip, the upload plus about
  0.4-0.65 times the CSV (its staged copy and the output); for a bz2, a zip, a gzip DuckDB
  would read differently (more than one member) or an uncompressed CSV, the unpacked text or
  the upload as well. With the input caps raised a 446 MiB CSV sent as
  159 MiB of gzip converts (it was a 413 at d83c738, its unpacked text on disk), while 450 MB
  sent uncompressed reaches the cap; past it the call is a 413, unpaid, never an instance out
  of memory.
- **`sort_by` is the worst case:** DuckDB fills its 400 MiB before it spills, so a sort holds
  ~500 MiB of process; about 100 MB of uncompressed CSV sorts, and so does 198 MiB sent as
  gzip, while 200 MB uncompressed is refused (413).
- **Two jobs at the cap** hold 1.4 GB next to ~500 MB for the API and the forkserver. Stored
  artifacts in `/tmp` come on top, so without a bucket the instance can still run out.
- **Time is the next limit.** 38 s of wall time for the 446 MiB gzip on an Apple M-series
  core (other work running); Cloud Run's vCPU was not measured, and conversions stop at
  `AIMART_CONVERT_TIMEOUT_S` (120 s, unpaid).

To convert bigger files concurrently, set `AIMART_MAX_CONCURRENT_JOBS=1` or give the service
more memory, and set `AIMART_GCS_BUCKET` so stored artifacts leave the RAM disk.

---

## 4. Fallback hosts (documented, not set up)

- **Hugging Face Spaces (Docker)** is no longer free for this (Docker Spaces stopped being free
  on 2026-07-21).
- **Modal Starter:** $30 of monthly credits; set the workspace spend limit to $0 so it stops
  when the credits run out instead of billing. `modal_app.py` is an **untested** sketch of the
  service there; the same image runs unchanged.
- **Oracle Cloud Always Free (Ampere A1):** up to 4 Arm OCPUs and 24 GB RAM, free indefinitely.
  The release publishes an arm64 image, so `docker run` it on the VM behind Caddy (automatic
  TLS) and set `AIMART_PUBLIC_BASE_URL` to that hostname. You operate the VM yourself: updates,
  firewall, restarts; there is no scale to zero and no deploy workflow for it here.

---

## 5. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Deploy fails at "Check inputs": *deploy is not configured* | A required variable is missing; the message lists which. |
| `unauthorized_client ... rejected by the attribute condition` | The token's repository or owner id differs from `setup-gcp.sh`'s `GITHUB_REPO`/`GITHUB_OWNER_ID`; re-run the script with the right values. |
| `Permission 'iam.serviceAccounts.getAccessToken' denied` | The `workloadIdentityUser` binding is missing or names another repository; re-run the script. |
| Revision fails to start: image pull error | The GHCR package is still private (1.6, step 3). |
| Revision fails to start: secret access denied | Runtime SA lacks `secretAccessor` on that secret; re-run the script (with `SECRET_ADMIN_TOKEN=...` if `GCP_SECRET_ADMIN_TOKEN` is set). |
| A setting you changed on the service is gone | The deploy overwrites env vars and secrets (1.4). Set it as a GitHub variable instead. |
| Smoke fails on `payTo` / network | The service advertises a different payee or chain than the variables say. Treat as an incident: an unexpected `payTo` is revenue going elsewhere. |
| Smoke fails on provenance / key fingerprint | `AIMART_SIGNING_KEY_MLDSA` is missing (ephemeral key) or was rotated without updating `SMOKE_SIGNING_KEY_SHA256`. |
| MCP requests rejected with a host error | `AIMART_PUBLIC_BASE_URL` does not match the host being called; the MCP transport allows only that host (and localhost). |
| Paid calls return 503 "facilitator is unreachable" | Facilitator outage; nothing is charged. Check its status, or override `AIMART_FACILITATOR_URLS`. |
| Service paused | The spend cap was reached (1.2 A). Raise it or wait for the next month. |
