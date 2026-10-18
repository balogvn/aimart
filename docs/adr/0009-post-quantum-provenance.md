# 0009: ML-DSA-65 provenance signatures on every hosted artifact

- Status: accepted
- Date: 2026-10-17 (built in commit d909ef1; recorded 2026-10-18)

## Context and problem statement

An agent that receives a Parquet file from a paid service can ask one question we can answer
cryptographically: "is this exactly what that service produced?" The product discussion asked
for "quantum-resistant" integrity. Which signature scheme, over what, and how do clients
verify?

## Considered options

1. No signatures: callers trust the TLS connection, and that trust does not carry over to a
   file that is passed on.
2. Ed25519: small and fast, but not designed to resist quantum attacks.
3. **ML-DSA-65** (NIST FIPS 204, a module-lattice signature), as implemented by
   `cryptography`. Standard wheels ship it from version 48.
4. A hybrid (Ed25519 + ML-DSA): more robust if ML-DSA is broken, but twice the verification
   code for every client.

## Decision outcome

We chose **ML-DSA-65** (`provenance.py`). `Service._publish` signs a manifest for every hosted
artifact. The manifest holds `artifact_id`, `sha256`, `size_bytes`, `filename`, `operation`,
`created_at`, `service` and `version`. The message is the canonical JSON of the manifest
(sorted keys, separators `(",", ":")`, UTF-8), so any language can re-serialize and verify it.
The signature uses the context string `aimart-parquet/provenance/v1` for domain separation,
so it cannot be replayed as a signature in another protocol.

- Public key: free at `GET /.well-known/aimart-signing-key` (base64 of the raw 1,952-byte key,
  plus the algorithm, context and message format, and an `ephemeral` flag).
- Verification: free at `POST /v1/verify` and in the `verify_provenance` MCP tool. With
  `artifact_id`, the server also re-checks the stored file's hash and size. Offline
  verification is one call to `cryptography` (see the README).
- Key: `AIMART_SIGNING_KEY_MLDSA`, base64 of a 32-byte seed, kept in Secret Manager. If it is
  unset, the server uses a per-boot ephemeral key, logs a warning, and sets
  `ephemeral: true`, so verification fails after a restart.
- Local mode does not sign: there is no service identity to vouch for.

### Consequences

- Good: origin and integrity of every hosted file can be checked, including by third
  parties, long after the call.
- Good: the claim is precise. A valid signature proves origin and integrity, and **nothing
  more**: not that the data is correct, not that cells are free of prompt injection, and no
  quantum hardware is involved.
- Bad: size. An ML-DSA-65 signature is 3,309 bytes (4,412 base64 characters) in every result.
- Bad: `cryptography` must be at least version 48 for ML-DSA in its standard wheels. It
  should be declared as a direct dependency with that floor; today it arrives only
  transitively.
- Bad: rotating the key invalidates old signatures unless old public keys are published too.
  Key rotation is not implemented yet.
