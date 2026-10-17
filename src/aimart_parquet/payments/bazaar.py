"""Discovery examples for facilitator catalogs (the x402 Bazaar extension).

Catalogs validate ``input`` against ``input_schema`` strictly and silently skip entries that
fail, so these are small, self-contained JSON Schemas (no ``$ref``) with examples that a test
validates against them. This is what a catalog shows a browsing agent, so it is the pitch.
"""

from __future__ import annotations

from typing import Any

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
        "size_bytes": 182344,
        "download_url": "https://example.run.app/v1/artifacts/Zx3k9Qm2Lr8Tf1Hc5Vb7Nd/sales.parquet?exp=1&sig=x",
    },
    "report": {
        "source_format": "csv",
        "rows": 120000,
        "columns": [
            {"name": "order_date", "type": "date32[day]", "null_count": 0, "rejected_count": 0}
        ],
        "warnings": [],
    },
}

EXAMPLES: dict[str, dict[str, Any]] = {
    "convert": {
        "input": {"url": "https://example.com/sales.csv", "options": {"sort_by": ["order_date"]}},
        "input_schema": {
            "type": "object",
            "properties": {
                **_SOURCE_PROPS,
                "options": {"type": "object", "description": "ConvertOptions; see /openapi.json."},
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
        "input": {"artifact_id": "Zx3k9Qm2Lr8Tf1Hc5Vb7Nd"},
        "input_schema": {"type": "object", "properties": _SOURCE_PROPS},
        "output": {
            "rows": 120000,
            "row_groups": 1,
            "columns": [
                {"name": "region", "type": "string", "null_count": 0, "min": "East", "max": "West"}
            ],
        },
    },
    "preview": {
        "input": {"artifact_id": "Zx3k9Qm2Lr8Tf1Hc5Vb7Nd", "limit": 5, "format": "markdown"},
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
            "columns": ["region", "units"],
            "text": "| region | units |\n|---|---|\n| North | 10 |",
            "row_count": 1,
            "truncated": True,
        },
    },
    "query": {
        "input": {
            "artifact_id": "Zx3k9Qm2Lr8Tf1Hc5Vb7Nd",
            "sql": "SELECT region, sum(units) AS units FROM data GROUP BY 1",
        },
        "input_schema": {
            "type": "object",
            "properties": {
                **_SOURCE_PROPS,
                "sql": {"type": "string", "description": "One DuckDB SELECT over table data."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "format": {"type": "string", "enum": ["json", "markdown", "csv"]},
            },
            "required": ["sql"],
        },
        "output": {
            "columns": ["region", "units"],
            "rows": [["North", 10]],
            "row_count": 1,
            "truncated": False,
        },
    },
}
