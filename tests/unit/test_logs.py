"""JSON logs: one object per line, structured extras kept, standard noise dropped."""

from __future__ import annotations

import json
import logging
import sys
from typing import TYPE_CHECKING

import pytest

from aimart_parquet.logs import JsonFormatter, configure

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    noisy = {n: logging.getLogger(n).level for n in ("uvicorn.access", "httpx", "httpcore", "mcp")}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, lvl in noisy.items():
        logging.getLogger(name).setLevel(lvl)


def record(msg: str, **extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("aimart.test", logging.WARNING, __file__, 1, msg, (), None)
    rec.__dict__.update(extra)
    return rec


def test_json_formatter_keeps_extras_and_drops_standard_fields() -> None:
    body = json.loads(JsonFormatter().format(record("paid %s", operation="convert", amount=10000)))
    assert body["level"] == "warning"
    assert body["logger"] == "aimart.test"
    assert body["msg"] == "paid %s"
    assert body["operation"] == "convert"
    assert body["amount"] == 10000
    assert "pathname" not in body
    assert "args" not in body
    assert "ts" in body


def test_json_formatter_serialises_odd_values_and_exceptions() -> None:
    try:
        raise RuntimeError("kaboom")
    except RuntimeError:
        rec = record("failed", path=object())
        rec.exc_info = sys.exc_info()
    body = json.loads(JsonFormatter().format(rec))
    assert "RuntimeError: kaboom" in body["exc"]
    assert body["path"].startswith("<object")


@pytest.mark.usefixtures("restore_root_logger")
def test_configure_installs_one_handler_and_quiets_chatty_loggers() -> None:
    configure("debug", json_logs=True)
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    assert root.handlers[0].stream is sys.stdout  # type: ignore[attr-defined]
    assert root.level == logging.DEBUG
    assert logging.getLogger("httpx").level == logging.WARNING

    configure("info", json_logs=False)
    assert not isinstance(root.handlers[0].formatter, JsonFormatter)
    assert root.handlers[0].stream is sys.stderr  # type: ignore[attr-defined]
