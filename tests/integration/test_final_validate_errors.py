"""Unexpected failures are typed, never settled, and never quote their own text.

Before: an exception that was not an ``AimartError`` escaped a paid REST route as a bare 500
("Internal Server Error", no code or hint), and over MCP the SDK answered an untyped
"Error executing tool ..." text. Now both answer ``internal_error`` with only the exception's
type, log its type and frames but never its text (it can quote paths or data), and settle
nothing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit

if TYPE_CHECKING:
    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

A = "Zz" * 12
CSV = b"a,b\n1,2\n3,4\n"
SECRET = "/srv/aimart/.work/tmp123/inline: customer value 4111-1111"
INTERNAL = {
    "error": {
        "code": "internal_error",
        "message": "Internal error (RuntimeError).",
        "hint": "Retry; if it persists, report it. You were not charged.",
    }
}


def _crash(*_: Any, **__: Any) -> Any:
    raise RuntimeError(SECRET)


def stored(settings: Settings) -> set[str]:
    root = settings.artifact_dir
    return {p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")}


async def test_rest_unexpected_exception_is_a_typed_500_never_settled(
    paid_app: kit.AppHarness,
    facilitator: kit.FakeFacilitator,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(paid_app.app.state.service, "inspect", _crash)
    with caplog.at_level(logging.ERROR):
        r = await paid_app.paying.post("/v1/inspect", json={"artifact_id": A})
    assert r.status_code == 500
    assert r.json() == INTERNAL
    assert "payment-response" not in r.headers
    assert len(facilitator.verified) == 1
    assert facilitator.settled == []
    [record] = [rec for rec in caplog.records if rec.getMessage() == "internal error"]
    assert record.error_type == "RuntimeError"  # type: ignore[attr-defined]
    assert any("_crash" in frame for frame in record.frames)  # type: ignore[attr-defined]
    assert "4111" not in caplog.text  # the exception's text can quote data: never logged


async def test_rest_crash_after_storing_an_output_deletes_it(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = paid_app.app.state.service
    real = service.convert

    async def convert_then_crash(*args: Any, **kwargs: Any) -> Any:
        await real(*args, **kwargs)
        raise RuntimeError(SECRET)

    monkeypatch.setattr(service, "convert", convert_then_crash)
    r = await paid_app.paying.post(
        "/v1/convert", json={"content_base64": kit.b64(CSV), "filename": "x.csv"}
    )
    assert r.status_code == 500
    assert r.json() == INTERNAL
    assert facilitator.settled == []
    assert stored(paid_app.settings) == set()


async def test_free_app_unexpected_exception_is_typed_too(
    free_app: kit.AppHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(free_app.app.state.service, "inspect", _crash)
    r = await free_app.http.post("/v1/inspect", json={"artifact_id": A})
    assert r.status_code == 500
    assert r.json() == INTERNAL
    assert r.headers["x-request-id"]  # the access log saw it as an ordinary 500


async def test_mcp_unexpected_exception_is_a_typed_error_never_settled(
    paid_app: kit.AppHarness,
    facilitator: kit.FakeFacilitator,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(paid_app.app.state.service, "inspect", _crash)
    with caplog.at_level(logging.ERROR):
        async with kit.mcp_over_http(paid_app.app) as client:
            quote = await client.call_tool("inspect_parquet", {"artifact_id": A})
            meta = await kit.mcp_payment_meta(kit.paying_client(), quote.structured_content)
            result = await client.call_tool("inspect_parquet", {"artifact_id": A}, meta=meta)
    assert result.is_error
    assert result.structured_content == INTERNAL
    assert "x402/payment-response" not in (result.meta or {})
    assert len(facilitator.verified) == 1
    assert facilitator.settled == []
    assert "4111" not in caplog.text
