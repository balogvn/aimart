from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aimart_parquet.errors import InvalidInput, InvalidRequest
from aimart_parquet.quantum.models import PortfolioOptions
from aimart_parquet.quantum.portfolio import MAX_ASSETS, optimize

if TYPE_CHECKING:
    from pathlib import Path


def _brute_force(path: Path, budget: int, q: float) -> list[str]:
    """Independent reference implementation: sort by date, simple returns, exhaustive search."""
    t = pq.read_table(path).sort_by("date")
    names = [c for c in t.column_names if c != "date"]
    p = np.column_stack([t.column(c).to_numpy() for c in names])
    r = p[1:] / p[:-1] - 1
    mu, cov = r.mean(0), np.cov(r, rowvar=False)
    best = min(
        itertools.combinations(range(len(names)), budget),
        key=lambda s: q * cov[np.ix_(s, s)].sum() - mu[list(s)].sum(),
    )
    return [names[i] for i in best]


def test_classical_optimum_matches_an_independent_brute_force(prices_parquet: Path) -> None:
    r = optimize(prices_parquet, PortfolioOptions(budget=3, risk_aversion=0.5))
    assert r["classical_optimum"]["assets"] == _brute_force(prices_parquet, 3, 0.5)


def test_qaoa_is_close_to_optimal_and_enriches_the_optimum(prices_parquet: Path) -> None:
    r = optimize(prices_parquet, PortfolioOptions(budget=3))
    assert len(r["quantum"]["assets"]) == 3  # XY mixer keeps every outcome at the budget
    assert r["approximation_ratio"] >= 0.9
    # Measured behaviour this test pins: QAOA concentrates probability on the optimum well above
    # uniform choice. If an ansatz change loses that, the premium claim is gone — fail loudly.
    assert r["optimum_probability"] > 2 * r["uniform_probability"]


def test_selection_statistics_are_consistent(prices_parquet: Path) -> None:
    r = optimize(prices_parquet, PortfolioOptions(budget=2))
    for sel in (r["quantum"], r["classical_optimum"]):
        assert sel["volatility"] >= 0
        assert sel["objective"] == pytest.approx(
            0.5 * (2**2) * sel["volatility"] ** 2 - 2 * sel["expected_return"], rel=1e-9, abs=1e-12
        )
    assert r["method"]["periods"] == 179
    assert r["method"]["candidates_considered"] <= 5


def test_default_budget_is_half_rounded_up(prices_parquet: Path) -> None:
    assert optimize(prices_parquet, PortfolioOptions(steps=10))["budget"] == 3


def test_returns_mode_uses_columns_as_is(tmp_path: Path) -> None:
    rng = np.random.default_rng(1)
    path = tmp_path / "r.parquet"
    pq.write_table(pa.table({f"r{i}": rng.normal(0.001, 0.01, 50) for i in range(4)}), path)
    r = optimize(path, PortfolioOptions(data_kind="returns", budget=2, steps=10))
    assert r["method"]["periods"] == 50


@pytest.mark.parametrize("budget", [0, 6, 7])
def test_rejects_invalid_budget(prices_parquet: Path, budget: int) -> None:
    with pytest.raises((InvalidRequest, ValueError)):
        optimize(prices_parquet, PortfolioOptions(budget=budget, steps=10))


def test_rejects_too_many_assets(tmp_path: Path) -> None:
    path = tmp_path / "wide.parquet"
    pq.write_table(pa.table({f"a{i}": np.linspace(1, 2, 30) for i in range(MAX_ASSETS + 1)}), path)
    with pytest.raises(InvalidRequest, match=f"at most {MAX_ASSETS}"):
        optimize(path, PortfolioOptions(steps=10))


def test_rejects_too_few_periods(tmp_path: Path) -> None:
    path = tmp_path / "short.parquet"
    pq.write_table(pa.table({"a": [1.0, 1.1, 1.2], "b": [2.0, 2.1, 2.0]}), path)
    with pytest.raises(InvalidInput, match="return periods"):
        optimize(path, PortfolioOptions(budget=1, steps=10))
