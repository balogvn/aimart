"""Quantum-kernel PCA: embed rows with an entangling quantum feature map, reduce in kernel space.

Pipeline: standardize -> (classical PCA pre-reduction if there are more features than qubits)
-> scale to [0, pi] -> IQPEmbedding state vectors (simulated) -> fidelity kernel
K(x, y) = |<psi(x)|psi(y)>|^2 on a random landmark sample -> centred eigendecomposition ->
project every row onto the top components (Nystrom out-of-sample extension).

Classical PCA on the same standardized features is computed alongside and written next to the
quantum components (pc_1..k beside qpc_1..k), with both explained-variance profiles, so the
caller can judge which representation serves it. We make no claim the quantum one is better.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from aimart_parquet.errors import InvalidInput, InvalidRequest
from aimart_parquet.quantum.data import load_matrix, open_schema, pick_columns
from aimart_parquet.quantum.models import QuantumCompressOptions

if TYPE_CHECKING:
    from pathlib import Path

DEVICE = "default.qubit"  # vectorized state-vector simulator: fastest for batched embeddings
_CHUNK = 1024
_MIN_ROWS = 8
_NEIGHBORS = 10
# Measured on synthetic latent-factor data (8 qubits): bandwidth 1.0 gave median kernel 0.05 and
# recovered latent structure at R^2 0.44; bandwidths giving median ~0.75-0.9 reached 0.63. Below
# that, the kernel tends to a constant and the map degenerates toward classical PCA.
_BANDWIDTHS = (1.0, 0.7, 0.5, 0.35, 0.25, 0.18, 0.12, 0.08, 0.05)
_TARGET_MEDIAN = 0.75


def _state_fn(qubits: int) -> Any:
    import pennylane as qml

    dev = qml.device(DEVICE, wires=qubits)

    @qml.qnode(dev)
    def state(x: Any) -> Any:
        qml.IQPEmbedding(x, wires=range(qubits), n_repeats=1)
        return qml.state()

    return state


def _states(fn: Any, rows: np.ndarray) -> np.ndarray:
    out: np.ndarray = np.asarray(fn(rows))
    if rows.shape[0] == 1:  # a single row comes back unbatched
        out = out.reshape(1, -1)
    return out


def _kernel(states_a: np.ndarray, states_b: np.ndarray) -> np.ndarray:
    fidelity: np.ndarray = np.abs(states_a.conj() @ states_b.T) ** 2
    return fidelity


def _neighbor_preservation(reference: np.ndarray, reduced: np.ndarray) -> float:
    """Mean share of each row's nearest neighbors (in ``reference``) kept in ``reduced``."""
    m = reference.shape[0]
    k = min(_NEIGHBORS, m - 1)
    if k < 1:
        return 1.0

    def neighbors(points: np.ndarray) -> np.ndarray:
        sq = (points**2).sum(axis=1)
        dist = sq[:, None] + sq[None, :] - 2 * points @ points.T
        np.fill_diagonal(dist, np.inf)
        return np.argsort(dist, axis=1)[:, :k]

    a, b = neighbors(reference), neighbors(reduced)
    return float(np.mean([len(set(a[i]) & set(b[i])) / k for i in range(m)]))


