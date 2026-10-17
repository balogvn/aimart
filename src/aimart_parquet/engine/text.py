"""Get any text input into clean UTF-8 before a parser sees it.

DuckDB and the JSON parsers expect UTF-8. Real files arrive as Windows-1252 exports from
Excel, Latin-1 from legacy systems, or UTF-16 from PowerShell. We validate UTF-8 by streaming
the whole file (a sample can be ASCII for 10 MB and break on row 200,000), and only fall back
to statistical detection when that fails.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from typing import TYPE_CHECKING

from charset_normalizer import from_bytes

from aimart_parquet.errors import InvalidRequest

if TYPE_CHECKING:
    from pathlib import Path

_CHUNK = 1024 * 1024
_DETECT_SAMPLE = 2 * 1024 * 1024


@dataclass(frozen=True)
class TextInfo:
    path: Path
    encoding: str
    replaced_chars: int


def _is_valid_utf8(path: Path) -> bool:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    try:
        with path.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                decoder.decode(chunk)
            decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def _detect(path: Path) -> str:
    with path.open("rb") as fh:
        sample = fh.read(_DETECT_SAMPLE)
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    best = from_bytes(sample).best()
    if best is None:
        return "cp1252"  # the most common non-UTF-8 export encoding; decoding never fails
    return str(best.encoding)


def _transcode(src: Path, dst: Path, encoding: str) -> int:
    """Transcode to UTF-8; returns how many undecodable characters became U+FFFD.

    Counting U+FFFD in the output is exact for every single-byte source encoding (none of them
    can encode U+FFFD), and close enough for UTF-16, where it would merely over-report.
    """
    replaced = 0
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    with src.open("rb") as fin, dst.open("w", encoding="utf-8", newline="") as fout:
        while chunk := fin.read(_CHUNK):
            text = decoder.decode(chunk)
            replaced += text.count("�")
            fout.write(text)
        tail = decoder.decode(b"", final=True)
        replaced += tail.count("�")
        fout.write(tail)
    return replaced


def to_utf8(path: Path, workdir: Path, forced_encoding: str | None = None) -> TextInfo:
    """Return a UTF-8 version of ``path`` (the same path when it already is UTF-8)."""
    if forced_encoding:
        try:
            codecs.lookup(forced_encoding)
        except LookupError as exc:
            raise InvalidRequest(
                f"Unknown encoding {forced_encoding!r}.",
                hint="Use a Python codec name such as 'utf-8', 'latin-1' or 'cp1252', or omit it.",
            ) from exc
        if codecs.lookup(forced_encoding).name == "utf-8":
            return TextInfo(path, "utf-8", 0)
        encoding = forced_encoding
    elif _is_valid_utf8(path):
        return TextInfo(path, "utf-8", 0)
    else:
        encoding = _detect(path)
    dst = workdir / "utf8.txt"
    replaced = _transcode(path, dst, encoding)
    return TextInfo(dst, codecs.lookup(encoding).name, replaced)
