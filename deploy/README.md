# Deploying AImart Parquet

The hosted service runs on **Modal** (section 0): the Starter plan's $30 of monthly credits pay
for it, one copy is kept running (about $11.70 a month), and the workspace budget stops it
rather than billing past them.
GitHub Actions deploys it with a Modal token kept in the `production` environment; the signing
keys live in Modal and never pass through GitHub. **Google Cloud Run** (section 1) is the
alternative: set the repository variable `DEPLOY_HOST=cloudrun` and a release deploys the
released image there instead, with Workload Identity Federation (no stored credential) and
Secret Manager.

```
merge release PR ─► release.yml ─► PyPI, GHCR (+ provenance, SBOM), MCP Registry
                                └► deploy-modal.yml ─► modal-deploy.sh: build from uv.lock + wheel,
                                      deploy, URL check, selftest on Modal
                                      └► scripts/smoke.py decodes live 402s
                                   (DEPLOY_HOST=cloudrun: deploy.yml, image by digest ─► Cloud Run)
synthetic.yml (every 6 h) ─► scripts/smoke.py against production ─► issue on failure
```

| File | What it is |
|---|---|
| `modal_app.py` | The Modal app: the web function (the ASGI app) and the deploy's selftest. |
| `modal-deploy.sh` | Builds `dist/modal/`, makes the signing keys once, deploys, checks the URL, runs the selftest. Run by `deploy-modal.yml`, or by hand. |
| `modal_keys.py` | Creates the Modal secret `aimart-parquet-keys` once (values generated, never printed). |
| `setup-gcp.sh` | Cloud Run only: one-time, idempotent Google Cloud setup (APIs, service accounts, WIF, secrets, optional bucket and admin token). Prints the GitHub variables to set. |
| `smoke-flags.sh` | Turns the deploy configuration into `scripts/smoke.py` expectations; shared by both deploys and the synthetic check. |
| `../.github/workflows/deploy-modal.yml` | The Modal deploy, after every release or by hand. |
| `../.github/workflows/deploy.yml` | The Cloud Run deploy: digest in, verified revision out, rollback on failure. |

Cloud Run's service shape (set by `deploy.yml`): 1 vCPU, 2 GiB, `concurrency=4`, `max-instances=1`,
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

## 0. Deploy on Modal (the current host), step by step

What runs: one container that reserves a quarter of a CPU core and 512 MiB and may use up to
2 GiB (`deploy/modal_app.py`), up to 4 requests at once, at most one container, stopped one
minute after the last request and started again by the next one (a cold start of several
seconds). Stored outputs live on the Modal Volume
`aimart-artifacts`, so download links last their full hour across restarts (1 GiB budget by
default); each call's temporary files stay on the container's local disk. Modal gives a web
request at most 150 s, then answers with a redirect a paying client must not see, so the paid
timeouts are tighter than on Cloud Run: 60 s per conversion, 30 s for all of a call's
downloads, 15 s waiting for a job slot (`RUNTIME_ENV` in `modal_app.py`;
`tests/unit/test_modal_deploy.py` checks they fit).

Cost: Modal bills the higher of what a container reserves and what it uses, for every second
it is up (a core at $0.0000131/s, memory at $0.00000222 per GiB-second), and nothing while it
is stopped. Idle, this container costs about $0.016 an hour, and one is kept running
(`MIN_CONTAINERS` in `modal_app.py`): about $11.70 a month, inside the $30 of monthly credits; a
job adds the core and memory it uses for its few seconds. Kept running, no caller waits for a
cold start (about 7 s), and expired outputs are deleted within a minute or two. The Volume holds at most the 1 GiB budget of outputs, each for an hour.

**The $30 needs a payment method on file.** Without one a Starter workspace gets $1 a month,
about 60 hours awake at this size, and once it is spent Modal disables the workspace: every
request answers 404 `modal-http: workspace ... is disabled` until a payment method is added or
the next month's credit arrives. A public address is rarely idle (registries and crawlers call
it), so plan for the container being awake much of the time. The first deploy reserved a whole
core and 2 GiB, $0.063 an hour awake, and spent the $1 in a day and a half.

### 0.1 Modal account and budget

1. Sign up at modal.com (GitHub login works). The Starter plan is $0 a month plus usage, with
   $30 of compute credits each month.
2. In Modal's settings, under usage and billing, add a payment method (it unlocks the full
   $30 of monthly credits; see "Cost" above) and set the **workspace budget** to $30 or less,
   so nothing is billed beyond the free credits. Check on that page that the budget is in place.
