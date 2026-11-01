"""An isolated job's unexpected exception is logged without its text.

Incident (docs review after e8d228e): ``retention_policy()['logs']``, published in /pricing and
llms.txt, says logs never hold file contents, but ``run_isolated`` logged the full text of a
child's unexpected exception, which can quote caller data (a KeyError names a column, a
ValueError quotes the cell it could not parse); docs/privacy.md had been changed to admit it.
The code now logs the exception's type and its traceback frames (file, line, function), with no
message and no locals, so the published sentence is true again.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING

import pytest

from aimart_parquet import errors
from aimart_parquet.api.discovery import retention_policy
from aimart_parquet.engine import isolate
from aimart_parquet.engine.isolate import run_isolated

if TYPE_CHECKING:
    from collections.abc import Callable

    from aimart_parquet.config import Settings

helpers = importlib.import_module("integration.isolate_helpers")
SECRET = "Zoë Example, card ending 4242"  # what helpers.raise_with_secret_text puts in its text


def test_the_logged_record_carries_no_exception_message(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG), pytest.raises(errors.AimartError) as info:
        run_isolated(helpers.raise_with_secret_text, timeout_s=30)
    assert info.value.message == "Internal error (KeyError)."  # the caller: the type only
    assert SECRET not in str(info.value.to_dict())
    records = [r for r in caplog.records if r.name == "aimart.isolate"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.ERROR
    assert record.exc_info is None  # a formatter would print the message from exc_info
    # Every attribute a handler or a JSON formatter could write out, extras included.
    for value in [record.getMessage(), *map(repr, vars(record).values())]:
        assert "Zoë" not in value
        assert "4242" not in value
    text = record.getMessage()
    assert "KeyError" in text  # enough to find the bug: its type and where it was raised
    assert "raise_with_secret_text" in text
    assert "isolate_helpers.py" in text
    assert "line " in text


def test_frames_name_file_line_and_function_only() -> None:
    def failing(value: str) -> None:
        local_secret = value  # noqa: F841 - a local the log must not show
        raise ValueError(value)

    try:
        failing(SECRET)
    except ValueError as exc:
        where = isolate._where(exc)
    assert SECRET not in where
    assert "local_secret" not in where  # no locals, and no source lines either
    assert where.splitlines()[0] == "ValueError (message withheld)"
    assert all(line.startswith('  File "') for line in where.splitlines()[1:])
    assert ", in failing" in where.splitlines()[-1]


def test_the_published_logs_sentence_matches_the_code(
    make_settings: Callable[..., Settings],
) -> None:
    logs = retention_policy(make_settings())["logs"]
    assert "never file contents" in logs
    assert "error type" in logs  # what a failed job does log
