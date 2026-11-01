"""One ``InternalError`` for every unexpected exception: the isolated job, REST and MCP agree.

Incident (final-validate review): the REST and MCP backstops answered an unexpected exception
with the hint "Retry; if it persists, report it. You were not charged.", while an isolated job's
crash, the same unsettled 500, said only "Retry; if it persists, report it.". Two copies of one
answer had drifted; ``errors.InternalError`` is now the only one.
"""

from __future__ import annotations

import importlib

import pytest

from aimart_parquet import errors
from aimart_parquet.engine.isolate import run_isolated
from aimart_parquet.service import internal_error

helpers = importlib.import_module("integration.isolate_helpers")


def test_an_isolated_crash_is_the_same_error_as_the_backstops() -> None:
    with pytest.raises(errors.InternalError) as info:
        run_isolated(helpers.raise_value_error, timeout_s=30)
    try:
        raise ValueError("unexpected bug")  # the backstop reads the traceback
    except ValueError as exc:
        backstop = internal_error(exc)
    assert info.value.to_dict() == backstop.to_dict()
    assert info.value.to_dict() == {
        "error": {
            "code": "internal_error",
            "message": "Internal error (ValueError).",
            "hint": "Retry; if it persists, report it. You were not charged.",
        }
    }
    assert info.value.http_status == 500


def test_the_internal_error_is_still_an_aimart_error() -> None:
    # Raising an AimartError is what keeps a call unsettled; the subclass must stay one.
    exc = errors.InternalError.of("KeyError")
    assert isinstance(exc, errors.AimartError)
    assert (exc.code, exc.http_status, exc.message) == (
        "internal_error",
        500,
        "Internal error (KeyError).",
    )
