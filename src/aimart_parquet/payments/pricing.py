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
)

BY_TOOL: dict[str, Operation] = {}
for _op in OPERATIONS:
    BY_TOOL.setdefault(_op.tool, _op)


def usd_to_atomic(price: str, decimals: int = 6) -> int:
    """'$0.01' -> 10000 (USDC has 6 decimals). Exact: no floats touch money."""
    text = price.removeprefix("$")
    whole, _, frac = text.partition(".")
    if len(frac) > decimals:
        msg = f"{price} has more precision than {decimals} decimals"
        raise ValueError(msg)
    return int(int(whole or "0") * 10**decimals + int((frac or "0").ljust(decimals, "0")))
