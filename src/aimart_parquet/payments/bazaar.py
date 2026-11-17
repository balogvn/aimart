"""Discovery examples for facilitator catalogs (the x402 Bazaar extension).

Catalogs validate ``input`` against ``input_schema`` strictly and silently skip entries that
fail, so these are small, self-contained JSON Schemas (no ``$ref``) with examples that a test
validates against them. This is what a catalog shows a browsing agent, so it is the pitch.

The examples are real calls. Their inputs are sample files in this repository
(``docs/examples/sales.csv``, and ``sales.parquet``, its conversion), fetched from GitHub, and
their outputs are what the service answers for them, ids, links and byte counts aside
(tests/integration/test_bazaar_examples_are_real.py). Catalogs replay examples (x402gle pays
for each route and grades what comes back) and agents copy them: they used to name
https://example.com/sales.csv and an artifact_id that never existed, so every replay failed.
"""

from __future__ import annotations

from typing import Any

# Where the sample inputs are published: this repository's main branch, as raw files.
SAMPLES = "https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples"
# Stands for the deployment's own address in an output example (see example_output).
BASE_URL_SLOT = "{public_base_url}"

_SOURCE_PROPS: dict[str, Any] = {
    "url": {"type": "string", "description": "Public http(s) URL of the input file."},
    "content_base64": {"type": "string", "description": "Raw file bytes, base64-encoded."},
    "filename": {"type": "string", "description": "Original filename, e.g. survey.sav."},
    "artifact_id": {"type": "string", "description": "artifact_id from a previous convert."},
}

_REPORT_EXAMPLE = {
    "artifact": {
        "id": "Zx3k9Qm2Lr8Tf1Hc5Vb7Nd",
        "filename": "sales.parquet",
        "size_bytes": 1022,
        "download_url": (
            f"{BASE_URL_SLOT}/v1/artifacts/Zx3k9Qm2Lr8Tf1Hc5Vb7Nd/sales.parquet"
            "?exp=1794869547&sig=c91f178f91820a3ca30777602d32193e"
        ),
    },
    "report": {
        "source_format": "csv",
        "rows": 6,
        "columns": [
            {"name": "order_id", "type": "int64", "null_count": 0, "rejected_count": 0},
            {"name": "order_date", "type": "date32[day]", "null_count": 0, "rejected_count": 0},
            {"name": "region", "type": "string", "null_count": 0, "rejected_count": 0},
            {"name": "units", "type": "int64", "null_count": 0, "rejected_count": 0},
            {"name": "unit_price", "type": "double", "null_count": 0, "rejected_count": 0},
        ],
        "warnings": [],
    },
}

