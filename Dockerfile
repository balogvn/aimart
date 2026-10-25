# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
#
# AImart Parquet server image: REST + remote MCP (x402-paid) on $PORT (default 8080).
#
#   docker build -t aimart-parquet:dev .
#   docker run --rm -p 8080:8080 -e AIMART_PAY_TO_EVM=0x... aimart-parquet:dev
#
# Every base is pinned by digest so a rebuild of the same commit pulls the same bytes; Dependabot
# (docker ecosystem) proposes digest bumps as reviewable PRs. Tag text is for humans only.
#
# No HEALTHCHECK: Cloud Run ignores Docker HEALTHCHECK and uses its own startup probe (TCP on
# $PORT by default). Locally, `curl localhost:8080/health`. A HEALTHCHECK would also fork a
# Python process every interval for nothing on a scale-to-zero host.

ARG PYTHON_IMAGE=python:3.13.16-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

# ----------------------------------------------------------------------------- uv binary
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

# ----------------------------------------------------------------------------- build
FROM ${PYTHON_IMAGE} AS build

COPY --from=uv /uv /usr/local/bin/uv

# UV_PYTHON_DOWNLOADS=never: use the digest-pinned interpreter above, never a downloaded one.
# UV_LINK_MODE=copy: the cache is a separate mount, so hardlinks are impossible anyway.
# Bytecode is compiled explicitly below (unchecked-hash), not by uv (timestamp-based).
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.13 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy

WORKDIR /src

# 1) Third-party dependencies only: this layer is reused until uv.lock or pyproject.toml change,
#    which is what makes source-only rebuilds take seconds instead of minutes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project --extra gcs --extra quantum

# 2) The project itself, installed as a regular (non-editable) wheel so the runtime stage needs
#    only /app/.venv and never the source tree.
COPY pyproject.toml uv.lock README.md LICENSE CHANGELOG.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra gcs --extra quantum

# 3) Bytecode, compiled once here. Two reasons it matters:
#    * pennylane's first import takes ~27 s from source; from .pyc it is a few seconds. On a
#      scale-to-zero host that is the difference between a cold start and a timeout.
#    * At runtime the app runs as uid 1000 over root-owned files, so Python could never write a
#      cache itself: without this, every process (and every forkserver child) recompiles.
#    unchecked-hash: the image is immutable, so skip per-import validation entirely. It also
#    survives layer timestamp rewriting (SOURCE_DATE_EPOCH), which would make timestamp-based
#    .pyc files look stale and silently bring the 27 s back.
# pennylane 0.45.1's wheel also installs its own test suite as a top-level `tests` package
# (37 MB, and it shadows any other `tests` import): drop it before compiling.
RUN rm -rf /app/.venv/lib/python3.13/site-packages/tests \
    && python -m compileall -q -j 0 --invalidation-mode unchecked-hash /app/.venv/lib \
    && /app/.venv/bin/aimart-parquet --version

# ----------------------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="aimart-parquet" \
      org.opencontainers.image.description="Pay-per-call MCP server and REST API: messy CSV/JSON/Excel/SPSS/Stata/SAS in, clean Parquet out. Paid in USDC via x402." \
      org.opencontainers.image.source="https://github.com/balogvn/aimart" \
      org.opencontainers.image.url="https://github.com/balogvn/aimart" \
      org.opencontainers.image.documentation="https://github.com/balogvn/aimart#readme" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="Kayode Balogun" \
      io.modelcontextprotocol.server.name="io.github.balogvn/aimart-parquet"

# The official python images ship the stdlib without .pyc files (size), so compile it here for
# the same reason as above. pip is removed: nothing at runtime installs packages, and an unused
# installer is attack surface and scanner noise.
RUN python -m compileall -q -j 0 --invalidation-mode unchecked-hash /usr/local/lib/python3.13 \
    && rm -rf /usr/local/lib/python3.13/site-packages/pip /usr/local/lib/python3.13/site-packages/pip-* \
              /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13 \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --no-create-home --home-dir /nonexistent \
               --shell /usr/sbin/nologin app

# Root-owned and read-only to the app user: a compromised request handler cannot rewrite the
# code that later requests run.
COPY --from=build /app/.venv /app/.venv

# PYTHONDONTWRITEBYTECODE: the tree is read-only to uid 1000, so do not even try.
# PORT: Cloud Run injects its own; 8080 is the container-contract default.
# AIMART_ARTIFACT_DIR: /tmp is writable (and RAM-backed on Cloud Run: keep
# AIMART_ARTIFACT_MAX_TOTAL_BYTES small there, or use AIMART_GCS_BUCKET).
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    AIMART_ARTIFACT_DIR=/tmp/aimart-artifacts

WORKDIR /app
USER 1000:1000
EXPOSE 8080

# Exec form: uvicorn is PID 1 and receives SIGTERM directly for its graceful shutdown.
CMD ["aimart-parquet", "serve"]
