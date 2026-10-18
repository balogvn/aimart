"""UNTESTED SKETCH: AImart Parquet on Modal, the documented fallback host (deploy/README.md).

Nothing in CI runs, imports or deploys this file, and it has never been deployed. Treat it as a
starting point that states the intended shape, not as a supported deployment path.

Why Modal at all: the Starter plan includes $30 of compute credits a month, and a workspace spend
limit of $0 makes it stop serving when they run out instead of billing. That is the same "never
pay by surprise" property the Cloud Run spend cap gives.

Shape, mirroring the Cloud Run service:

* the published package from PyPI (pinned), with the quantum and gcs extras, on Modal's own
  Debian + Python 3.13 image. Not the GHCR image: Modal installs its client into the image's
  Python with pip, and our runtime image deliberately ships without pip.
* 1 CPU, 2 GiB, 4 concurrent requests, at most one container, scale to zero.
* configuration from a Modal secret named ``aimart-parquet`` holding the same ``AIMART_*``
  variables as Cloud Run: AIMART_PAY_TO_EVM, AIMART_PUBLIC_BASE_URL (the ``*.modal.run`` URL
  Modal prints on first deploy), AIMART_SIGNING_KEY_MLDSA, AIMART_SIGNING_SECRET,
  AIMART_TRUST_FORWARDED_FOR=true, AIMART_ARTIFACT_MAX_TOTAL_BYTES.

Try it (with the Modal CLI installed and logged in):

    modal secret create aimart-parquet AIMART_PAY_TO_EVM=0x... AIMART_PUBLIC_BASE_URL=https://...
    AIMART_VERSION=0.2.0 modal deploy deploy/modal_app.py
    uv run --script scripts/smoke.py https://<workspace>--aimart-parquet-web.modal.run \
        --expect-pay-to 0x... --expect-network eip155:84532 --require-provenance

Things to check before trusting it: that the forkserver job isolation works inside Modal's
sandbox, that cold starts (the quantum stack imports at boot) fit Modal's startup timeout, and
that Modal's proxy sets X-Forwarded-For / X-Forwarded-Proto the way the rate limiter and the
MCP Host check expect.
"""

from __future__ import annotations

import os
from typing import Any

import modal

# Pin the release; an unpinned install would deploy whatever PyPI has at build time.
VERSION = os.environ.get("AIMART_VERSION", "0.1.0")

image = modal.Image.debian_slim(python_version="3.13").pip_install(
    f"aimart-parquet[quantum,gcs]=={VERSION}"
)

app = modal.App("aimart-parquet", image=image)


@app.function(
    cpu=1.0,
    memory=2048,  # MiB, as on Cloud Run
    timeout=120,
    min_containers=0,  # scale to zero
    max_containers=1,  # the cost ceiling's first line, as on Cloud Run
    secrets=[modal.Secret.from_name("aimart-parquet")],
)
@modal.concurrent(max_inputs=4)
@modal.asgi_app()
def web() -> Any:
    # Imported inside the function: the package exists in the Modal image, not necessarily on
    # the machine running `modal deploy`.
    from aimart_parquet.api.app import create_app  # noqa: PLC0415

    return create_app()
