#!/usr/bin/env bash
# Deploy AImart Parquet to Modal from this checkout, then prove the deployment works.
#
#   AIMART_PUBLIC_BASE_URL=https://<workspace>--aimart-parquet.modal.run \
#   AIMART_PAY_TO_EVM=0x... deploy/modal-deploy.sh
#
# Used by .github/workflows/deploy-modal.yml after every release, and by hand the same way. Needs
# uv and a Modal token (MODAL_TOKEN_ID/MODAL_TOKEN_SECRET in the environment, or `modal token new`
# once on your machine). The deploy configuration comes from the environment: the names in
# deploy/modal_app.py CONFIG_KEYS, which are the GitHub variables deploy.yml uses.
#
# Steps: build dist/modal/ (the exact pins of uv.lock and this commit's wheel), make the signing
# keys secret if it does not exist yet (deploy/modal_keys.py), deploy, check Modal serves the app
# at AIMART_PUBLIC_BASE_URL, and run the selftest (a real conversion through the isolated job
# child on Modal). The smoke test of the public URL is separate: scripts/smoke.py.
set -euo pipefail

MODAL_VERSION="1.6.2" # pinned; tests/unit/test_modal_deploy.py keeps the workflow in step
cd "$(dirname "$0")/.."

: "${AIMART_PUBLIC_BASE_URL:?set AIMART_PUBLIC_BASE_URL to https://<workspace>--aimart-parquet.modal.run}"
if [ -z "${AIMART_PAY_TO_EVM:-}" ] && [ -z "${AIMART_PAY_TO_SVM:-}" ]; then
  echo "modal-deploy: set AIMART_PAY_TO_EVM (or AIMART_PAY_TO_SVM), the address that is paid" >&2
  exit 2
fi

dist="dist/modal"
rm -rf "$dist"
mkdir -p "$dist"
extras=()
if [ "${AIMART_ENABLE_QUANTUM:-false}" = "true" ]; then extras+=(--extra quantum); fi
# ${extras[@]+...}: bash 3.2 (macOS) treats an empty array as unbound under set -u.
uv export --frozen --no-dev --no-emit-project ${extras[@]+"${extras[@]}"} \
  --format requirements-txt --output-file "$dist/requirements.txt" > /dev/null
uv build --wheel --out-dir "$dist" > /dev/null

with_modal=(uv run --no-project --python 3.13 --with "modal==${MODAL_VERSION}")
"${with_modal[@]}" python deploy/modal_keys.py
"${with_modal[@]}" modal deploy deploy/modal_app.py

served="$("${with_modal[@]}" python -c '
import modal
print(modal.Function.from_name("aimart-parquet", "web").get_web_url() or "")
')"
want="${AIMART_PUBLIC_BASE_URL%/}"
if [ "${served%/}" != "$want" ]; then
  echo "modal-deploy: Modal serves the app at '${served}', but AIMART_PUBLIC_BASE_URL is '${want}'." >&2
  echo "modal-deploy: set AIMART_PUBLIC_BASE_URL to '${served}' and deploy again." >&2
  exit 1
fi
echo "serving at ${served}"

"${with_modal[@]}" modal run deploy/modal_app.py::selftest
