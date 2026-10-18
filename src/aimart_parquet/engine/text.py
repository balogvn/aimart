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
    guess = codecs.lookup(str(best.encoding)).name
    if guess in _CONFUSABLE_WESTERN and _plausible_cp1252(sample):
        # charset_normalizer picks cp437/cp1250 for short Western-European files, silently
        # turning € into Ç and ñ into ń with zero replacement characters. cp1252 (Windows
        # Excel's export encoding, a superset of Latin-1's printable range) is the far more
        # likely truth when its decoding reads as Latin text. Cyrillic/CJK guesses are trusted.
        return "cp1252"
    return guess


# Single-byte code pages statistically confusable with cp1252 on short samples.
_CONFUSABLE_WESTERN = frozenset(
    codecs.lookup(n).name
    for n in (
        "cp437",
        "cp850",
        "cp852",
        "cp858",
        "cp1250",
        "cp1257",
        "iso8859-2",
        "iso8859-4",
        "iso8859-13",
        "iso8859-15",
        "latin-1",
        "mac-roman",
        "mac-latin2",
        "cp1254",
        "iso8859-9",
    )
)
_CP1252_TEXT = frozenset(
    "ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝÞßàáâãäåæçèéêëìíîïðñòóôõöøùúûüýþÿ"
    "€‚„…†‡‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ¡¢£¤¥¦§¨©ª«¬®¯°±²³´µ¶·¸¹º»¼½¾¿×÷\u00a0"
)


def _plausible_cp1252(sample: bytes) -> bool:
    """Decodes as cp1252 (no undefined bytes) and its non-ASCII characters read as Latin text."""
    try:
        text = sample.decode("cp1252")
    except UnicodeDecodeError:
        return False  # 0x81, 0x8D, 0x8F, 0x90, 0x9D are undefined in cp1252
    non_ascii = [ch for ch in text if ord(ch) > 127]
    if not non_ascii:
        return True
    return sum(ch in _CP1252_TEXT for ch in non_ascii) / len(non_ascii) >= 0.95


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
