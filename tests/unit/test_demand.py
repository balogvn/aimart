from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from aimart_parquet.demand import CapabilityRequest, DemandSignals, extension_of


@pytest.mark.parametrize(
    ("name", "ext"),
    [
        ("report.DOCX", ".docx"),
        ("a/b/c.xml", ".xml"),
        (None, "(none)"),
        ("noext", "(none)"),
        ("x.reallylongextension", "(other)"),
        ("..\\evil.<script>", "(other)"),
    ],
)
def test_only_a_sanitized_extension_is_recorded(name: str | None, ext: str) -> None:
    assert extension_of(name) == ext


def test_unsupported_formats_are_counted_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    demand = DemandSignals()
    with caplog.at_level(logging.INFO, logger="aimart.demand"):
        demand.unsupported_format("deck.pptx")
        demand.unsupported_format("other.PPTX")
        demand.unsupported_format("site.html")
    assert demand.snapshot()["unsupported_formats"] == {".pptx": 2, ".html": 1}
    record = caplog.records[0]
    assert record.getMessage() == "demand_signal"
    assert record.kind == "unsupported_format"  # type: ignore[attr-defined]
    assert record.extension == ".pptx"  # type: ignore[attr-defined]


def test_capability_requests_are_cleaned_bounded_and_acknowledged() -> None:
    demand = DemandSignals()
    ack = demand.capability_request(
        CapabilityRequest(
            task="Extract\x00 line items\x1b from scanned receipts",
            input_formats=["jpg"],
            max_price_usd=0.05,
        )
    )
    assert ack.received
    snap = demand.snapshot()
    assert snap["capability_requests_total"] == 1
    [rec] = snap["capability_requests_recent"]
    assert rec["id"] == ack.id
    assert "\x00" not in rec["task"]
    assert "\x1b" not in rec["task"]
    for _ in range(snap["recent_limit"] + 5):
        demand.capability_request(CapabilityRequest(task="more of the same"))
    snap = demand.snapshot()
    assert len(snap["capability_requests_recent"]) == snap["recent_limit"]  # bounded, and says so
    assert snap["capability_requests_total"] == snap["recent_limit"] + 6


def test_capability_request_rejects_oversized_or_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CapabilityRequest(task="x" * 1001)
    with pytest.raises(ValidationError):
        CapabilityRequest(task="ok task", email="someone@example.com")  # type: ignore[call-arg]
