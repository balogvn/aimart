"""Demand signals: what agents tried to buy that we do not sell yet.

Two sources, both free to the caller and zero-cost to run (structured log lines, which Cloud
Logging keeps under its free tier):

* ``unsupported_format`` — a conversion rejected because of the file type. Only the extension
  is recorded, never the content or the full name.
* ``capability_request`` — an agent tells us, in its own words, what it needed and what it
  would pay, via the free ``request_capability`` tool or ``POST /v1/feedback``.

This is how the roadmap gets decided by real traffic instead of guesses: read the log lines
(``jsonPayload.msg="demand_signal"``) or, with ``AIMART_ADMIN_TOKEN`` set, ``GET /v1/demand``.
Counters reset on restart; the logs are the durable record.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections import Counter, deque
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger("aimart.demand")

_EXTENSION = re.compile(r"^\.[a-z0-9]{1,10}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_RECENT_REQUESTS = 200


class CapabilityRequest(BaseModel):
    """What an agent needed and could not get. Never include personal or confidential data."""

    model_config = ConfigDict(extra="forbid")

    task: str = Field(
        min_length=3,
        max_length=1000,
        description="What you were trying to do, in a sentence or two.",
    )
    input_formats: list[str] = Field(
        default_factory=list, max_length=10, description="File types involved, e.g. ['pdf', 'xml']."
    )
    desired_output: str | None = Field(
        default=None, max_length=300, description="What you wanted back."
    )
    max_price_usd: float | None = Field(
        default=None, ge=0, le=1000, description="The most you would pay per call."
    )
    calls_per_month: int | None = Field(
        default=None, ge=0, le=100_000_000, description="Rough expected volume."
    )
    agent: str | None = Field(
        default=None, max_length=100, description="Your agent or platform name (optional)."
    )


class CapabilityAck(BaseModel):
    received: bool = True
    id: str
    message: str = "Thank you. The operator reads these to decide what to build next."


def _clean(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    return _CONTROL.sub(" ", text).strip()[:limit] or None


def extension_of(filename: str | None) -> str:
    if not filename:
        return "(none)"
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    if not suffix:
        return "(none)"
    return suffix if _EXTENSION.match(suffix) else "(other)"


class DemandSignals:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._unsupported: Counter[str] = Counter()
        self._requests: deque[dict[str, Any]] = deque(maxlen=_RECENT_REQUESTS)
        self._request_count = 0
        self._started = time.time()

    def unsupported_format(self, filename: str | None) -> None:
        ext = extension_of(filename)
        with self._lock:
            self._unsupported[ext] += 1
        log.info("demand_signal", extra={"kind": "unsupported_format", "extension": ext})

    def capability_request(self, request: CapabilityRequest) -> CapabilityAck:
        record = {
            "id": secrets.token_hex(6),
            "at": int(time.time()),
            "task": _clean(request.task, 1000),
            "input_formats": [f for f in (_clean(x, 32) for x in request.input_formats) if f],
            "desired_output": _clean(request.desired_output, 300),
            "max_price_usd": request.max_price_usd,
            "calls_per_month": request.calls_per_month,
            "agent": _clean(request.agent, 100),
        }
        with self._lock:
            self._requests.append(record)
            self._request_count += 1
        log.info("demand_signal", extra={"kind": "capability_request", **record})
        return CapabilityAck(id=str(record["id"]))

    def snapshot(self) -> dict[str, Any]:
        """Everything since boot. The recent list is bounded and says so (no silent caps)."""
        with self._lock:
            return {
                "since": int(self._started),
                "unsupported_formats": dict(self._unsupported.most_common()),
                "capability_requests_total": self._request_count,
                "capability_requests_recent": list(self._requests),
                "recent_limit": _RECENT_REQUESTS,
                "note": "counters reset on restart; the durable record is the demand_signal log lines",
            }