def _classical_pca(x: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(scores n x k, explained variance ratio of the top k, all eigenvectors)."""
    cov = (x.T @ x) / max(1, x.shape[0] - 1)
    vals, vecs = np.linalg.eigh(cov)
    order = np.argsort(vals)[::-1]
    vals, vecs = np.clip(vals[order], 0, None), vecs[:, order]
    total = vals.sum() or 1.0
    k = min(k, vecs.shape[1])
    return x @ vecs[:, :k], vals[:k] / total, vecs


def compress(path: Path, out_path: Path, options: QuantumCompressOptions) -> dict[str, Any]:
    started = time.monotonic()
    schema = open_schema(path)
    keep = None
    if options.keep_columns:
        names = {f.name.lower(): f.name for f in schema}
        missing = [c for c in options.keep_columns if c.lower() not in names]
        if missing:
            raise InvalidRequest(
                f"keep_columns not found: {missing}.", hint="Call inspect_parquet to list columns."
            )
        keep = [names[c.lower()] for c in options.keep_columns]
    features = pick_columns(schema, options.columns, what="Feature column")
    if options.columns is None and keep:
        features = [f for f in features if f not in keep]  # an ID column is not a feature
        if not features:
            raise InvalidRequest(
                "No numeric feature columns remain after keep_columns.", hint="Pass `columns`."
            )
    matrix, kept, dropped = load_matrix(path, features, extra=keep, max_rows=options.max_rows)
    n, d = matrix.shape
    if n < _MIN_ROWS:
        raise InvalidInput(
            f"Only {n} complete rows remain after dropping {dropped} rows with nulls; need at least {_MIN_ROWS}.",
            hint="Pass columns with fewer missing values.",
        )
    k = options.components
    notes: list[str] = []

    mean, std = matrix.mean(axis=0), matrix.std(axis=0)
    constant = std == 0
    if constant.any():
        notes.append(
            f"{int(constant.sum())} constant feature(s) carry no information and were zeroed"
        )
    x = (matrix - mean) / np.where(constant, 1.0, std)

    classical_scores, classical_ratio, eigvecs = _classical_pca(x, k)
    qubits = min(d, options.max_qubits)
    pre_reduction = None
    z = x
    if d > qubits:
        z = x @ eigvecs[:, :qubits]
        pre_reduction = (
            f"classical PCA from {d} to {qubits} features before the quantum feature map"
        )
        notes.append(pre_reduction)
    lo, hi = z.min(axis=0), z.max(axis=0)
    span = np.where(hi > lo, hi - lo, 1.0)
    unit_angles = (z - lo) / span * np.pi

    rng = np.random.default_rng(options.seed)
    m = min(options.landmarks, n)
    landmark_idx = np.sort(rng.choice(n, size=m, replace=False))
    state = _state_fn(qubits)
    off_diagonal = ~np.eye(m, dtype=bool)
    candidates = (options.bandwidth,) if options.bandwidth is not None else _BANDWIDTHS
    best: tuple[float, float, np.ndarray, np.ndarray] | None = None
    for bw in candidates:
        s = _states(state, unit_angles[landmark_idx] * bw)
        kern = _kernel(s, s)
        median = float(np.median(kern[off_diagonal])) if m > 1 else 1.0
        if best is None or abs(median - _TARGET_MEDIAN) < abs(best[1] - _TARGET_MEDIAN):
            best = (bw, median, s, kern)
    assert best is not None
    bandwidth, median_kernel, s_land, k_ll = best
    angles = unit_angles * bandwidth

    # Centre in feature space, then eigendecompose.
    row_mean = k_ll.mean(axis=0)
    grand = k_ll.mean()
    k_c = k_ll - row_mean[None, :] - row_mean[:, None] + grand
    vals, vecs = np.linalg.eigh(k_c)
    order = np.argsort(vals)[::-1]
    vals, vecs = vals[order], vecs[:, order]
    positive = vals > 1e-10
    usable = min(k, int(positive.sum()))
    if usable < k:
        notes.append(f"kernel has only {usable} informative component(s); the rest are zero")
    total = vals[positive].sum() or 1.0
    quantum_ratio = [float(v / total) for v in vals[:usable]] + [0.0] * (k - usable)
    alphas = np.zeros((m, k))
    if usable:
        alphas[:, :usable] = vecs[:, :usable] / np.sqrt(vals[:usable])

    scores = np.empty((n, k))
    for start in range(0, n, _CHUNK):
        block = _states(state, angles[start : start + _CHUNK])
        k_x = _kernel(block, s_land)
        k_x_c = k_x - row_mean[None, :] - k_x.mean(axis=1, keepdims=True) + grand
        scores[start : start + _CHUNK] = k_x_c @ alphas

    reference = x[landmark_idx]
    np_quantum = _neighbor_preservation(reference, scores[landmark_idx])
    np_classical = _neighbor_preservation(reference, classical_scores[landmark_idx])
    if np_classical > np_quantum:
        notes.append(
            f"classical PCA preserved local structure better here ({np_classical:.2f} vs "
            f"{np_quantum:.2f}); prefer pc_1..{classical_scores.shape[1]} for downstream use"
        )

    columns: dict[str, pa.Array] = {}
    if kept is not None:
        for name in kept.column_names:
            columns[name] = kept.column(name).combine_chunks()
    out_cols: list[str] = []
    for j in range(k):
        columns[f"qpc_{j + 1}"] = pa.array(scores[:, j])
        out_cols.append(f"qpc_{j + 1}")
    for j in range(classical_scores.shape[1]):
        columns[f"pc_{j + 1}"] = pa.array(classical_scores[:, j])
        out_cols.append(f"pc_{j + 1}")
    pq.write_table(pa.table(columns), out_path, compression="zstd", compression_level=3)

    return {
        "quantum_explained_variance": quantum_ratio,
        "classical_explained_variance": [float(v) for v in classical_ratio],
        "neighbor_preservation_quantum": np_quantum,
        "neighbor_preservation_classical": np_classical,
        "output_columns": [*(kept.column_names if kept is not None else []), *out_cols],
        "method": {
            "device": f"{DEVICE} (PennyLane state-vector simulator)",
            "qubits": qubits,
            "features_in": d,
            "pre_reduction": pre_reduction,
            "landmarks": m,
            "rows": n,
            "rows_dropped_null": dropped,
            "bandwidth": bandwidth,
            "median_kernel": median_kernel,
        },
        "duration_ms": int((time.monotonic() - started) * 1000),
        "notes": notes,
    }
