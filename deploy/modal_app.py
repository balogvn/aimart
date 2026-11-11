"""AImart Parquet on Modal, the deployment host (deploy/README.md, section 0).

Deploy with ``deploy/modal-deploy.sh`` (``.github/workflows/deploy-modal.yml`` runs it after every
release). The script first writes ``dist/modal/``: ``requirements.txt``, ``uv export`` of
``uv.lock`` with hashes, so the image runs exactly the pins CI tests; and this commit's wheel, so
``/health`` reports the release's version. This file reads them only on the deploying machine:
Modal builds image definitions lazily, and inside a container the deployed definition is used.

Shape, mirroring the Cloud Run service (deploy.yml):

* 1 CPU core, 2 GiB, 4 concurrent requests, at most one container, scale to zero. Modal bills a
  container while it runs: about $0.06 an hour at this size, inside the Starter plan's $30 of
  monthly credits; the workspace budget (README) stops it instead of billing past them.
* Stored outputs on the Volume ``aimart-artifacts`` (background commits every few seconds and a
  final one at shutdown), so a download link outlives the container's scale-down. The Volume's
  version is the workspace's default: nothing here needs hard links on it.
  Each call's temporary files stay on the container's local disk (``AIMART_WORK_DIR``): only the
  finished output is moved to the Volume.
* Modal answers a web request still running after ``HTTP_LIMIT_S`` with a 303 redirect, which a
  paying client must not see. ``RUNTIME_ENV`` keeps the longest paid call (queue wait, one fetch
  allowance, the operation, verify and settle) inside it, well inside the 300 s payment window;
  ``tests/unit/test_modal_deploy.py`` checks the arithmetic.
* Secrets: ``aimart-parquet-keys`` (the ML-DSA signing seed and the download-link HMAC key, made
  once by ``deploy/modal_keys.py``, never printed). Everything else is deploy-time configuration
  from the deploying environment (GitHub variables in CI), passed whole on every deploy: a value
  changed by hand in Modal is replaced by the next deploy, as on Cloud Run.
* The client address is Modal's own (``request.client``), so X-Forwarded-For is not trusted.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import modal

APP_NAME = "aimart-parquet"
LABEL = "aimart-parquet"  # the URL: https://<workspace>--aimart-parquet.modal.run
KEYS_SECRET = "aimart-parquet-keys"  # noqa: S105 - the secret's name, made by modal_keys.py
VOLUME_NAME = "aimart-artifacts"
ARTIFACT_DIR = "/artifacts"
UV_VERSION = "0.13.0"  # as in ci.yml and the Dockerfile
HTTP_LIMIT_S = 150  # Modal's limit for one web request before it redirects (303)
SCALEDOWN_S = 120  # idle seconds before the container stops; each minute up costs ~$0.001

# Settings Modal needs on top of the application defaults; a deploy-time variable of the same
# name wins. Timeouts: 15 s queue + 30 s fetch + 60 s job leaves 45 s of HTTP_LIMIT_S to verify,
# settle and send the response. Plain literals: tests read this dict without importing modal.
RUNTIME_ENV = {
    "AIMART_ARTIFACT_DIR": "/artifacts",  # ARTIFACT_DIR, the Volume's mount
    "AIMART_WORK_DIR": "/tmp/aimart-work",  # noqa: S108 - the container's own local disk
    "AIMART_ARTIFACT_MAX_TOTAL_BYTES": "1073741824",  # 1 GiB: the Volume is not RAM (Cloud Run)
    "AIMART_CONVERT_TIMEOUT_S": "60",
    "AIMART_QUANTUM_TIMEOUT_S": "60",
    "AIMART_FETCH_TIMEOUT_S": "30",
    "AIMART_QUEUE_WAIT_S": "15",
    "AIMART_TRUST_FORWARDED_FOR": "false",
}

# Deploy-time configuration read from the deploying environment, the same names deploy.yml uses.
CONFIG_KEYS = (
    "AIMART_PUBLIC_BASE_URL",
    "AIMART_NETWORK_MODE",
    "AIMART_PAY_TO_EVM",
    "AIMART_PAY_TO_SVM",
    "AIMART_FACILITATOR_URLS",
    "AIMART_ENABLE_QUANTUM",
    "AIMART_JOB_MEMORY_LIMIT_MB",
    "AIMART_ARTIFACT_MAX_TOTAL_BYTES",
)

DIST = Path(__file__).resolve().parent.parent / "dist" / "modal"


def _config() -> dict[str, str | None]:
    """RUNTIME_ENV plus the deploying environment's non-empty values; empty off the deployer."""
    if not modal.is_local():
        return {}
    values: dict[str, str | None] = dict(RUNTIME_ENV)
    # Stripped: a value pasted into a settings page often ends with a line break (the first
    # deploy's wallet address did). modal-deploy.sh trims too; this also covers `modal deploy`.
    values.update({k: v for k in CONFIG_KEYS if (v := os.environ.get(k, "").strip())})
    if not values.get("AIMART_PUBLIC_BASE_URL"):
        raise RuntimeError(
            "Set AIMART_PUBLIC_BASE_URL to https://<workspace>--aimart-parquet.modal.run"
        )
    if not (values.get("AIMART_PAY_TO_EVM") or values.get("AIMART_PAY_TO_SVM")):
        raise RuntimeError("Set AIMART_PAY_TO_EVM (or AIMART_PAY_TO_SVM): the address that is paid")
    return values


