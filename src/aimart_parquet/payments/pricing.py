"""The one table of what costs what.

The REST paywall, the MCP paywall, ``/pricing``, ``/.well-known/x402`` and the tool
descriptions all read prices from here, so they cannot drift apart: a route priced in one place
and free in another is a revenue leak nobody notices until they read the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aimart_parquet.config import Settings


@dataclass(frozen=True)
class Operation:
    key: str
    tool: str
    method: str
    path: str
    price_setting: str
    summary: str
    requires: str | None = None  # optional extra the operation needs ("quantum")

    def price(self, settings: Settings) -> str:
        return str(getattr(settings, self.price_setting))

    @property
    def route(self) -> str:
        return f"{self.method} {self.path}"


OPERATIONS: tuple[Operation, ...] = (
    Operation(
        "convert",
        "convert_to_parquet",
        "POST",
        "/v1/convert",
        "price_convert",
        "Convert a messy CSV/JSON/Excel/SPSS/Stata/SAS file (by URL or base64) to clean Parquet.",
    ),
    Operation(
        "convert_upload",
        "convert_to_parquet",
        "POST",
        "/v1/convert/upload",
        "price_convert",
        "Convert an uploaded (multipart) data file to clean Parquet.",
    ),
    Operation(
        "inspect",
        "inspect_parquet",
        "POST",
        "/v1/inspect",
        "price_inspect",
        "Describe a Parquet file's schema, null counts, ranges and labels without reading rows.",
    ),
    Operation(
        "preview",
        "preview_parquet",
        "POST",
        "/v1/preview",
        "price_preview",
        "Return a slice of Parquet rows as JSON, Markdown or CSV.",
    ),
    Operation(
        "query",
        "query_parquet",
        "POST",
        "/v1/query",
        "price_query",
        "Run one read-only DuckDB SELECT over a Parquet file (table `data`).",
    ),
    Operation(
        "quantum_compress",
        "quantum_compress_features",
        "POST",
        "/v1/quantum/compress",
        "price_quantum_compress",
        "Premium: quantum-kernel PCA (PennyLane, simulated) with a classical PCA baseline.",
        requires="quantum",
    ),
    Operation(
        "quantum_portfolio",
        "quantum_optimize_portfolio",
        "POST",
        "/v1/quantum/portfolio",
        "price_quantum_portfolio",
        "Premium: QAOA portfolio selection (PennyLane, simulated) verified by an exact classical solver.",
        requires="quantum",
    ),
)


def active_operations() -> tuple[Operation, ...]:
    """Operations this installation can actually serve — what we advertise and charge for.

    Advertising a premium tool whose extra is not installed would take a verification, fail
    the call, and teach agents that the listing lies.
    """
    from aimart_parquet import quantum  # noqa: PLC0415 - keeps pricing import-light

    has_quantum = quantum.available()
    return tuple(op for op in OPERATIONS if op.requires != "quantum" or has_quantum)


BY_TOOL: dict[str, Operation] = {}
for _op in OPERATIONS:
    BY_TOOL.setdefault(_op.tool, _op)
BY_KEY: dict[str, Operation] = {op.key: op for op in OPERATIONS}


def usd_to_atomic(price: str, decimals: int = 6) -> int:
    """'$0.01' -> 10000 (USDC has 6 decimals). Exact: no floats touch money."""
    text = price.removeprefix("$")
    whole, _, frac = text.partition(".")
    if len(frac) > decimals:
        msg = f"{price} has more precision than {decimals} decimals"
        raise ValueError(msg)
    return int(int(whole or "0") * 10**decimals + int((frac or "0").ljust(decimals, "0")))
