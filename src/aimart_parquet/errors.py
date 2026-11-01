"""Typed errors that read well to both an LLM and a human.

Every error carries a stable machine-readable ``code``, a one-sentence ``message`` saying what
went wrong, and a ``hint`` saying what the caller should do next. Agents act on the hint, so it
must be concrete ("pass `sheet`", not "check your input").

Every ``http_status`` is >= 400 on purpose: the x402 middleware only settles a payment when the
handler returns < 400, so raising any of these guarantees the caller is not charged for a
failed call.

A message built from a library's exception text goes through ``without_paths`` first: native
libraries name the file they failed on, and remote callers never see server paths.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    import os

# A quoted absolute path, as DuckDB ("...") and Arrow ('...') put them in error messages.
_QUOTED_PATH = re.compile(r"""(["'])/[^"']*\1""")
# A bare absolute path of two parts or more, as in pyarrow's FileNotFoundError ("/tmp/x/inline").
# The lookbehind keeps ratios and dates ("3/4", "N/A", "12/31/2025"), and what follows a
# directory already replaced ("<tmp>/inputs/t0.parquet"), as they are.
_BARE_PATH = re.compile(r"(?<![\w.~/>-])/[\w.-]+(?:/[\w.-]*)+")


def without_paths(exc: BaseException | str, *directories: str | os.PathLike[str]) -> str:
    """The first line of an exception's text, fit for a caller: no server path in it.

    pyarrow's "Could not open Parquet input source '/tmp/aimart-artifacts/.work/tmpX/inline'"
    reached callers of convert with the server's work layout. Each of ``directories`` (as given
    and resolved; DuckDB prints a query's spill directory unquoted) becomes ``<tmp>``, a quoted
    absolute path ``"<file>"``, and a bare one ``<file>``. An exception with no text gives its
    type's name.
    """
    text = exc if isinstance(exc, str) else str(exc)
    lines = text.splitlines()
    message = lines[0] if lines else ("" if isinstance(exc, str) else type(exc).__name__)
    names = {str(d) for d in directories} | {str(Path(d).resolve()) for d in directories}
    for name in sorted(names, key=len, reverse=True):  # "/private/var/x" before "/var/x"
        message = message.replace(name, "<tmp>")
    message = _QUOTED_PATH.sub('"<file>"', message)
    return _BARE_PATH.sub("<file>", message)


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


class StorageUnavailable(AimartError):
    """The artifact store (GCS) could not be reached; the call is refused, so it is unpaid."""

    code = "storage_unavailable"
    http_status = 503