def _wheel() -> Path:
    wheels = sorted(DIST.glob("aimart_parquet-*.whl"))
    if len(wheels) == 1:
        return wheels[0]
    if modal.is_local():
        raise RuntimeError(f"expected one wheel in {DIST}; deploy with deploy/modal-deploy.sh")
    return DIST / "aimart_parquet.whl"  # never read inside a container


WHEEL = _wheel()
image = (
    modal.Image.debian_slim(python_version="3.13")
    .env({"PYTHONUNBUFFERED": "1"})
    .uv_pip_install(requirements=[str(DIST / "requirements.txt")], uv_version=UV_VERSION)
    .add_local_file(str(WHEEL), f"/root/{WHEEL.name}", copy=True)
    .uv_pip_install(f"/root/{WHEEL.name}", extra_options="--no-deps", uv_version=UV_VERSION)
)
app = modal.App(APP_NAME, image=image)
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
secrets = [modal.Secret.from_name(KEYS_SECRET), modal.Secret.from_dict(_config())]


@app.function(
    cpu=1.0,
    memory=2048,  # MiB, as on Cloud Run
    timeout=HTTP_LIMIT_S,
    min_containers=0,  # scale to zero
    max_containers=1,  # the cost ceiling's first line, as on Cloud Run
    scaledown_window=SCALEDOWN_S,
    volumes={ARTIFACT_DIR: volume},
    secrets=secrets,
)
@modal.concurrent(max_inputs=4)
@modal.asgi_app(label=LABEL)
def web() -> Any:
    from aimart_parquet.api.app import create_app  # noqa: PLC0415 - exists in the image only

    return create_app()


@app.function(cpu=1.0, memory=2048, timeout=300, secrets=secrets)
async def selftest() -> dict[str, Any]:
    """Convert and read back a small CSV through the isolated job child, then check the memory
    watchdog stops a job, in this image on Modal's hardware. The deploy runs it before the smoke
    test: job isolation (a forkserver child, RSS read from /proc) is what Modal's sandbox could
    break, and no unpaid route exercises it. Uses a scratch store, never the Volume."""
    import base64  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from aimart_parquet.artifacts import build_store  # noqa: PLC0415
    from aimart_parquet.config import Settings  # noqa: PLC0415
    from aimart_parquet.engine.models import ConvertOptions  # noqa: PLC0415
    from aimart_parquet.errors import InputTooLarge  # noqa: PLC0415
    from aimart_parquet.service import Service, SourceRef  # noqa: PLC0415

    def source(rows: int) -> SourceRef:
        csv = b"id,city,amount\n" + b"".join(b"%d,Lagos,%d.50\n" % (i, i) for i in range(rows))
        return SourceRef(content_base64=base64.b64encode(csv).decode(), filename="selftest.csv")

    async def run() -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            return await check(Path(tmp))

    async def check(root: Path) -> dict[str, Any]:
        settings = Settings(artifact_dir=root / "artifacts", work_dir=root / "work")
        if not settings.isolate_jobs:
            raise RuntimeError("isolate_jobs is off: the selftest would not test isolation")
        service = Service(settings, build_store(settings), local_mode=False)
        result = await service.convert(source(1000), ConvertOptions())
        inspected = await service.inspect(SourceRef(artifact_id=result.artifact.id))
        if inspected.rows != 1000:
            raise RuntimeError(f"converted {inspected.rows} rows, expected 1000")
        # The watchdog first looks 0.1 s into a job: 300,000 rows keep the job running past that.
        capped = settings.model_copy(update={"job_memory_limit_mb": 1})
        try:
            await Service(capped, build_store(capped), local_mode=False).convert(
                source(300_000), ConvertOptions()
            )
        except InputTooLarge as exc:
            watchdog = exc.message
            if "memory budget" not in watchdog:  # refused, but by something else (DuckDB's limit)
                raise RuntimeError(f"the watchdog did not stop the job: {watchdog}") from exc
        else:
            raise RuntimeError("a 1 MB job memory cap did not stop the job")
        return {"rows": inspected.rows, "watchdog": watchdog}

    try:
        return await run()
    except Exception as exc:  # noqa: BLE001 - every failure must cross to the deployer
        # Raised as a builtin: the deploying machine has no aimart_parquet to rebuild an
        # AimartError from, and would report a deserialization error instead of this one.
        raise RuntimeError(f"selftest failed: {type(exc).__name__}: {exc}") from None