3. Note your **workspace name** (in the dashboard's URLs). The service will be at
   `https://<workspace>--aimart-parquet.modal.run`, or with a region in it, such as
   `https://<workspace>--aimart-parquet.us-east.modal.run`: the first deploy prints the exact
   address.

### 0.2 GitHub configuration

1. **Settings → Environments → New environment** `production`: deployment branches **main only**.
   In it, add two **environment secrets** from a Modal token (Modal's settings → API Tokens →
   New Token). Modal may show the token inside a `modal token set ...` command; only two pieces
   of it are wanted: `MODAL_TOKEN_ID` (it starts with `ak-`) and `MODAL_TOKEN_SECRET` (it starts
   with `as-`). A Proxy Auth Token (`wk-`/`ws-`) is a different thing and does not work here.
   Optional, on mainnet: two more environment secrets, `AIMART_CDP_API_KEY_ID` and
   `AIMART_CDP_API_KEY_SECRET`, put Coinbase's facilitator first for Base and list the service
   in the CDP Bazaar and on agentic.market (section 2, "List the service in the CDP Bazaar").
2. **Settings → Secrets and variables → Actions → Variables**:

| Variable | Required | Value |
|---|---|---|
| `AIMART_PUBLIC_BASE_URL` | yes | the address Modal serves the app at, e.g. `https://<workspace>--aimart-parquet.us-east.modal.run`; start with `https://<workspace>--aimart-parquet.modal.run` and correct it from the first deploy's message |
| `PRODUCTION_URL` | yes, for the synthetic check | the same URL |
| `AIMART_PAY_TO_EVM` | one of the two | Base address that receives USDC (receive-only) |
| `AIMART_PAY_TO_SVM` | one of the two | Solana address that receives USDC (receive-only) |
| `AIMART_NETWORK_MODE` | no | `testnet` (default) or `mainnet` |
| `AIMART_ENABLE_QUANTUM` | no | `true` serves the premium quantum tools (off by default) |
| `AIMART_FACILITATOR_URLS`, `AIMART_JOB_MEMORY_LIMIT_MB`, `AIMART_ARTIFACT_MAX_TOTAL_BYTES` | no | as in section 1.4 |
| `SMOKE_SIGNING_KEY_SHA256` | no, recommended | pins the public signing key (section 1.7) |

Every deploy passes these whole: a setting changed by hand in Modal is replaced by the next
deploy. `DEPLOY_HOST` stays unset (Modal). Spaces and line breaks around a pasted value are
ignored (the first deploy's wallet address ended with one), and the deploy checks every value's
shape before it builds anything: `deploy/modal-deploy.sh --check` runs only those checks.

3. The branch protection ruleset, PyPI Trusted Publishing and the GHCR visibility step are the
   same as for Cloud Run: sections 1.4 (last paragraph), 1.5 and 1.6 step 3.

### 0.3 First deploy

**Actions → Deploy to Modal → Run workflow** on `main`. After that, every release deploys by
itself. The run builds the image from `uv.lock` and the commit's wheel, creates the signing keys
secret `aimart-parquet-keys` the first time (never printed, never rotated by a re-run), deploys,
runs the selftest (a real conversion through the isolated job child, and the memory watchdog
stopping a job), checks that Modal serves the app at `AIMART_PUBLIC_BASE_URL`, then smoke tests
the public URL as the Cloud Run deploy does. The first run usually stops at the address check:
it prints the address Modal serves the app at; set `AIMART_PUBLIC_BASE_URL` and `PRODUCTION_URL`
to it and run the workflow again (the service names itself by that address in every price quote
and download link). When a step fails, the run prints what the app logged, without its access
log (`deploy/modal-deploy.sh --logs`).

By hand: `modal token new` once, then
`AIMART_PUBLIC_BASE_URL=https://<workspace>--aimart-parquet.modal.run AIMART_PAY_TO_EVM=0x... deploy/modal-deploy.sh`.

If the deploy stops with "This version of the modal client supports the following image builder
versions", the workspace builds images with a newer builder than the pinned client knows: raise
`MODAL_VERSION` in `deploy/modal-deploy.sh` (and the same version in `modal_keys.py`'s usage line).

Then pin the signing key as in section 1.7.

### 0.4 Operating it on Modal

- **Redeploy** (after changing a variable): Actions → Deploy to Modal → Run workflow.
- **Roll back**: the Starter plan has no rollbacks. Run the workflow from the previous
  release's tag (Use workflow from → Tags).
- **Logs**: the app's page on modal.com (kept 1 day on Starter).
- **Mainnet**: as in "Switching to mainnet" (section 2), deploying with Deploy to Modal.
- **Client addresses**: Modal passes the caller's address itself, so `X-Forwarded-For` is not
  trusted (`AIMART_TRUST_FORWARDED_FOR=false`).

---

## 1. First deploy, step by step

This section is for Google Cloud Run, the alternative host: set the repository variable
`DEPLOY_HOST=cloudrun` so a release deploys there instead of Modal. Do these in order. Budgets come before anything that can bill.

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

Real money. Do it deliberately, and on a quiet day. These steps are for the Modal host; on
Cloud Run the variables are the same and the workflow is `Deploy` instead of `Deploy to Modal`.

The payee, `AIMART_PAY_TO_EVM` (Base) and/or `AIMART_PAY_TO_SVM` (Solana), is a
**receive-only** address you control; the server never holds a key. If it is new, send
yourself a tiny amount on that chain first, to prove you can see and move funds there.

1. **Probe the facilitators.** In mainnet mode the service uses xpay (Base), Primer (Base, if
   xpay does not answer) and Dexter (Solana), unless `AIMART_FACILITATOR_URLS` names others.
   This asks each only for its public list of what it supports, `GET /supported`:

   ```bash
   for u in https://facilitator.xpay.sh https://x402.primer.systems https://x402.dexter.cash; do curl -s -m 15 -o /dev/null -w "%{http_code} $u\n" "$u/supported"; done
   ```

   Each line should start with `200`. (Without a terminal: open
   `https://facilitator.xpay.sh/supported` in a browser; it should show text that contains
   `eip155:8453`.) One line that is not 200 does not block the switch: the service skips a
   facilitator that does not answer and uses the next one. If xpay **and** Primer both fail,
   wait: nobody would settle Base (Dexter lists it as Permit2, which this service does not
   quote, so it is never given Base), every paid call would answer 503 (nobody is charged)
   and the smoke test would fail
   ([ADR 0003](../docs/adr/0003-facilitators-and-price-floors.md), the amendments).
2. **Set the variable.** GitHub → the repository → Settings → Secrets and variables → Actions
   → Variables: set `AIMART_NETWORK_MODE` to `mainnet` (create it if it is not there). Leave
   `AIMART_FACILITATOR_URLS` unset.
3. **Deploy.** Actions → Deploy to Modal → Run workflow, on `main`.
4. **Read the run's smoke test.** Open the run, then its smoke test step. The expectations
   follow the variable by themselves (`smoke-flags.sh` maps mainnet to `eip155:8453` and
   canonical mainnet USDC), and the last line must read `N passed, 0 failed`. A failure here
   means the service is not quoting what you configured: go to "To undo" and look at the
   run's log before trying again. In the app's log on modal.com, `facilitator skipped` names
   a facilitator that did not answer (the next one is serving), `facilitator dropped` one
   that has not answered for 15 minutes, and `price below facilitator minimum` a price that
   could not be settled.
5. **Make one paid call** (below) and see the USDC arrive at the payee.

**To undo:** set `AIMART_NETWORK_MODE` to `testnet` and run Deploy to Modal again. Nothing
else needs changing.

#### Make one paid test call

The smoke test proves what the service *asks* for. Only a payment proves that the facilitator
accepts it and that the money arrives. What a payer needs on Base: a wallet with a few cents
of USDC on Base. No ETH, and no approval: the wallet signs one authorization for the exact
price to your payee, and the facilitator submits it and pays the gas. Use a wallet made for
this, holding only those few cents, never one that holds your savings.

Checking the payment, either way: open `https://basescan.org/address/<your payee address>`,
then "Token Transfers": a USDC transfer of the price (0.02 for a conversion) from the paying
wallet appears within about a minute.

**From a browser, without handling a key: x402scan** (not yet tried against this service).
x402scan pays with Coinbase's official x402 client, which sends the 402's Bazaar description
back with the payment, and its wallet sits behind an email sign-in, so no key or recovery
phrase is ever shown or typed. The wallet is Coinbase's: use a network that reaches
coinbase.com.

1. Open the service's page, <https://www.x402scan.com/server/f9a7740c-6797-438e-97fe-e0ed58b456c8>,
   and choose **POST /v1/convert**.
2. Connect a wallet with your email (a 6-digit code arrives by email), then send it about
   $0.10 of USDC on Base, from any wallet or exchange that holds some, to the address it shows.
3. Set `url` to `https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.csv`,
   check that the price shows $0.02 of USDC on Base, and pay. x402scan may not show the
   receipt: check Basescan as above.

**With this repository's example** (`docs/examples/agent_pays.py`). Verified: its code was
read, and the test suite runs it against a fake facilitator on testnet. Not verified: a run
against mainnet. It needs [uv](https://docs.astral.sh/uv/), a small CSV file, and the paying
wallet's private key, which it asks for at a hidden prompt: the key is not shown, not saved in
the shell's history, and never leaves that process (the service never sees it). Run it in your
computer's own Terminal app, not through a chat's Run button, whose output is recorded:

```bash
export AIMART_URL=https://<workspace>--aimart-parquet.modal.run   # your service's address
export AIMART_MAINNET_CONFIRM=I_UNDERSTAND_THIS_SPENDS_REAL_USDC
uv run docs/examples/agent_pays.py small.csv --network mainnet --max-usd 0.02
```

Paste the key when it asks (nothing appears as you paste). Do not type
`export PAYER_PRIVATE_KEY=...`: the shell keeps every typed line, in plain text, in its history
file (`~/.zsh_history` on a Mac).

It makes three payments, $0.027 in all: a conversion ($0.02), an inspect ($0.002) and a query
over MCP ($0.005), and prints each quote before paying it and each settlement's transaction.
How it guards real money: it refuses to run on mainnet without that exact confirmation
sentence; it can sign for Base only; and it refuses any single quote above `--max-usd` (a cap
on each payment, not on their sum). A payment the facilitator rejects ends it with the
reason, and a failed call is not charged.

Coinbase's Agentic Wallet command line (`npx awal`) and its Payments MCP, which this guide
used to suggest, were retired by Coinbase on 2026-11-20: they can no longer pay, and Coinbase
asks that nobody fund them.

### List the service in the CDP Bazaar (optional, mainnet)

Coinbase's CDP facilitator lists a service in its **Bazaar** after it settles a payment for it,
and agentic.market, where agents with Coinbase wallets look for services, shows what the
Bazaar holds. xpay and Primer settle for free but publish no list, so sales through them list
the service nowhere ([discovery.md](../docs/discovery.md)). With a CDP key in the `production`
environment, CDP settles Base first, and xpay, then Primer, take over when CDP does not
answer, fails to verify a payment, or fails to settle one (for the calls after it: a settle is
never repeated elsewhere, so that one call is unpaid). Cost: the first 1,000 settlements a month
are free, then $0.001 each ([economics.md](../docs/economics.md)); past the free ones CDP asks
for a payment method on the CDP account, so add one in the portal (**Billing**) before sales
get near 1,000 a month (see the log lines below). Without the key nothing changes.

The key is a **CDP Secret API key**: an ID and a secret. It lets the service ask CDP to verify
and settle payments; it is not a wallet and cannot move the USDC paid to your payee address.
Never paste it into a chat, a file in the repository or a GitHub variable: only into the two
GitHub secrets below.

1. **Create a CDP account.** Open <https://portal.cdp.coinbase.com> and sign up (an email, or
   your Coinbase account). A project is made for you.
2. **Create the key.** In the portal's left menu choose **API Keys**, then the **Secret API
   Keys** tab, then **Create API key**. Give it a name such as `aimart-facilitator`.
   - Signature algorithm: leave the default, **Ed25519** (ECDSA works too).
   - Permissions: give it the least the form allows. The facilitator needs no wallet, trade or
     transfer permission: Coinbase submits the transfer the payer signed and pays the gas. If
     the form has an IP allowlist, leave it empty: Modal's addresses change.
   - Click **Create**. The portal shows the **API key ID** and the **API key secret** once (it
     may also offer a file download). Keep that page open for step 3.
3. **Put both into GitHub.** In the repository: **Settings → Environments → production →
   Environment secrets → Add environment secret**.
   - Name `AIMART_CDP_API_KEY_ID`; Value: paste the API key ID (a short code like
     `1a2b3c4d-...`). **Add secret**.
   - Again: Name `AIMART_CDP_API_KEY_SECRET`; Value: paste the API key secret (an Ed25519
     secret is one line of 88 characters ending in `==`; an ECDSA one is a block from
     `-----BEGIN EC PRIVATE KEY-----` to `-----END EC PRIVATE KEY-----`: paste all of it).
     From a downloaded file, paste only the value after `"privateKey":`, without the quotes.
     **Add secret**.

   Then close the portal's page. GitHub never shows a secret again; to change one, add it
   again under the same name. Spaces and line breaks around a pasted value are ignored.
4. **Deploy.** **Actions → Deploy to Modal → Run workflow** on `main` (every release after
   this deploys with the key by itself). The deploy checks the two values' shape before
   building (its log says `CDP key (Ed25519) has the right shape: CDP settles Base first`,
   never the values), and the selftest asks CDP, with the key, for its list of networks, which
   moves no money. Its line `selftest ok: {...}` then contains `CDP accepted the key; it lists
   x402 v2 'exact' on eip155:8453`. If CDP refuses the key the run fails at the selftest with
   `CDP refused the API key (HTTP 401)`: the new version is already serving, with Base on xpay
   as before, so no sale is lost; check that the two secrets hold the right pieces (not
   swapped, nothing missing), or create a new key, and run the workflow again.
5. **Make one paid call** as in "Make one paid test call" above: on x402scan (no key to
   handle) or with `docs/examples/agent_pays.py`. A REST call's 402 carries the Bazaar
   description, which the payer's x402 client sends back with the payment; an MCP call's 402
   carries none (and CDP lists only web resources today), so pay a REST route. Each REST route
   is listed after its own first payment settled through CDP: a convert lists `/v1/convert`
   (the example also lists `/v1/inspect`); `/v1/preview` and `/v1/query` follow with their first
   customers. Then, in the app's log on modal.com, search for `extension responses`: a line
   `[x402] extension responses: {"bazaar": ...}` means CDP settled the payment and took the
   description (its `status` says how it went). No such line, but a `facilitator skipped` or
   `facilitator verify failed` WARNING, means the payment went to xpay, unlisted: pay once
   more after 5 minutes.
6. **Confirm the listing**, up to about 15 minutes later. Open, in a browser,
   `https://api.cdp.coinbase.com/platform/v2/x402/discovery/merchant?payTo=<your payee address>`
   (public, no key; the address exactly as `/pricing` shows it): it lists every resource CDP
   holds for your address, and the service's `/v1/convert` should be among them.
   `https://api.cdp.coinbase.com/platform/v2/x402/discovery/search?urlSubstring=aimart-parquet`
   finds it by its address instead. Then search for "parquet" on <https://agentic.market>.
   Not listed after a day: see "Troubleshooting" (section 5).

In the app's log on modal.com, `facilitator refused this service's API key` (ERROR) means CDP
answered 401 or 403 to a verify, a settle or `/supported`: the key was deleted, disabled or
changed. Base then goes to xpay at once (sales continue, unlisted) until the key is fixed and
the service redeployed; only a call CDP had already verified when it started refusing fails,
unpaid. `CDP asks for a payment method on the account` (ERROR) means CDP answered 402: the
account's free settlements are used up, and the key is fine. Base goes to xpay the same way
(the next line, `facilitator dropped: it asks for a payment method`, says so); add a payment
method in the portal, then run Deploy to Modal to bring CDP back. `facilitator settle failed:
the next facilitator verifies and settles the calls after this one` (WARNING) is a settle CDP
did not answer, or one it says failed on its side (a timeout, a 5xx, a 429, its node): that
call is unpaid, the next minute of calls goes to xpay, then CDP is asked again (after the 402
above: not until the next deploy). `facilitator refused to settle and named a transaction:
check it on chain` (WARNING), and `facilitator's second settle of a pending payment did not
settle it` (WARNING), name a transfer CDP broadcast without confirming it: look the
transaction up on basescan.org; if it landed, that payer paid for a call that answered
"failed", so refund it by hand. `CDP facilitator not used: ...` at start names a key that cannot
be read.

To stop using CDP: delete the two secrets and run Deploy to Modal again, and only after that
deploy is green delete the key in the portal (**API Keys → Secret API Keys**).
In the other order nothing is lost either, but each running copy of the
service logs the ERROR line above once.

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
  at `AIMART_ARTIFACT_TTL_S` (60 minutes) and is deleted after that by a sweep that runs when
  the service starts and then at most once a minute while it runs (local files and their GCS
  copies). On Modal the outputs are on a Volume that outlives a stopped container: one that
  expires while the service is stopped is deleted when it next starts, normally within a day ([docs/privacy.md](../docs/privacy.md#when-outputs-are-deleted)). A GCS copy whose local record was lost in an instance restart is caught by a
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
| a job's files in `/tmp` | the upload (≤ 50 MB) + the staged text (18-31% of the CSV) + the output (20-33%) + spill (`sort_by` only, ≤ 300 MiB). A gzip of CSV or TSV is read by DuckDB where it lies: DuckDB reads UTF-8 from it, and other text is transcoded from it once all of it has been counted against the cap, its UTF-8 version on disk while it is read. A gzip DuckDB would read differently from Python is unpacked instead, as at d83c738: more than one member (DuckDB can stop before the last of them, and refuses a later member's header of 32,760 bytes or more), FTEXT, FHCRC, FCOMMENT or zero padding. That gzip, a bz2 and a zip are unpacked (≤ 256 MB, `AIMART_MAX_DECOMPRESSED_BYTES`; a second layer inside, such as a zip of a gzip, too, its compressed file deleted once unpacked), plus a UTF-8 version while one that is not UTF-8 is transcoded, which then replaces the unpacked text. Whatever the reader read is deleted once it returns |
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
- **Modal Starter** is now the host (section 0).
- **Oracle Cloud Always Free (Ampere A1):** up to 4 Arm OCPUs and 24 GB RAM, free indefinitely.
  The release publishes an arm64 image, so `docker run` it on the VM behind Caddy (automatic
  TLS) and set `AIMART_PUBLIC_BASE_URL` to that hostname. You operate the VM yourself: updates,
  firewall, restarts; there is no scale to zero and no deploy workflow for it here.

---

## 5. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Every request answers 404 `modal-http: workspace ... is disabled` (Modal) | The month's credit is spent: $1 without a payment method, $30 with one (section 0, "Cost"). Add a payment method and a workspace budget in Modal's settings, then run **Deploy to Modal** again; or wait for the next month's credit. |
| Deploy fails at "Check inputs": *deploy is not configured* | A required variable is missing; the message lists which. |
| `unauthorized_client ... rejected by the attribute condition` | The token's repository or owner id differs from `setup-gcp.sh`'s `GITHUB_REPO`/`GITHUB_OWNER_ID`; re-run the script with the right values. |
| `Permission 'iam.serviceAccounts.getAccessToken' denied` | The `workloadIdentityUser` binding is missing or names another repository; re-run the script. |
| Revision fails to start: image pull error | The GHCR package is still private (1.6, step 3). |
| Revision fails to start: secret access denied | Runtime SA lacks `secretAccessor` on that secret; re-run the script (with `SECRET_ADMIN_TOKEN=...` if `GCP_SECRET_ADMIN_TOKEN` is set). |
| A setting you changed on the service is gone | The deploy overwrites env vars and secrets (1.4). Set it as a GitHub variable instead. |
| Smoke fails on `payTo` / network | The service advertises a different payee or chain than the variables say. Treat as an incident: an unexpected `payTo` is revenue going elsewhere. |
| Smoke fails on provenance / key fingerprint | `AIMART_SIGNING_KEY_MLDSA` is missing (ephemeral key) or was rotated without updating `SMOKE_SIGNING_KEY_SHA256`. |
| MCP requests rejected with a host error | `AIMART_PUBLIC_BASE_URL` does not match the host being called; the MCP transport allows only that host (and localhost). |
| Paid calls return 503 "facilitator is unreachable" | No configured facilitator answered `GET /supported` with x402 v2 'exact' on a network you accept; nothing is charged, and the next request asks again. The app's log names each one (`facilitator skipped`). Run the probe in "Switching to mainnet", or name others in `AIMART_FACILITATOR_URLS`. One facilitator down alone does not cause this. |
| Service paused | The spend cap was reached (1.2 A). Raise it or wait for the next month. |
| Deploy to Modal fails at the selftest: `CDP refused the API key (HTTP 401)` (or 403) | The two `AIMART_CDP_API_KEY_*` secrets do not hold a key CDP accepts: swapped, a piece missing, or the key deleted in the portal. The new version is serving anyway, with Base on xpay. Add both secrets again, or a new key, and redeploy (section 2, "List the service in the CDP Bazaar"). |
| Settled through CDP, yet not in the Bazaar after a day | The paying client must echo the 402's `bazaar` extension (the x402 SDKs do); only the paid route is listed, not every route; CDP lists asynchronously. Check `https://api.cdp.coinbase.com/platform/v2/x402/discovery/merchant?payTo=<payee>`, then ask CDP's support with the settlement's transaction. |
