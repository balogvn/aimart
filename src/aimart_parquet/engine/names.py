"""Column names an LLM can write SQL against without quoting.

``Customer Name (USD)`` becomes ``customer_name_usd``; ``orderID`` becomes ``order_id``; empty
and duplicate headers become ``column_3`` and ``name_2``. Non-Latin letters are kept (dropping
them would erase the only meaning a Chinese header has); accents on Latin letters are removed.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

_CAMEL_1 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_2 = re.compile(r"([a-z0-9])([A-Z])")
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
_UNDERSCORES = re.compile(r"_+")
MAX_NAME_LENGTH = 64


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def snake_case(name: str) -> str:
    text = _strip_accents(name.strip())
    text = _CAMEL_1.sub(r"\1_\2", text)
    text = _CAMEL_2.sub(r"\1_\2", text)
    text = _NON_WORD.sub("_", text.lower())
    text = _UNDERSCORES.sub("_", text).strip("_")
    if text and text[0].isdigit():
        text = "c_" + text
    return text[:MAX_NAME_LENGTH].rstrip("_")


def make_unique(names: Sequence[str], *, normalize: bool) -> list[str]:
    """Normalize (optionally) and de-duplicate case-insensitively, filling blanks."""
    result: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(names, start=1):
        base = snake_case(raw) if normalize else raw.strip()[:MAX_NAME_LENGTH]
        if not base:
            base = f"column_{i}"
        candidate, n = base, 2
        while candidate.lower() in seen:
            candidate = f"{base}_{n}"
            n += 1
        seen.add(candidate.lower())
        result.append(candidate)
    return result
