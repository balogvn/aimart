"""/v1/convert over the real app, in the isolated child: nested keys that differ only by case.

The caller pays for a 200, so a conversion that silently dropped every ``K`` value of
``{"a": {"k": 1, "K": 2}}`` charged for data it threw away. Assert what the client receives:
both columns, both values (read back through /v1/query), and the warning that says why.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import aimart_testkit as kit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

HEADERS = [
    {"id": 1, "headers": {"Content-Type": "text/csv", "content-type": "text/plain"}},
    {"id": 2, "headers": {"Content-Type": "application/json", "content-type": "x"}},
]


@pytest.fixture
async def isolated_app(make_settings: Callable[..., Settings]) -> AsyncIterator[kit.AppHarness]:
    async with kit.running_app(make_settings(isolate_jobs=True), paid=False) as harness:
        yield harness


@pytest.mark.parametrize(
    ("filename", "data"),
    [
        pytest.param("h.json", json.dumps(HEADERS), id="json"),
        pytest.param("h.ndjson", "".join(json.dumps(r) + "\n" for r in HEADERS), id="ndjson"),
    ],
)
async def test_convert_keeps_both_case_variants(
    isolated_app: kit.AppHarness, filename: str, data: str
) -> None:
    r = await isolated_app.http.post(
        "/v1/convert",
        json={
            "content_base64": kit.b64(data.encode()),
            "filename": filename,
            "options": {"flatten_nested": True},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    names = [c["name"] for c in body["report"]["columns"]]
    assert names == ["id", "headers_content_type", "headers_content_type_2"]
    assert any(
        "headers.content-type is now headers.content-type_2" in w
        for w in body["report"]["warnings"]
    )
    q = await isolated_app.http.post(
        "/v1/query",
        json={
            "artifact_id": body["artifact"]["id"],
            "sql": "SELECT headers_content_type, headers_content_type_2 FROM data ORDER BY id",
        },
    )
    assert q.status_code == 200, q.text
    assert q.json()["rows"] == [["text/csv", "text/plain"], ["application/json", "x"]]
