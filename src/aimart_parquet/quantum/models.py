"""Contracts for the premium quantum tools. Field text is what calling agents read."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aimart_parquet.engine.models import ArtifactRef

SIMULATOR_NOTE = (
    "Computed on a quantum circuit simulator (PennyLane); at this size a simulator gives no "
    "speed or accuracy advantage over classical methods, so a classical baseline is included."
)


class QuantumCompressOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: list[str] | None = Field(
        default=None,
        max_length=512,
        description="Numeric feature columns. Default: all numeric columns.",
    )
    keep_columns: list[str] | None = Field(
        default=None,
        max_length=16,
        description="Columns copied unchanged into the output (IDs, keys).",
    )
    components: int = Field(default=3, ge=1, le=8, description="Number of output dimensions.")
    max_qubits: int = Field(
        default=8,
        ge=2,
        le=12,
        description="Qubits in the feature map. More features than this are first reduced with classical PCA.",
    )
    landmarks: int = Field(
        default=256,
        ge=16,
        le=1024,
        description="Rows sampled to fit the kernel (Nystrom approximation).",
    )
    max_rows: int = Field(
        default=100_000, ge=1, le=200_000, description="Rows projected (first N after cleaning)."
    )
    bandwidth: float | None = Field(
        default=None,
        ge=0.01,
        le=1.0,
        description=(
            "Scales the feature-map angles. Large values make every row look unrelated (kernel "
            "concentration); small values approach classical PCA. Default: chosen automatically."
        ),
    )
    seed: int = Field(default=0, ge=0, description="Sampling seed, for reproducible results.")


class CompressMethod(BaseModel):
    feature_map: str = "IQPEmbedding (ZZ-entangling)"
    kernel: str = "fidelity |<psi(x)|psi(y)>|^2"
    device: str
    qubits: int
    features_in: int
    pre_reduction: str | None = Field(
        default=None, description="Classical step applied before the quantum feature map, if any."
    )
    landmarks: int
    rows: int
    rows_dropped_null: int
    bandwidth: float
    median_kernel: float = Field(
        description="Median off-diagonal kernel value on the landmarks (target ~0.75)."
    )


class QuantumCompressResult(BaseModel):
    artifact: ArtifactRef
    quantum_explained_variance: list[float] = Field(
        description=(
            "Share of KERNEL-space variance per quantum component (qpc_1..k). "
            "Not comparable to the classical figure."
        )
    )
    classical_explained_variance: list[float] = Field(
        description="Share of INPUT-space variance per classical PCA component (pc_1..k)."
    )
    neighbor_preservation_quantum: float = Field(
        description="Comparable metric: mean share of each sampled row's 10 nearest neighbors kept by qpc_1..k."
    )
    neighbor_preservation_classical: float = Field(
        description="The same metric for pc_1..k. Higher is better; use whichever representation scores higher."
    )
    output_columns: list[str]
    method: CompressMethod
    duration_ms: int
    notes: list[str] = Field(default_factory=lambda: [SIMULATOR_NOTE])


class PortfolioOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assets: list[str] | None = Field(
        default=None,
        max_length=14,
        description="Asset columns (max 14). Default: all numeric non-date columns.",
    )
    data_kind: Literal["prices", "returns"] = Field(
        default="prices",
        description="Whether columns hold prices (returns are computed) or returns already.",
    )
    budget: int | None = Field(
        default=None, ge=1, description="How many assets to select. Default: half, rounded up."
    )
    risk_aversion: float = Field(
        default=0.5, ge=0, le=1000, description="Weight on variance versus return."
    )
    layers: int = Field(default=2, ge=1, le=4, description="QAOA depth p.")
    steps: int = Field(default=60, ge=10, le=200, description="Optimizer iterations.")
    seed: int = Field(default=0, ge=0)


class Selection(BaseModel):
    assets: list[str]
    objective: float = Field(
        description="risk_aversion * variance - expected return (lower is better)."
    )
    expected_return: float = Field(
        description="Mean per-period return of the equal-weight selection."
    )
    volatility: float = Field(
        description="Per-period standard deviation of the equal-weight selection."
    )
    probability: float | None = Field(
        default=None, description="QAOA output probability of this selection."
    )


class PortfolioMethod(BaseModel):
    algorithm: str = (
        "QAOA with XY (Hamming-weight preserving) mixer, uniform feasible initial state"
    )
    device: str
    qubits: int
    layers: int
    steps: int
    candidates_considered: int = Field(
        description="Most probable QAOA outcomes evaluated to pick the answer."
    )
    periods: int = Field(description="Return observations used.")


class PortfolioResult(BaseModel):
    budget: int
    assets_considered: list[str]
    quantum: Selection
    classical_optimum: Selection = Field(
        description="Exact optimum by exhaustive search over all selections."
    )
    approximation_ratio: float = Field(
        description="1.0 means QAOA found the exact optimum; 0.0 means the worst valid selection."
    )
    optimum_probability: float = Field(
        description="Probability QAOA assigned to the exact optimum."
    )
    uniform_probability: float = Field(
        description="Probability of the optimum under uniform random choice."
    )
    method: PortfolioMethod
    duration_ms: int
    notes: list[str] = Field(default_factory=lambda: [SIMULATOR_NOTE])
