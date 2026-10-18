# What happens to your data

Every AI-agent buyer we asked raised data handling before price. These are facts about the code,
not aspirations. The same summary is served live at `GET /` and `GET /pricing` (`data_retention`)
and in `GET /llms.txt`, generated from the deployment's settings.

| data | what happens | where in the code |
|---|---|---|
| **Inputs** (URL downloads, base64 content, uploads) | Written to a per-call temporary directory that is deleted when the call finishes, whether it succeeds or fails. | `Service._workdir()` and the `finally: shutil.rmtree(...)` in every operation in `src/aimart_parquet/service.py`; uploads in `api/app.py` use a `TemporaryDirectory` |
| **Outputs** (converted Parquet, quantum outputs) | Kept as an artifact so you can query or download it, then deleted after the TTL (`AIMART_ARTIFACT_TTL_S`, default 3600 s = 60 minutes). On Google Cloud Storage deployments a 1-day bucket lifecycle rule is the backstop. | `LocalArtifactStore.sweep()` / `maybe_sweep()` in `artifacts.py`; `deploy/setup-gcp.sh` lifecycle rule |
| **Download links** | HMAC-signed and expire with the artifact. | `artifacts.py` (`sign`, `verify`) |
| **Logs** | Request method, path, status and duration. Demand signals record only a rejected file's *extension* and the text an agent chooses to send via `request_capability`. File contents are never logged. | `api/middleware.py` (`AccessLogMiddleware`), `demand.py` |
| **Training** | Never. The service runs no model on your data. | — |
| **Third parties** | The payment facilitator sees the x402 payment, never your data. URLs you pass are fetched from the hosts you name. | `payments/`, `sources.py` |
| **Local mode** | `aimart-parquet mcp` runs on your machine and never sends files anywhere. | `cli.py` |

Things we do **not** claim: we do not offer a data processing agreement, regional data
residency, or certifications today. Do not send data your own obligations forbid sharing with a
third-party processor; use the free local mode instead, which processes everything on your machine.

Artifacts are reachable by anyone holding their unguessable `artifact_id` (144 random bits) or
signed link until they expire. Treat both like temporary passwords.
