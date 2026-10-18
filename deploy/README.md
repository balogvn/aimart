# Deploying AImart Parquet

The hosted service runs on **Google Cloud Run** (us-central1, request-based billing, scale to
zero), pulled straight from the public image `ghcr.io/balogvn/aimart-parquet`. GitHub Actions
deploys it with no stored credential: Workload Identity Federation (WIF) exchanges the job's OIDC
token for a short-lived Google token. The two secrets live in Secret Manager and never pass
through GitHub.

```
merge release PR ─► release.yml ─► PyPI, GHCR (+ provenance, SBOM), MCP Registry
                                └► deploy.yml (image by digest) ─► Cloud Run
                                      └► scripts/smoke.py decodes live 402s ─► pass, or roll back
synthetic.yml (every 6 h) ─► scripts/smoke.py against production ─► issue on failure
```

| File | What it is |
|---|---|
| `setup-gcp.sh` | One-time, idempotent Google Cloud setup (APIs, service accounts, WIF, secrets, optional bucket). Prints the GitHub variables to set. |
| `smoke-flags.sh` | Turns the deploy configuration into `scripts/smoke.py` expectations; shared by deploy and synthetic checks. |
| `modal_app.py` | Untested sketch for the Modal fallback. |
| `../.github/workflows/deploy.yml` | The deploy: digest in, verified revision out, rollback on failure. |

Service shape (set by `deploy.yml`): 1 vCPU, 2 GiB, `concurrency=4`, `max-instances=1`,
`min-instances=0`, 120 s request timeout, startup CPU boost, CPU billed only during requests.

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
| `GCP_SECRET_SIGNING_KEY`, `GCP_SECRET_SIGNING_SECRET` | no | secret names, if you changed them |
| `AIMART_PUBLIC_BASE_URL` | yes | the run.app URL, or your custom domain |
| `PRODUCTION_URL` | yes, for the synthetic check | same as above |
| `AIMART_NETWORK_MODE` | no | `testnet` (default) or `mainnet` |
| `AIMART_PAY_TO_EVM` | one of the two | Base address that receives USDC (receive-only) |
| `AIMART_PAY_TO_SVM` | one of the two | Solana address that receives USDC (receive-only) |
| `AIMART_ARTIFACT_MAX_TOTAL_BYTES` | no | `268435456` (the default applied when no bucket is set) |
| `AIMART_GCS_BUCKET` | no | bucket name; artifacts then leave the instance's RAM |
| `AIMART_FACILITATOR_URLS` | no | comma-separated override of the default facilitators |
| `SMOKE_SIGNING_KEY_SHA256` | no, recommended | pins the public signing key (section 1.7) |

`deploy.yml` always sets `AIMART_TRUST_FORWARDED_FOR=true` (Google's front end sets
`X-Forwarded-For`) and omits any optional variable that is empty rather than sending `""`.

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

---

## 3. Cost model

List prices for us-central1 with request-based billing at the time of writing; check the Cloud
Run pricing page before relying on them.

| Resource | Free each month | Price beyond |
|---|---|---|
| CPU | 180,000 vCPU-seconds | $0.000024 per vCPU-second |
| Memory | 360,000 GiB-seconds | $0.0000025 per GiB-second |
| Requests | 2 million | $0.40 per million |
| Secret Manager | 6 active versions, 10,000 accesses | we use 2 versions, 2 accesses per instance start |
| Cloud Storage (optional) | 5 GB-months, 100 GB egress from North America | delete-after-1-day rule keeps storage near zero |
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
  (counted inside the 2 GiB), hence the 256 MiB budget. With `AIMART_GCS_BUCKET`, downloads are
  V4 signed GCS URLs and leave Cloud Run's 1 GiB egress allowance alone.

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
| Revision fails to start: secret access denied | Runtime SA lacks `secretAccessor` on that secret; re-run the script. |
| Smoke fails on `payTo` / network | The service advertises a different payee or chain than the variables say. Treat as an incident: an unexpected `payTo` is revenue going elsewhere. |
| Smoke fails on provenance / key fingerprint | `AIMART_SIGNING_KEY_MLDSA` is missing (ephemeral key) or was rotated without updating `SMOKE_SIGNING_KEY_SHA256`. |
| MCP requests rejected with a host error | `AIMART_PUBLIC_BASE_URL` does not match the host being called; the MCP transport allows only that host (and localhost). |
| Paid calls return 503 "facilitator is unreachable" | Facilitator outage; nothing is charged. Check its status, or override `AIMART_FACILITATOR_URLS`. |
| Service paused | The spend cap was reached (1.2 A). Raise it or wait for the next month. |
