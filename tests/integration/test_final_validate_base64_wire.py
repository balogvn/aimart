"""``content_base64`` over the wire: the bytes converted are the bytes sent, or nothing is quoted.

The decoder used to drop every character outside the standard alphabet. URL-safe base64 (a
5-row CSV with 4 '-' or '_' in its encoding) was converted, answered 200 and settled, on REST
and MCP, as 1 row of garbage: the caller paid for data built from bytes it never sent. Now the
URL-safe alphabet is read as what it means, and a string with any other stray character can
only fail, so it is refused before the price is quoted.
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import pyarrow.parquet as pq
import pytest

import aimart_testkit as kit

pytestmark = pytest.mark.anyio

ROWS = [(i, f"café ¿{i * 7}?", i * 3.5) for i in range(5)]
CSV = ("id,label,value\n" + "".join(f"{i},{label},{value}\n" for i, label, value in ROWS)).encode()
URLSAFE = base64.urlsafe_b64encode(CSV).decode()
EXPECTED = [{"id": i, "label": label, "value": value} for i, label, value in ROWS]


def test_the_sample_is_the_one_that_was_silently_misread() -> None:
    marks = sum(URLSAFE.count(c) for c in "-_")
    assert marks
    assert marks % 4 == 0  # dropping them left a valid length: no error, just other bytes
    assert base64.b64decode(URLSAFE, validate=False) != CSV


async def _downloaded(h: kit.AppHarness, artifact: dict[str, Any]) -> list[dict[str, Any]]:
    r = await h.http.get("/" + artifact["download_url"].split("/", 3)[3])
    assert r.status_code == 200
    rows: list[dict[str, Any]] = pq.read_table(io.BytesIO(r.content)).to_pylist()
    return rows


async def test_rest_converts_url_safe_base64_to_the_rows_sent(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    r = await paid_app.paying.post(
        "/v1/convert", json={"content_base64": URLSAFE.rstrip("="), "filename": "sales.csv"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["report"]["rows"] == 5
    assert await _downloaded(paid_app, r.json()["artifact"]) == EXPECTED
    assert len(facilitator.settled) == 1


async def test_mcp_converts_url_safe_base64_to_the_rows_sent(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    args = {"content_base64": URLSAFE, "filename": "sales.csv"}
    async with kit.mcp_over_http(paid_app.app) as client:
        quote = await client.call_tool("convert_to_parquet", args)
        meta = await kit.mcp_payment_meta(kit.paying_client(), quote.structured_content)
        result = await client.call_tool("convert_to_parquet", args, meta=meta)
    assert not result.is_error, result.content
    assert result.structured_content["report"]["rows"] == 5
    assert await _downloaded(paid_app, result.structured_content["artifact"]) == EXPECTED
    assert len(facilitator.settled) == 1


# A stray character anywhere, and the percent-encoded data: URL JavaScript builds with
# encodeURIComponent: each used to be dropped or misread, and sometimes converted and charged.
STRAY = [
    (URLSAFE[:8] + "*" + URLSAFE[8:], "content_base64 is not valid base64."),
    (
        URLSAFE[:8] + "." + URLSAFE[8:12] + ".." + URLSAFE[12:] + ".",
        "content_base64 is not valid base64.",
    ),
    (
        "data:text/csv;charset=utf-8,id%2Clabel%0A1%2Cx%0A",
        "content_base64 is a data: URL that is not base64-encoded.",
    ),
]


async def _signature(h: kit.AppHarness, path: str) -> dict[str, str]:
    from x402 import parse_payment_required

    quote = await h.http.post(path, json={})
    required = parse_payment_required(base64.b64decode(quote.headers["payment-required"]))
    payload = await kit.paying_client().create_payment_payload(required)
    header = base64.b64encode(
        json.dumps(payload.model_dump(by_alias=True, exclude_none=True)).encode()
    ).decode()
    return {"payment-signature": header}


@pytest.mark.parametrize(("value", "message"), STRAY)
@pytest.mark.parametrize("path", ["/v1/convert", "/v1/inspect", "/v1/preview", "/v1/query"])
@pytest.mark.parametrize("paying", [False, True], ids=["unpaid", "paid"])
async def test_rest_refuses_a_stray_character_before_the_quote(
    value: str,
    message: str,
    path: str,
    paying: bool,
    paid_app: kit.AppHarness,
    facilitator: kit.FakeFacilitator,
) -> None:
    body: dict[str, Any] = {"content_base64": value, "filename": "sales.csv"}
    if path == "/v1/query":
        body["sql"] = "SELECT * FROM data"
    headers = await _signature(paid_app, path) if paying else {}
    r = await paid_app.http.post(path, json=body, headers=headers)
    assert r.status_code == 400, r.text
    assert "payment-required" not in r.headers
    error = r.json()["error"]
    assert (error["code"], error["message"]) == ("invalid_request", message)
    assert "base64" in error["hint"].lower()
    assert facilitator.verified == []


@pytest.mark.parametrize(("value", "message"), STRAY)
@pytest.mark.parametrize("paying", [False, True], ids=["unpaid", "paid"])
async def test_mcp_refuses_a_stray_character_before_the_quote(
    value: str,
    message: str,
    paying: bool,
    paid_app: kit.AppHarness,
    facilitator: kit.FakeFacilitator,
) -> None:
    args = {"content_base64": value, "filename": "sales.csv"}
    async with kit.mcp_over_http(paid_app.app) as client:
        meta = None
        if paying:
            quote = await client.call_tool("convert_to_parquet", {"content_base64": URLSAFE})
            meta = await kit.mcp_payment_meta(kit.paying_client(), quote.structured_content)
        result = await client.call_tool("convert_to_parquet", args, meta=meta)
    assert result.is_error
    error = (result.structured_content or {})["error"]
    assert (error["code"], error["message"]) == ("invalid_request", message)
    assert facilitator.verified == []
