"""The access log says ``payment_settled`` only for a settlement that succeeded.

Review of 2026-11-16 (S6): ``AccessLogMiddleware`` set it whenever a PAYMENT-RESPONSE header
was present, and the 402 after a failed settlement carries that header too (``success``
false, the reason). So the one line the owner counts paid calls from said "settled" for calls
whose result was withheld and whose payer, as far as the facilitator said, was not charged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit

if TYPE_CHECKING:
    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

BODY = {"content_base64": kit.b64(b"region,units\nNorth,10\n"), "filename": "sales.csv"}
TX = kit.TX_HASH


def access_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[Any, Any, Any]]:
    return [
        (r.path, r.status, r.payment_settled)  # type: ignore[attr-defined]
        for r in caplog.records
        if r.name == "aimart.access"
    ]


@pytest.mark.parametrize(
    "facilitator",
    [
        pytest.param(lambda: kit.FakeFacilitator(settle_ok=False), id="refused"),
        pytest.param(
            lambda: kit.FakeFacilitator(settle_error=RuntimeError("boom")), id="settle_raised"
        ),
        pytest.param(lambda: kit.wire_facilitator({"status": "ok"}), id="unreadable_answer"),
        pytest.param(
            lambda: kit.wire_facilitator({"success": False, "transaction": TX, "error": "late"}),
            id="refused_with_a_transaction",
        ),
        pytest.param(
            lambda: kit.wire_facilitator({"success": True, "transaction": TX}, status=500),
            id="http_500",
        ),
    ],
)
async def test_a_failed_settlement_is_not_logged_as_settled(
    facilitator: Any, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="aimart.access"):
        async with kit.running_app(settings, facilitators=[facilitator()]) as app:
            refused = await app.paying.post("/v1/convert", json=BODY)
    assert refused.status_code == 402
    # The header that made the log say "settled": present, and saying the opposite.
    assert kit.payment_response(refused)["success"] is False
    # The paying client's first, unpaid attempt and the paid one: neither settled.
    assert access_lines(caplog) == [("/v1/convert", 402, False), ("/v1/convert", 402, False)]


@pytest.mark.parametrize(
    "facilitator",
    [
        pytest.param(kit.FakeFacilitator, id="schema"),
        pytest.param(
            lambda: kit.wire_facilitator({"success": True, "txHash": TX}, status=201),
            id="read_leniently",
        ),
    ],
)
async def test_a_settlement_that_succeeded_is_logged_as_settled(
    facilitator: Any, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="aimart.access"):
        async with kit.running_app(settings, facilitators=[facilitator()]) as app:
            paid = await app.paying.post("/v1/convert", json=BODY)
    assert kit.payment_response(paid)["success"] is True
    assert access_lines(caplog) == [("/v1/convert", 402, False), ("/v1/convert", 200, True)]


@pytest.mark.parametrize(
    "value", [b"", b"not base64 !!", b"bnVsbA==", b"W10=", b"eyJzdWNjZXNzIjoxfQ=="]
)
async def test_a_header_that_does_not_say_success_true_is_not_a_settlement(
    value: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    """The reading itself: junk, ``null``, ``[]`` and ``{"success": 1}`` are not "settled",
    and none of them can break the response the log line is about."""
    from aimart_parquet.api.middleware import AccessLogMiddleware

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"Payment-Response", value)],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    sent: list[Any] = []

    async def send(message: Any) -> None:
        sent.append(message)

    with caplog.at_level(logging.INFO, logger="aimart.access"):
        await AccessLogMiddleware(app)({"type": "http", "path": "/x", "method": "GET"}, None, send)  # type: ignore[arg-type]
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert access_lines(caplog) == [("/x", 200, False)]
