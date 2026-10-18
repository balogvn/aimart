"""Machine-readable self-description: how agents and directories find and understand us.

Discovery is the marketing for a product whose customers are programs. Each document here is
generated from the same pricing table and settings the paywall uses, so what we advertise is
exactly what we charge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aimart_parquet import __version__
from aimart_parquet.engine.detect import SUPPORTED_EXTENSIONS
from aimart_parquet.payments.pricing import active_operations

if TYPE_CHECKING:
    from aimart_parquet.config import Settings

DESCRIPTION = (
    "Converts messy CSV, JSON, Excel, SPSS, Stata and SAS files into clean, typed, "
    "schema-validated, zstd-compressed Parquet — then inspects, previews and queries it with "
    "DuckDB SQL. Built for AI agents: no signup, pay per call in USDC via x402."
)


def _tools(settings: Settings) -> list[str]:
    """MCP tool names, from the same table that prices them."""
    return list(dict.fromkeys(op.tool for op in active_operations(settings)))


def pricing(settings: Settings, networks: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "currency": "USDC",
        "protocol": "x402",
        "x402Version": 2,
        "networks": networks,
        "charged": "only on success (HTTP < 400 / MCP isError=false)",
        "operations": [
            {
                "operation": op.key,
                "http": op.route,
                "mcp_tool": op.tool,
                "price": op.price(settings),
                "summary": op.summary,
            }
            for op in active_operations(settings)
        ],
        "free": [
            "GET /",
            "GET /health",
            "GET /pricing",
            "GET /llms.txt",
            "GET /openapi.json",
            "GET /.well-known/x402",
            "GET /.well-known/mcp/server-card.json",
            "GET /.well-known/aimart-signing-key",
            "POST /v1/verify",
            "GET /v1/artifacts/{id}/{filename} (signed link)",
            "MCP initialize, tools/list and the verify_provenance tool",
        ],
    }


def service_index(settings: Settings) -> dict[str, Any]:
    base = settings.public_base_url
    return {
        "name": settings.service_name,
        "version": __version__,
        "description": DESCRIPTION,
        "mcp": f"{base}/mcp",
        "openapi": f"{base}/openapi.json",
        "docs": f"{base}/docs",
        "llms_txt": f"{base}/llms.txt",
        "pricing": f"{base}/pricing",
        "x402_discovery": f"{base}/.well-known/x402",
        "source": "https://github.com/balogvn/aimart",
        "formats": list(SUPPORTED_EXTENSIONS),
    }


def llms_txt(settings: Settings) -> str:
    base = settings.public_base_url
    lines = [
        f"# {settings.service_name}",
        "",
        f"> {DESCRIPTION}",
        "",
        "Every paid call answers HTTP 402 first with a base64 `PAYMENT-REQUIRED` header (x402 v2). Sign it with an "
        "x402 client and retry with `PAYMENT-SIGNATURE`. You are charged only when the call succeeds.",
        "",
        "## Use it",
        "",
        f"- [MCP server (Streamable HTTP)]({base}/mcp): tools {', '.join(_tools(settings))}",
        f"- [OpenAPI]({base}/openapi.json): REST endpoints under /v1",
        f"- [Pricing]({base}/pricing): per-call prices in USDC",
        f"- [x402 discovery]({base}/.well-known/x402): payable resources",
        "",
        "## Operations",
        "",
    ]
    lines += [
        f"- `{op.route}` ({op.price(settings)}): {op.summary}" for op in active_operations(settings)
    ]
    lines += [
        "",
        "## Notes",
        "",
        "- Inputs: `url` (preferred), `content_base64` + `filename`, or `artifact_id` from an earlier convert.",
        "- convert returns an `artifact_id` and a signed `download_url`; artifacts expire after "
        f"{settings.artifact_ttl_s // 60} minutes.",
        f"- Supported extensions: {', '.join(SUPPORTED_EXTENSIONS)} (optionally .gz/.bz2/.zip).",
        "- query accepts one read-only SELECT over table `data`.",
        "",
        "## Optional",
        "",
        "- [Source code](https://github.com/balogvn/aimart): Apache-2.0; run it locally for free as a stdio MCP server",
    ]
    return "\n".join(lines) + "\n"


def mcp_server_card(settings: Settings) -> dict[str, Any]:
    """Static fallback card for directory scanners (Smithery reads /.well-known/mcp/server-card.json)."""
    return {
        "serverInfo": {
            "name": "aimart-parquet",
            "title": settings.service_name,
            "version": __version__,
        },
        "description": DESCRIPTION,
        "transport": {"type": "streamable-http", "url": f"{settings.public_base_url}/mcp"},
        "authentication": {
            "required": False,
            "payment": "x402 per tool call (_meta 'x402/payment')",
        },
        "tools": [
            {"name": op.tool, "description": op.summary, "price": op.price(settings)}
            for op in active_operations(settings)
            if op.key != "convert_upload"
        ],
    }
