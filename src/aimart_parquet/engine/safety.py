"""Guards that run before any parser sees untrusted bytes.

The threat is not malformed data — parsers reject that — but small inputs that expand into
huge ones (gzip/zip bombs, an xlsx whose XML parts inflate 1000x) and exhaust memory or disk
on a free-tier host. Every expansion here is streamed and counted against a hard cap, and zip
archives are checked from their central directory before a single member is inflated.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import os
import shutil
import struct
import zipfile
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import IO, TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

from aimart_parquet.errors import InputTooLarge, InvalidInput, without_paths

Compression = Literal["gzip", "bz2", "zip"]

_CHUNK = 1024 * 1024
# A legitimate CSV compresses ~10x and a very repetitive one ~100x; an xlsx part rarely more.
# 250x and up is the signature of a crafted bomb.
MAX_ZIP_RATIO = 250
MAX_ZIP_MEMBERS = 10_000
_IGNORED_MEMBER_PREFIXES = ("__MACOSX/", ".DS_Store")


# The bytes ``detect_format`` judges a file's start by (``read_magic``'s default).
MAGIC_BYTES = 600


def read_magic(path: Path, n: int = MAGIC_BYTES) -> bytes:
    with path.open("rb") as fh:
        return fh.read(n)


# The bytes ``sniff_compression`` and ``unread_compression`` judge a file's start by.
SNIFF_BYTES = 10
# After 'BZh', a bz2 stream's block size, then its first block's magic (or its end's, when it
# holds nothing).
_BZ2_LEVELS = tuple(bytes([b]) for b in b"123456789")
_BZ2_BLOCKS = (b"1AY&SY", b"\x17rE8P\x90")


def sniff_compression(magic: bytes) -> Compression | None:
    """The compression ``convert.unwrap`` removes that ``magic``, a file's first SNIFF_BYTES,
    begins with."""
    if magic.startswith(b"\x1f\x8b"):
        return "gzip"
    # 'BZh' alone is text a CSV may begin with (a header 'BZh_code'), which was then refused as
    # a corrupt bz2 stream; inside a gzip it converted.
    if magic[:3] == b"BZh" and magic[3:4] in _BZ2_LEVELS and magic[4:10] in _BZ2_BLOCKS:
        return "bz2"
    if magic.startswith(b"PK\x03\x04"):
        return "zip"
    return None


# Compression no reader here reads, by its magic. Read as text, xz named data.csv was a 200 of
# cp1252 garbage; now it is refused, as is any of these inside a zip or a gzip.
_UNREAD = {
    b"\xfd7zXZ\x00": "xz",
    b"\x28\xb5\x2f\xfd": "zstd",
    b"\x04\x22\x4d\x18": "lz4",
    b"7z\xbc\xaf\x27\x1c": "7z",
    b"Rar!\x1a\x07": "rar",
}


def unread_compression(magic: bytes) -> str | None:
    """The name of the compression no reader here reads that ``magic`` begins with, or None."""
    return next((kind for start, kind in _UNREAD.items() if magic.startswith(start)), None)


class Readable(Protocol):
    def read(self, size: int = ..., /) -> bytes: ...


def _too_large(cap: int) -> InputTooLarge:
    return InputTooLarge(
        f"Decompressed input exceeds the {cap // (1024 * 1024)} MB limit.",
        hint="Split the file, or send a smaller extract.",
        limit_bytes=cap,
    )


def copy_capped(src: Readable, dst: IO[bytes], cap: int) -> int:
    """Stream src to dst, raising InputTooLarge as soon as more than ``cap`` bytes appear."""
    total = 0
    while chunk := src.read(_CHUNK):
        total += len(chunk)
        if total > cap:
            raise _too_large(cap)
        dst.write(chunk)
    return total


# What reading a stream that cannot be inflated raises. zlib.error is not an OSError: Python's
# gzip and zipfile modules let it through for deflate data zlib refuses ('invalid block type'),
# and it escaped as the 500 that says to retry, for a file that can never convert.
_CORRUPT = (OSError, EOFError, zlib.error)


def decompress_stream(
    src_path: Path, dst_path: Path, kind: Literal["gzip", "bz2"], cap: int
) -> int:
    opener = gzip.open if kind == "gzip" else bz2.open
    try:
        with opener(src_path, "rb") as src, dst_path.open("wb") as dst:
            return copy_capped(src, dst, cap)
    except _CORRUPT as exc:
        raise _corrupt(kind, exc) from exc


def _corrupt(kind: str, exc: BaseException) -> InvalidInput:
    return InvalidInput(
        f"The {kind} stream is corrupt or truncated: {without_paths(exc)}",
        hint="Re-compress the file, or send it uncompressed.",
    )


class _CappedGzip:
    def __init__(self, fh: gzip.GzipFile, cap: int) -> None:
        self._fh, self._cap, self._total = fh, cap, 0

    def read(self, size: int = _CHUNK, /) -> bytes:
        try:
            chunk = self._fh.read(size)
        except _CORRUPT as exc:
            raise _corrupt("gzip", exc) from exc
        self._total += len(chunk)
        if self._total > self._cap:
            raise _too_large(self._cap)
        return chunk


@contextmanager
def open_gzip(path: Path, cap: int) -> Iterator[Readable]:
    """What a gzip file holds, failing as decompress_stream does: corrupt, or past ``cap``."""
    with gzip.open(path, "rb") as fh:
        yield _CappedGzip(fh, cap)


def gzip_head(path: Path, n: int) -> bytes | None:
    """The first ``n`` bytes a gzip file holds (all of them if fewer), or None where those are
    corrupt: decompress_stream then says what is wrong."""
    try:
        with gzip.open(path, "rb") as fh:
            return fh.read(n)
    except _CORRUPT:
        return None


class GzipMismatch(Exception):
    """A gzip file DuckDB might not read exactly as Python's gzip module does."""


