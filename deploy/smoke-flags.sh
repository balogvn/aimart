#!/usr/bin/env bash
# Print, one per line, the scripts/smoke.py flags that state what a production deployment must
# serve, derived from the deploy configuration (the deployer's intent), never from the server.
#
#   flags="$(deploy/smoke-flags.sh)" || exit
#   args=(); while IFS= read -r f; do args+=("$f"); done <<< "$flags"   # bash 3.2 has no mapfile
#   uv run --script scripts/smoke.py "$URL" "${args[@]}"
#
# Used by .github/workflows/deploy.yml (after every deploy) and synthetic.yml (every 6 hours), so
# both check the same expectations, provided both pass every input below (a test checks that:
# the synthetic check once ran without AIMART_ENABLE_QUANTUM and never required the quantum
# tools). It deliberately does not ask the package which network a mode means: the smoke test
# exists to catch the package disagreeing with the deployer.
#
# Inputs (environment):
#   AIMART_NETWORK_MODE      testnet (default) | mainnet
#   AIMART_PAY_TO_EVM        Base payee; at least one payee is required
#   AIMART_PAY_TO_SVM        Solana payee
#   AIMART_PUBLIC_BASE_URL   origin the server must advertise in resource URLs (optional)
#   SMOKE_SIGNING_KEY_SHA256 sha256 of the raw ML-DSA-65 public key to pin (optional)
#   AIMART_ENABLE_QUANTUM    true: the two quantum operations must be served (default: false)
set -euo pipefail

mode="${AIMART_NETWORK_MODE:-testnet}"
case "$mode" in
  testnet)
    evm_network="eip155:84532"                            # Base Sepolia
    svm_network="solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1" # Solana devnet
    ;;
  mainnet)
    evm_network="eip155:8453"                             # Base
    svm_network="solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp" # Solana mainnet-beta
    ;;
  *)
    echo "smoke-flags: AIMART_NETWORK_MODE must be testnet or mainnet, got '${mode}'" >&2
    exit 2
    ;;
esac

evm="${AIMART_PAY_TO_EVM:-}"
svm="${AIMART_PAY_TO_SVM:-}"
if [ -z "$evm" ] && [ -z "$svm" ]; then
  echo "smoke-flags: set AIMART_PAY_TO_EVM and/or AIMART_PAY_TO_SVM (who must be paid)" >&2
  exit 2
fi

if [ -n "$evm" ]; then
  printf '%s\n' --expect-pay-to "$evm" --expect-network "$evm_network"
fi
if [ -n "$svm" ]; then
  printf '%s\n' --expect-accept "${svm_network}=${svm}"
fi
if [ -n "${AIMART_PUBLIC_BASE_URL:-}" ]; then
  printf '%s\n' --expect-base-url "${AIMART_PUBLIC_BASE_URL%/}"
fi
if [ -n "${SMOKE_SIGNING_KEY_SHA256:-}" ]; then
  printf '%s\n' --expect-signing-key-sha256 "$SMOKE_SIGNING_KEY_SHA256"
fi
# A persistent signing key is mandatory in production: its absence is a broken deploy. The quantum
# tools are off by default (AIMART_ENABLE_QUANTUM); when they are switched on, they must be live.
if [ "${AIMART_ENABLE_QUANTUM:-false}" = "true" ]; then
  printf '%s\n' --require-operation quantum_compress --require-operation quantum_portfolio
fi
printf '%s\n' --require-provenance
