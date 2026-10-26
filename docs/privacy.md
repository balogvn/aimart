# What happens to your data

Every AI-agent buyer we asked raised data handling before price. These are facts about the code,
not aspirations. The same summary is served live at `GET /` and `GET /pricing` (`data_retention`)
and in `GET /llms.txt`, generated from the deployment's settings.

| data | what happens | where in the code |
|---|---|---|
| **Inputs** (URL downloads, base64 content, uploads) | Written to a per-call work directory, created once the call holds a job slot and deleted when the call finishes, whether it succeeds or fails. An upload is held by the web framework while the request is read (in memory, or past 1 MiB in an unnamed temporary file), then copied into that directory. The isolated job's own temporary files (a query's links to its inputs, spill files) are in a per-job directory that is deleted when the job ends, even when it was killed at its deadline or memory cap. | `Service._job()`, `Service._workdir()` and the `finally: shutil.rmtree(...)` in every operation in `src/aimart_parquet/service.py`, where `Service.convert_upload` copies uploads; `run_isolated` in `engine/isolate.py` |
| **Outputs** (converted Parquet, quantum outputs) | Kept as an artifact so you can query or download it. At the TTL (`AIMART_ARTIFACT_TTL_S`, default 3600 s = 60 minutes) it becomes **unreachable**: every lookup checks the expiry, and download links (HMAC-signed, or Cloud Storage V4 signed URLs) expire with it. Deletion follows, normally within minutes (see below). An output of a paid call that is not settled (the call failed, settlement failed, or the client went away) is deleted at once. | the TTL checks, `maybe_sweep` and the bucket-listing sweep in `artifacts.py`; `discard_withheld`, called from `payments/__init__.py` and `payments/mcp_gate.py`; `deploy/setup-gcp.sh` lifecycle rule |
| **Download links** | HMAC-signed (with Cloud Storage, V4 signed URLs) and expire with the artifact. | `artifacts.py` (`sign`, `verify`; `GcsArtifactStore` for signed URLs) |
| **Logs** | Request method, path, status and duration. Demand signals record only a rejected file's *extension* and the text an agent chooses to send via `request_capability`. File contents are not logged, with one exception: when a job fails with an unexpected error (a bug, not a refusal), the error's text is logged so it can be fixed, and it can quote a column name or value. The caller sees only the error's type. | `api/middleware.py` (`AccessLogMiddleware`), `demand.py`, `run_isolated` in `engine/isolate.py` |
| **Training** | Never. The service runs no model on your data. | — |
| **Third parties** | The payment facilitator sees the x402 payment, never your data. URLs you pass are fetched from the hosts you name. | `payments/`, `sources.py` |
| **Local mode** | `aimart-parquet mcp` runs on your machine and never sends files anywhere. | `cli.py` |

## When outputs are deleted

- **Normally, within minutes of the TTL.** A cleanup sweep runs at most once a minute while the
  service has traffic. It deletes expired artifacts from local disk and, on Google Cloud Storage
  deployments, their stored copies.
- **Local disk** on Cloud Run is the instance's memory (`/tmp`), wiped when the instance stops.
- **Worst case, about a day** (Cloud Storage only). A copy whose local record was lost when an
  instance restarted is found by a sweep of the bucket listing (at most every 6 hours, while the
  service has traffic) or, at the latest, by the bucket's 1-day lifecycle rule. Cloud Storage
  applies lifecycle rules asynchronously, so that last deletion can come some hours after the
  copy turns one day old.

## What we do not claim

Things we do **not** claim: we do not offer a data processing agreement, regional data
residency, or certifications today. Do not send data your own obligations forbid sharing with a
third-party processor; use the free local mode instead, which processes everything on your machine.

Artifacts are reachable by anyone holding their unguessable `artifact_id` (144 random bits) or
signed link until they expire. Treat both like temporary passwords.
