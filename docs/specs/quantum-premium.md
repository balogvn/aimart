# Spec: premium quantum tier

Status: implemented (2026-10-17); **off by default** since 2026-10-18 (`AIMART_ENABLE_QUANTUM=true` to serve it) after a buyer panel of AI agents valued it at $0. Source: the 13 Oct 2026 product discussion, which proposed a
PennyLane-powered premium tier priced at $0.50–$2.00 per call, sitting *behind* the Parquet
conversion (quantum computing does not help the conversion itself).

## Honesty contract — non-negotiable

At the sizes a request handler can simulate (≤ ~16 qubits), simulated quantum circuits offer
**no computational advantage** over classical methods. So every quantum result:

1. Says it ran on a quantum **simulator** and names the device in the result: `default.qubit`
   (vectorized state vectors) for the kernel, `lightning.qubit` (C++, adjoint gradients) for QAOA.
2. Ships with an **exact or standard classical baseline** computed in the same call, plus the
   comparison metric (explained variance; approximation ratio). The agent always receives a
   verified answer, whichever method wins.
3. Never claims speed-ups, "quantum advantage", or security properties it does not have.

The "quantum-resistant tamper-proof metadata via quantum kernels" idea from the discussion is
not a real technique. The real technology for that goal is **post-quantum cryptography**: we
sign artifacts with **ML-DSA-65** (NIST FIPS 204), via the `cryptography` package.

## 1. `quantum_compress_features` — $0.50

Quantum-kernel PCA. REST: `POST /v1/quantum/compress`. MCP tool: `quantum_compress_features`.

Input (`QuantumCompressRequest`):

| field | type | default | notes |
|---|---|---|---|
| `artifact_id` / `url` | str | — | exactly one; must be Parquet (convert first) |
| `columns` | list[str] \| null | all numeric | feature columns |
| `keep_columns` | list[str] \| null | none | carried through unchanged (IDs, keys) |
| `components` | int 1..8 | 3 | output dimensions |
| `max_qubits` | int 2..12 | 8 | features above this are pre-reduced with classical PCA (reported) |
| `landmarks` | int 16..1024 | 256 | rows sampled to fit the kernel (Nyström-style) |
| `max_rows` | int 1..200000 | 100000 | rows projected (first N after dropping nulls) |
| `bandwidth` | float 0.01..1 \| null | auto | angle scale; auto targets a median kernel ≈ 0.75 to avoid kernel concentration |
| `seed` | int | 0 | sampling reproducibility |

Method: standardize features, scale to [0, π], embed each row with PennyLane `IQPEmbedding`
(entangling ZZ feature map) on `q = min(features, max_qubits)` qubits, compute the fidelity
kernel `K(x, y) = |⟨ψ(x)|ψ(y)⟩|²` from simulated state vectors, centre it, eigendecompose on
the landmarks, and project every row (in chunks) onto the top `components`.

Output (`QuantumCompressResult`): a new Parquet artifact with `keep_columns`, `qpc_1..k`
(quantum kernel components) and `pc_1..k` (classical PCA baseline); `quantum_explained_variance`
(kernel space) and `classical_explained_variance` (input space) — not comparable to each other —
plus the comparable `neighbor_preservation_quantum` / `neighbor_preservation_classical` (share of
each sampled row's 10 nearest neighbours kept); `method` (feature_map, qubits, device, landmarks,
rows, features_in, pre_reduction, bandwidth, median_kernel); `duration_ms`; `notes`.

## 2. `quantum_optimize_portfolio` — $1.00

QAOA portfolio selection. REST: `POST /v1/quantum/portfolio`. MCP tool: `quantum_optimize_portfolio`.

Input (`PortfolioRequest`): `artifact_id`/`url`; `assets` (≤ 14 numeric columns, default all
numeric non-date columns); `data_kind` `"prices"|"returns"` (default prices: simple returns are
computed); `budget` k (default ⌈n/2⌉); `risk_aversion` q ≥ 0 (default 0.5); `layers` p 1..4
(default 2); `steps` 10..200 (default 60); `seed`.

Method: μ, Σ from returns; minimize `q·xᵀΣx − μᵀx` over binary x with `Σx = k`, encoded as an
Ising cost Hamiltonian; QAOA from the uniform superposition over valid selections with a
Hamming-weight-preserving XY mixer (no penalty term), p layers on `lightning.qubit`, Adam-optimized;
read the state's bitstring distribution; choose the best feasible bitstring among the most
probable. Classical reference: exhaustive search over all C(n, k) selections (exact).

Output (`PortfolioResult`): `quantum` (selection, objective, probability, expected_return,
volatility), `classical_optimum` (same fields), `approximation_ratio`, `optimum_probability`,
`method` (algorithm, layers, steps, qubits, device), `duration_ms`, `notes`.

## 3. Post-quantum provenance — free

Every artifact the hosted service produces carries `provenance`:

```json
{"algorithm": "ML-DSA-65", "manifest": {"artifact_id": "...", "sha256": "...", "size_bytes": 1,
 "filename": "x.parquet", "operation": "convert", "created_at": "...", "service": "aimart-parquet",
 "version": "0.1.0"}, "signature": "<base64>", "public_key_url": "https://host/.well-known/aimart-signing-key"}
```

The manifest is canonical JSON (sorted keys, no whitespace). Free endpoints:
`GET /.well-known/aimart-signing-key` (base64 public key + algorithm) and `POST /v1/verify`
(manifest + signature, optional `artifact_id` to re-hash the stored file). MCP tool
`verify_provenance` (free). Key: `AIMART_SIGNING_KEY_MLDSA` = base64 of the 32-byte seed; unset →
ephemeral per-boot key with a loud warning (verification breaks across restarts).

## Settings

`enable_quantum=False`, `price_quantum_compress="$0.50"`, `price_quantum_portfolio="$1.00"`,
`quantum_timeout_s=90`, `signing_key_mldsa` (secret),
`hosted_url` (local-mode upsell hint). Quantum tools register only if the `quantum` extra is
installed. They run in the isolated worker process like other heavy jobs.

## Traffic

Local stdio mode stays free. Local responses carry a one-line `hint` pointing at the hosted
endpoint (`hosted_url`) for premium tools — the discussion's "free local, paid hosted" funnel.
