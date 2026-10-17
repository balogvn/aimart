"""JSON logs on stdout — what free hosts capture — with no payloads and no secrets."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        body: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        body.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD})
        if record.exc_info:
            body["exc"] = self.formatException(record.exc_info)
        return json.dumps(body, default=str)


def configure(level: str = "INFO", *, json_logs: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout if json_logs else sys.stderr)
    handler.setFormatter(
        JsonFormatter() if json_logs else logging.Formatter("%(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access", "httpx", "httpcore", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