# The header flags DuckDB 1.5 reads: FEXTRA and FNAME. It refuses FTEXT, FHCRC, FCOMMENT and
# the reserved bits, which Python's gzip module reads. It reads the header from the file, at
# any length; this check reads up to 1 MiB of it.
_FEXTRA, _FNAME = 0x04, 0x08
_MAX_GZIP_HEADER = _CHUNK


def gzip_chunks(path: Path, cap: int) -> Generator[bytes]:
    """What a gzip file of one member holds, in chunks, checked for DuckDB to read it after.

    DuckDB reads gzip without checking a member's CRC or length (a corrupt or truncated file
    reads as whatever inflates) and refuses some headers. Wherever the two could read the file
    differently this raises GzipMismatch, and decompress_stream unpacks it, or reports what is
    wrong with it, as d83c738 did with every gzip. Past ``cap`` it raises InputTooLarge.

    Anything after the first member is a mismatch: another member, or the zero padding Python
    skips. DuckDB 1.5.6 refills its 32 KiB input buffer when a member ends; once a refill has
    read exactly to the end of the file, the refill at the next member's end reads nothing and
    DuckDB stops, dropping the members still in its buffer, with no error. A gzip of one member
    per CSV line (what appending a row at a time writes) lost its last 712 rows of 9,000: a 200
    for wrong data. Such files also converted 2.7-3.5x slower than d83c738 unpacked them, a 504
    on a slow core: this pass re-sliced its 1 MiB read buffer for every member, and DuckDB
    starts each member afresh on each of its two reads. And DuckDB refuses a later member's
    header of 32,760 bytes or more (3298e73). A gzip written in one go (gzip, pigz, Python's
    gzip module, pandas) has one member.
    """
    with path.open("rb") as fh:
        try:
            buf = _past_header(fh)
            inflate = zlib.decompressobj(-zlib.MAX_WBITS)
            crc = size = 0
            while not inflate.eof:
                data = buf or fh.read(_CHUNK)
                # At most _CHUNK out per call: a 1 MB read of a bomb inflates to 1 GB.
                out = inflate.decompress(data, _CHUNK)
                buf = inflate.unconsumed_tail
                if not data and not out:
                    raise GzipMismatch("truncated")
                size += len(out)
                if size > cap:
                    raise _too_large(cap)
                crc = zlib.crc32(out, crc)
                if out:
                    yield out
            buf = _fill(fh, inflate.unused_data, 8)
            if buf[:8] != struct.pack("<II", crc, size & 0xFFFFFFFF):
                raise GzipMismatch("CRC or length")
            if buf[8:] or fh.read(1):
                raise GzipMismatch("more than one member")  # or padding, or trailing bytes
        except zlib.error as exc:
            raise GzipMismatch(str(exc)) from exc


def _past_header(fh: Readable) -> bytes:
    """What ``fh`` has read past the gzip header it begins with, one DuckDB reads as Python does
    and of at most _MAX_GZIP_HEADER bytes."""
    buf = _fill(fh, b"", 10)
    flags = buf[3]
    if buf[:3] != b"\x1f\x8b\x08" or flags & ~(_FEXTRA | _FNAME):
        raise GzipMismatch("header")
    pos = 10
    if flags & _FEXTRA:
        buf = _fill(fh, buf, 12)
        pos = 12 + int.from_bytes(buf[10:12], "little")
    buf = _fill(fh, buf, pos)
    if flags & _FNAME:
        while (end := buf.find(b"\0", pos)) < 0:
            if len(buf) > _MAX_GZIP_HEADER:
                raise GzipMismatch("header")
            buf = _fill(fh, buf, len(buf) + 1)
        pos = end + 1
    if pos > _MAX_GZIP_HEADER:
        raise GzipMismatch("header")
    return buf[pos:]


