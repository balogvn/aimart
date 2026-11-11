#!/usr/bin/env bash
# Deploy AImart Parquet to Modal from this checkout, then prove the deployment works.
#
#   AIMART_PUBLIC_BASE_URL=https://<workspace>--aimart-parquet.<region>.modal.run \
#   AIMART_PAY_TO_EVM=0x... deploy/modal-deploy.sh
#
#   deploy/modal-deploy.sh --check    # check the inputs only: nothing is built, Modal is not called
#   deploy/modal-deploy.sh --logs     # the deployed app's recent log lines, without the access log
#
# AIMART_PUBLIC_BASE_URL is the address Modal serves the app at. It is not known before the first
# deploy (a workspace's addresses may carry a region, e.g. ...us-east.modal.run): that deploy
# stops after the selftest and prints the address to set.
#
# Used by .github/workflows/deploy-modal.yml after every release, and by hand the same way. Needs
# uv and a Modal token (MODAL_TOKEN_ID/MODAL_TOKEN_SECRET in the environment, or `modal token new`
# once on your machine). The deploy configuration comes from the environment: the names in
# deploy/modal_app.py CONFIG_KEYS, which are the GitHub variables deploy.yml uses.
#
# Steps: check the inputs, build dist/modal/ (the exact pins of uv.lock and this commit's wheel),
# make the signing keys secret if it does not exist yet (deploy/modal_keys.py), deploy, run the
# selftest (a real conversion through the isolated job child on Modal), and check Modal serves
# the app at AIMART_PUBLIC_BASE_URL. The smoke test of the public URL is separate: scripts/smoke.py.
set -euo pipefail

# Pinned; tests/unit/test_modal_deploy.py keeps modal_keys.py in step. The client must know the
# workspace's image builder version: 1.6.2 stopped at 2025.06 and a workspace made in November
# 2026 builds with 2026.10 ("This version of the modal client supports ...": raise this pin).
MODAL_VERSION="1.6.3"
cd "$(dirname "$0")/.."

fail() {
  echo "modal-deploy: $*" >&2
  exit 2
}

# A value pasted into a settings page often ends with a line break or a space: the first deploy's
# wallet address ended with CR LF. Every input is trimmed here, before anything reads it.
trim() {
  local v="${1:-}"
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  printf '%s' "$v"
}
for name in $(compgen -e | grep -E '^(AIMART_|MODAL_TOKEN_)' || true); do
  export "$name=$(trim "$(printenv "$name")")"
done

url="${AIMART_PUBLIC_BASE_URL:-}"
url_re='^https://[^[:space:]/]+/?$'
[ -n "$url" ] || fail "set AIMART_PUBLIC_BASE_URL to the address Modal serves the app at, like https://<workspace>--aimart-parquet.modal.run (the first deploy prints the exact one)"
[[ "$url" =~ $url_re ]] \
  || fail "AIMART_PUBLIC_BASE_URL must be an https address with nothing after the host, like https://<workspace>--aimart-parquet.modal.run"

evm="${AIMART_PAY_TO_EVM:-}"
svm="${AIMART_PAY_TO_SVM:-}"
evm_re='^0x[0-9a-fA-F]{40}$'
[ -n "$evm" ] || [ -n "$svm" ] \
  || fail "set AIMART_PAY_TO_EVM (or AIMART_PAY_TO_SVM), the address that is paid"
if [ -n "$evm" ] && ! [[ "$evm" =~ $evm_re ]]; then
  fail "AIMART_PAY_TO_EVM must be 0x followed by 40 characters from 0-9 and a-f; it has ${#evm} characters in all, not 42"
fi

# Modal answers any wrong pair with "Token validation failed", which does not say what is wrong.
# These checks do, and never print either value.
check_token() { # name, value, its prefix, the other one's prefix
  local name="$1" value="$2" want="$3" other="$4"
  case "$value" in
    *[[:space:]]*)
      fail "$name holds more than the token: paste only the piece that starts with ${want}- (no spaces, no 'modal token set', no line breaks)"
      ;;
    "${want}-"?*) ;;
    "${other}-"*)
      fail "MODAL_TOKEN_ID and MODAL_TOKEN_SECRET look swapped: the ID starts with ak-, the secret starts with as-"
      ;;
    wk-* | ws-*)
      fail "$name is a Proxy Auth Token (wk-/ws-). Create an API token instead: Modal's settings, API Tokens, New Token"
      ;;
    *)
      fail "$name does not look like a Modal API token: it must start with ${want}-"
      ;;
  esac
}
token_id="${MODAL_TOKEN_ID:-}"
token_secret="${MODAL_TOKEN_SECRET:-}"
if [ -n "$token_id" ] || [ -n "$token_secret" ]; then
  { [ -n "$token_id" ] && [ -n "$token_secret" ]; } \
    || fail "set both MODAL_TOKEN_ID (it starts with ak-) and MODAL_TOKEN_SECRET (it starts with as-)"
  check_token MODAL_TOKEN_ID "$token_id" ak as
  check_token MODAL_TOKEN_SECRET "$token_secret" as ak
  token_note="token pair has the right shape"
elif [ "${GITHUB_ACTIONS:-}" = "true" ]; then
  fail "the production environment has no MODAL_TOKEN_ID and MODAL_TOKEN_SECRET secrets (deploy/README.md, section 0.2)"
else
  token_note="token from your modal profile"
fi

echo "modal-deploy: inputs look right: payee ${evm:-$svm}, URL ${url%/}, ${token_note}"
if [ "${1:-}" = "--check" ]; then exit 0; fi

with_modal=(uv run --no-project --python 3.13 --with "modal==${MODAL_VERSION}")

if [ "${1:-}" = "--logs" ]; then
  # What the app logged lately (an error's type and frames, a crash at start). Without the access
  # log: other callers' request paths have no place in a public CI log.
  "${with_modal[@]}" modal app logs aimart-parquet --since 20m --tail 500 \
    | grep -v '"aimart.access"' | tail -n 150 || true
  exit 0
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

"${with_modal[@]}" python deploy/modal_keys.py
"${with_modal[@]}" modal deploy deploy/modal_app.py

# The deployed selftest, not `modal run`: it tests the app that was just deployed, and a failure
# arrives here as one line ("selftest failed: ...").
"${with_modal[@]}" python -c '
import modal
print("selftest ok:", modal.Function.from_name("aimart-parquet", "selftest").remote())
'

served="$("${with_modal[@]}" python -c '
import modal
print(modal.Function.from_name("aimart-parquet", "web").get_web_url() or "")
')"
want="${url%/}"
if [ "${served%/}" != "$want" ]; then
  echo "modal-deploy: Modal serves the app at '${served}', but AIMART_PUBLIC_BASE_URL is '${want}'." >&2
  echo "modal-deploy: set AIMART_PUBLIC_BASE_URL (and PRODUCTION_URL) to '${served}' and deploy again:" >&2
  echo "modal-deploy: the service names itself by that address in every price quote and download link." >&2
  exit 1
fi
echo "serving at ${served}"
