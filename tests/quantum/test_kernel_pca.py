from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aimart_parquet.errors import InvalidInput, InvalidRequest
from aimart_parquet.quantum.kernel_pca import compress
from aimart_parquet.quantum.models import QuantumCompressOptions

if TYPE_CHECKING:
    from pathlib import Path


def test_writes_quantum_and_classical_columns_side_by_side(
    features_parquet: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out.parquet"
    r = compress(
        features_parquet, out, QuantumCompressOptions(components=3, keep_columns=["customer_id"])
    )
    table = pq.read_table(out)
    assert table.column_names == ["customer_id", "qpc_1", "qpc_2", "qpc_3", "pc_1", "pc_2", "pc_3"]
    assert r["output_columns"] == table.column_names
    assert table.num_rows == 599  # one row had a null feature
    assert r["method"]["rows_dropped_null"] == 1
    assert all(np.isfinite(table.column("qpc_1").to_numpy()))


def test_keep_columns_are_not_used_as_features(features_parquet: Path, tmp_path: Path) -> None:
    r = compress(
        features_parquet,
        tmp_path / "o.parquet",
        QuantumCompressOptions(keep_columns=["customer_id"]),
    )
    assert r["method"]["features_in"] == 12  # the numeric id column is carried, not embedded


def test_more_features_than_qubits_is_pre_reduced_and_said_so(
    features_parquet: Path, tmp_path: Path
) -> None:
    r = compress(features_parquet, tmp_path / "o.parquet", QuantumCompressOptions(max_qubits=4))
    assert r["method"]["qubits"] == 4
    assert r["method"]["pre_reduction"] is not None
    assert any("classical PCA from" in n for n in r["notes"])


def test_bandwidth_is_chosen_to_avoid_kernel_concentration(
    features_parquet: Path, tmp_path: Path
) -> None:
    auto = compress(features_parquet, tmp_path / "a.parquet", QuantumCompressOptions())
    full = compress(features_parquet, tmp_path / "b.parquet", QuantumCompressOptions(bandwidth=1.0))
    # bandwidth 1.0 concentrates the kernel (rows look unrelated); the auto choice must not.
    assert full["method"]["median_kernel"] < auto["method"]["median_kernel"]
    assert 0.5 <= auto["method"]["median_kernel"] <= 0.95
    assert sum(full["quantum_explained_variance"]) < sum(auto["quantum_explained_variance"])


def test_reports_a_comparable_metric_and_an_honest_note(
    features_parquet: Path, tmp_path: Path
) -> None:
    r = compress(features_parquet, tmp_path / "o.parquet", QuantumCompressOptions())
    for key in ("neighbor_preservation_quantum", "neighbor_preservation_classical"):
        assert 0.0 <= r[key] <= 1.0
    # Linear latent data: classical PCA should win, and the result must say so plainly.
    assert r["neighbor_preservation_classical"] > r["neighbor_preservation_quantum"]
    assert any("classical PCA preserved local structure better" in n for n in r["notes"])


def test_deterministic_for_a_seed(features_parquet: Path, tmp_path: Path) -> None:
    a = compress(features_parquet, tmp_path / "a.parquet", QuantumCompressOptions(seed=3))
    b = compress(features_parquet, tmp_path / "b.parquet", QuantumCompressOptions(seed=3))
    assert a["quantum_explained_variance"] == b["quantum_explained_variance"]
    np.testing.assert_allclose(
        pq.read_table(tmp_path / "a.parquet").column("qpc_1").to_numpy(),
        pq.read_table(tmp_path / "b.parquet").column("qpc_1").to_numpy(),
    )


def test_rejects_non_numeric_feature(features_parquet: Path, tmp_path: Path) -> None:
    with pytest.raises(InvalidRequest, match="not a numeric column"):
        compress(
            features_parquet, tmp_path / "o.parquet", QuantumCompressOptions(columns=["segment"])
        )


def test_rejects_unknown_keep_column(features_parquet: Path, tmp_path: Path) -> None:
    with pytest.raises(InvalidRequest, match="keep_columns not found"):
        compress(
            features_parquet, tmp_path / "o.parquet", QuantumCompressOptions(keep_columns=["nope"])
        )


def test_rejects_too_few_rows(tmp_path: Path) -> None:
    path = tmp_path / "tiny.parquet"
    pq.write_table(pa.table({"a": [1.0, 2.0, 3.0], "b": [3.0, 1.0, 2.0]}), path)
    with pytest.raises(InvalidInput, match="complete rows"):
        compress(path, tmp_path / "o.parquet", QuantumCompressOptions())


def test_rejects_non_parquet_input(tmp_path: Path) -> None:
    path = tmp_path / "x.csv"
    path.write_text("a,b\n1,2\n")
    with pytest.raises(InvalidInput, match="Parquet"):
        compress(path, tmp_path / "o.parquet", QuantumCompressOptions())