EXAMPLES: dict[str, dict[str, Any]] = {
    "convert": {
        "input": {"url": f"{SAMPLES}/sales.csv", "options": {"sort_by": ["order_date"]}},
        "input_schema": {
            "type": "object",
            "properties": {
                **_SOURCE_PROPS,
                "options": {
                    "type": "object",
                    "description": (
                        "ConvertOptions, e.g. sheet, or pdf_pages and pdf_table for tables in a "
                        "digital (not scanned) PDF; see /openapi.json."
                    ),
                },
            },
        },
        "output": _REPORT_EXAMPLE,
    },
    "convert_upload": {
        "input": {"options": '{"sort_by": ["order_date"]}'},
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "The data file (multipart form field)."},
                "options": {"type": "string", "description": "ConvertOptions as a JSON string."},
            },
        },
        "output": _REPORT_EXAMPLE,
    },
    "inspect": {
        "input": {"url": f"{SAMPLES}/sales.parquet"},
        "input_schema": {"type": "object", "properties": _SOURCE_PROPS},
        "output": {
            "rows": 6,
            "row_groups": 1,
            "columns": [
                {"name": "order_id", "type": "int64", "null_count": 0, "min": 1001, "max": 1006},
                {
                    "name": "order_date",
                    "type": "date32[day]",
                    "null_count": 0,
                    "min": "2026-01-05",
                    "max": "2026-01-08",
                },
                {"name": "region", "type": "string", "null_count": 0, "min": "East", "max": "West"},
                {"name": "units", "type": "int64", "null_count": 0, "min": 3, "max": 12},
                {
                    "name": "unit_price",
                    "type": "double",
                    "null_count": 0,
                    "min": 19.99,
                    "max": 104.0,
                },
            ],
        },
    },
    "preview": {
        "input": {"url": f"{SAMPLES}/sales.parquet", "limit": 2, "format": "markdown"},
        "input_schema": {
            "type": "object",
            "properties": {
                **_SOURCE_PROPS,
                "columns": {"type": "array", "items": {"type": "string"}},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "format": {"type": "string", "enum": ["json", "markdown", "csv"]},
            },
        },
        "output": {
            "columns": ["order_id", "order_date", "region", "units", "unit_price"],
            "text": (
                "| order_id | order_date | region | units | unit_price |\n"
                "|---|---|---|---|---|\n"
                "| 1001 | 2026-01-05 | North | 12 | 19.99 |\n"
                "| 1002 | 2026-01-05 | South | 5 | 24.5 |"
            ),
            "row_count": 2,
            "truncated": True,
            "total_rows": 6,
        },
    },
    "query": {
        "input": {
            "url": f"{SAMPLES}/sales.parquet",
            "sql": "SELECT region, sum(units) AS units FROM data GROUP BY region ORDER BY region",
        },
        "input_schema": {
            "type": "object",
            "properties": {
                **_SOURCE_PROPS,
                "sql": {
                    "type": "string",
                    "description": "One DuckDB SELECT over table data, joined with any `tables`.",
                },
                "tables": {
                    "type": "object",
                    "description": (
                        "Up to 7 more tables to join, by name: "
                        '{"orders": {"artifact_id": "..."}}. The top-level input is `data`.'
                    ),
                    "maxProperties": 7,
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "artifact_id": _SOURCE_PROPS["artifact_id"],
                            "url": {
                                "type": "string",
                                "description": "Public http(s) URL of a Parquet file.",
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "format": {"type": "string", "enum": ["json", "markdown", "csv"]},
            },
            "required": ["sql"],
        },
        "output": {
            "columns": ["region", "units"],
            "rows": [["East", 7], ["North", 15], ["South", 13], ["West", 10]],
            "row_count": 4,
            "truncated": False,
        },
    },
    "quantum_compress": {
        "input": {
            "artifact_id": "Zx3k9Qm2Lr8Tf1Hc5Vb7Nd",
            "options": {"components": 3, "keep_columns": ["customer_id"]},
        },
        "input_schema": {
            "type": "object",
            "properties": {
                "artifact_id": _SOURCE_PROPS["artifact_id"],
                "url": _SOURCE_PROPS["url"],
                "options": {
                    "type": "object",
                    "description": "QuantumCompressOptions; see /openapi.json.",
                    "properties": {
                        "components": {"type": "integer", "minimum": 1, "maximum": 8},
                        "keep_columns": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
        "output": {
            "artifact": {"id": "Qk3k9Qm2Lr8Tf1Hc5Vb7Nd", "filename": "features_qpca.parquet"},
            "quantum_explained_variance": [0.24, 0.09, 0.08],
            "classical_explained_variance": [0.41, 0.29, 0.22],
            "neighbor_preservation_quantum": 0.26,
            "neighbor_preservation_classical": 0.77,
        },
    },
    "quantum_portfolio": {
        "input": {
            "artifact_id": "Zx3k9Qm2Lr8Tf1Hc5Vb7Nd",
            "options": {"budget": 3, "risk_aversion": 0.5},
        },
        "input_schema": {
            "type": "object",
            "properties": {
                "artifact_id": _SOURCE_PROPS["artifact_id"],
                "url": _SOURCE_PROPS["url"],
                "options": {
                    "type": "object",
                    "description": "PortfolioOptions; see /openapi.json.",
                    "properties": {
                        "budget": {"type": "integer", "minimum": 1},
                        "risk_aversion": {"type": "number", "minimum": 0},
                    },
                },
            },
        },
        "output": {
            "quantum": {"assets": ["A2", "A4", "A7"], "objective": -0.0039, "probability": 0.18},
            "classical_optimum": {"assets": ["A2", "A4", "A7"], "objective": -0.0039},
            "approximation_ratio": 1.0,
        },
    },
}


def example_output(key: str, public_base_url: str) -> Any:
    """``key``'s output example as this deployment would send it: links on its own address.

    The convert example's download link stood on example.run.app, a host that is not the
    service, in every catalog that showed it.
    """
    return _on_base(EXAMPLES[key]["output"], public_base_url.rstrip("/"))


def _on_base(value: Any, base: str) -> Any:
    if isinstance(value, str):
        return value.replace(BASE_URL_SLOT, base)
    if isinstance(value, dict):
        return {k: _on_base(v, base) for k, v in value.items()}
    if isinstance(value, list):
        return [_on_base(v, base) for v in value]
    return value
