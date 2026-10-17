"""Fixtures for the premium quantum tier. Self-contained: no dependency on the root conftest."""

from __future__ import annotations

import base64
import datetime as dt
import secrets
from typing import TYPE_CHECKING, Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

if TYPE_CHECKING:
    from pathlib import Path

pytest.importorskip("pennylane", reason="quantum extra not installed")

PAY_TO = "0x209693Bc6afc0C5328bA36FaF03C514EF312287C"


@pytest.fixture
def features_parquet(tmp_path: Path) -> Path:
    """600 rows, 12 features driven by 3 latent factors, an id column and one null cell."""
    rng = np.random.default_rng(0)
    latent = rng.normal(size=(600, 3))
    x = latent @ rng.normal(size=(3, 12)) + 0.3 * rng.normal(size=(600, 12))
    x[7, 2] = np.nan
    cols: dict[str, Any] = {f"f{i}": x[:, i] for i in range(12)}
    cols["customer_id"] = np.arange(600)
    cols["segment"] = ["a" if i % 2 else "b" for i in range(600)]
    path = tmp_path / "features.parquet"
    pq.write_table(pa.table(cols), path)
    return path


@pytest.fixture
def prices_parquet(tmp_path: Path) -> Path:
    """6 assets x 180 days, rows deliberately shuffled (the date column must restore order)."""
    rng = np.random.default_rng(7)
    days = [dt.date(2025, 1, 1) + dt.timedelta(days=d) for d in range(180)]
    prices = 100 * np.cumprod(1 + rng.normal(0.0005, 0.012, size=(180, 6)), axis=0)
    order = rng.permutation(180)
    cols: dict[str, Any] = {"date": pa.array([days[i] for i in order])}
    cols.update({f"asset_{j}": prices[order, j] for j in range(6)})
    path = tmp_path / "prices.parquet"
    pq.write_table(pa.table(cols), path)
    return path


@pytest.fixture
def signing_seed() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()
