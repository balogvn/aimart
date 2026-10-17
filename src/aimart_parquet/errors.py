"""Typed errors that read well to both an LLM and a human.

Every error carries a stable machine-readable ``code``, a one-sentence ``message`` saying what
went wrong, and a ``hint`` saying what the caller should do next. Agents act on the hint, so it
must be concrete ("pass `sheet`", not "check your input").

Every ``http_status`` is >= 400 on purpose: the x402 middleware only settles a payment when the
handler returns < 400, so raising any of these guarantees the caller is not charged for a
failed call.
"""

from __future__ import annotations

from typing import Any, ClassVar


class AimartError(Exception):
    """Base class. Subclasses set ``code`` and ``http_status``."""

    code: ClassVar[str] = "internal_error"
    http_status: ClassVar[int] = 500

    def __init__(self, message: str, *, hint: str | None = None, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            body["hint"] = self.hint
        if self.details:
            body["details"] = self.details
        return {"error": body}


class InvalidRequest(AimartError):
    code = "invalid_request"
    http_status = 400


class InputTooLarge(AimartError):
    code = "input_too_large"
    http_status = 413


class UnsupportedFormat(AimartError):
    code = "unsupported_format"
    http_status = 415


class InvalidInput(AimartError):
    """The bytes were received but could not be parsed as the claimed/detected format."""

    code = "invalid_input"
    http_status = 422


class SchemaViolation(AimartError):
    code = "schema_violation"
    http_status = 422


class FetchFailed(AimartError):
    code = "fetch_failed"
    http_status = 422


class UrlNotAllowed(AimartError):
    code = "url_not_allowed"
    http_status = 422


class QueryRejected(AimartError):
    code = "query_rejected"
    http_status = 400


class QueryFailed(AimartError):
    code = "query_failed"
    http_status = 422


class Timeout(AimartError):
    code = "timeout"
    http_status = 504


class ArtifactNotFound(AimartError):
    code = "artifact_not_found"
    http_status = 404


class Busy(AimartError):
    code = "busy"
    http_status = 503