def _fill(fh: Readable, buf: bytes, n: int) -> bytes:
    """``buf``, grown from ``fh`` to at least ``n`` bytes; GzipMismatch if the file ends first."""
    while len(buf) < n:
        more = fh.read(max(_CHUNK, n - len(buf)))
        if not more:
            raise GzipMismatch("truncated")
        buf += more
    return buf


def check_zip(path: Path, cap: int) -> list[zipfile.ZipInfo]:
    """Validate a zip from its central directory without inflating anything."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise InvalidInput(
            f"Not a readable zip archive: {without_paths(exc)}",
            hint="Re-create the archive or send the file.",
        ) from exc
    if len(infos) > MAX_ZIP_MEMBERS:
        raise InvalidInput(
            f"Zip archive has {len(infos)} members; the limit is {MAX_ZIP_MEMBERS}.",
            hint="Send one data file per call.",
        )
    total = sum(i.file_size for i in infos)
    packed = sum(i.compress_size for i in infos)
    if total > cap:
        raise InputTooLarge(
            f"Zip archive expands to {total} bytes, over the {cap} byte limit.",
            hint="Split the archive or send a smaller extract.",
            limit_bytes=cap,
            expanded_bytes=total,
        )
    if packed and total / packed > MAX_ZIP_RATIO and total > 10 * _CHUNK:
        raise InvalidInput(
            "Zip archive compression ratio is implausibly high (possible zip bomb).",
            hint="Send the file uncompressed.",
        )
    return infos


def data_members(infos: list[zipfile.ZipInfo]) -> list[zipfile.ZipInfo]:
    return [
        i
        for i in infos
        if not i.is_dir()
        and not i.filename.startswith(_IGNORED_MEMBER_PREFIXES)
        and "/._" not in i.filename
        and not i.filename.startswith("._")
    ]


# What ``convert.unwrap`` writes into a job's work directory: a decompressed gzip/bz2 stream
# and a zip's one member. Like every input ``unwrap`` hands a reader, they are the pipeline's
# own: a UTF-8 version replaces one (``text.to_utf8``), and ``convert_file`` deletes it once
# read.
DECOMPRESSED = "decompressed"
UNZIPPED = "unzipped"


def extract_single_member(path: Path, dst_dir: Path, cap: int) -> tuple[Path, str]:
    """Extract the only data file in a zip. Returns (extracted path, member name)."""
    members = data_members(check_zip(path, cap))
    if len(members) != 1:
        names = [m.filename for m in members[:10]]
        raise InvalidInput(
            f"Zip archive must contain exactly one data file; found {len(members)}.",
            hint="Send one data file per call.",
            members=names,
        )
    member = members[0]
    name = Path(member.filename).name
    # Never trust the member path: write to a fixed name inside our own temp directory.
    dst = dst_dir / (UNZIPPED + "".join(Path(name).suffixes[-2:]))
    try:
        with zipfile.ZipFile(path) as zf, zf.open(member) as src, dst.open("wb") as out:
            copy_capped(src, out, cap)
    except (*_CORRUPT, zipfile.BadZipFile, lzma.LZMAError) as exc:
        # A member whose data does not inflate ('invalid distance too far back') or whose CRC
        # fails ('Bad CRC-32'): untyped 500s before, as nothing here caught them.
        raise _corrupt("zip", exc) from exc
    except NotImplementedError as exc:  # a RuntimeError: before that clause
        # A compression method zipfile does not implement (deflate64, from Windows Explorer).
        raise InvalidInput(
            f"Could not extract the zip's data file: {without_paths(exc)}",
            hint="Re-create the archive with deflate compression, or send the file itself.",
        ) from exc
    except RuntimeError as exc:
        # zipfile's other RuntimeError here: "File <ZipInfo ...> is encrypted, password required
        # for extraction". The archive may be fine; no password can be passed.
        raise InvalidInput(
            "The zip's data file is password-protected.",
            hint="Re-create the archive without a password, or send the file itself.",
        ) from exc
    return dst, name


def ensure_size(path: Path, cap: int) -> int:
    size = path.stat().st_size
    if size > cap:
        raise InputTooLarge(
            f"Input is {size} bytes; the limit is {cap} bytes.",
            hint="Split the file or send a smaller extract.",
            limit_bytes=cap,
        )
    return size


def copy_file(src: Path, dst: Path) -> None:
    shutil.copyfile(src, dst)


def link_or_copy(src: Path, dst: Path) -> None:
    """Put ``src`` at ``dst``: a hard link, which takes no space, else (another filesystem) a copy.

    Never over an existing ``dst``: that may be the caller's file.
    """
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError:
        copy_file(src, dst)
