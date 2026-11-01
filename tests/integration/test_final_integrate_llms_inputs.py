"""/llms.txt says which inputs each MCP tool takes, as the tools' own schemas declare them.

Incident (final-validate round 5): /llms.txt said "Inputs: `url` (preferred), `content_base64`
+ `filename`, or `artifact_id`" for every tool, but over MCP only convert_to_parquet takes
content_base64; inspect_parquet, preview_parquet, query_parquet and the quantum tools take
artifact_id or url. README and the agents guide said so already; the text agents read first
did not.
"""

from __future__ import annotations

import re

import pytest

import aimart_testkit as kit
from aimart_parquet.api.schemas import ConvertBody, InspectBody, PreviewBody, QueryBody
from aimart_parquet.service import inputs_among

pytestmark = pytest.mark.anyio


async def test_llms_txt_names_each_tools_inputs(paid_app: kit.AppHarness) -> None:
    text = (await paid_app.http.get("/llms.txt")).text
    [line] = [line for line in text.splitlines() if line.startswith("- Over MCP")]
    stated = {
        tool: re.findall(r"`(\w+)`", inputs)
        for tool, inputs in re.findall(r"`(\w+)` takes ([^;.]+)", line)
    }
    async with kit.mcp_over_http(paid_app.app) as client:
        tools = (await client.list_tools()).tools
    paid = {t.name: inputs_among(t.input_schema["properties"]) for t in tools if t.name in stated}
    assert set(stated) == set(paid)  # every paid tool of this deployment, quantum included
    assert len(stated) == 6
    for tool, inputs in paid.items():
        assert tuple(stated[tool]) == inputs, tool
    assert stated["convert_to_parquet"] == ["url", "content_base64", "artifact_id"]
    assert stated["inspect_parquet"] == ["url", "artifact_id"]


def test_rest_routes_take_all_three() -> None:
    for body in (ConvertBody, InspectBody, PreviewBody, QueryBody):
        assert inputs_among(body.model_fields) == ("url", "content_base64", "artifact_id"), body
