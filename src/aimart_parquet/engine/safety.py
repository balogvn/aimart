"""Guards that run before any parser sees untrusted bytes.

The threat is not malformed data — parsers reject that — but small inputs that expand into
huge ones (gzip/zip bombs, an xlsx whose XML parts inflate 1000x) and exhaust memory or disk
on a free-tier host. Every expansion here is streamed and counted against a hard cap, and zip
archives are checked from their central directory before a single member is inflated.
"""

from __future__ import annotations

import bz2
import gzip
import shutil
import zipfile
from pathlib import Path
from typing import IO, Literal, Protocol

from aimart_parquet.errors import InputTooLarge, InvalidInput

Compression = Literal["gzip", "bz2", "zip"]

_CHUNK = 1024 * 1024
# A legitimate CSV compresses ~10x and a very repetitive one ~100x; an xlsx part rarely more.
# 250x and up is the signature of a crafted bomb.
MAX_ZIP_RATIO = 250
MAX_ZIP_MEMBERS = 10_000
_IGNORED_MEMBER_PREFIXES = ("__MACOSX/", ".DS_Store")


def read_magic(path: Path, n: int = 600) -> bytes:
    with path.open("rb") as fh:
        return fh.read(n)


def sniff_compression(magic: bytes) -> Compression | None:
    if magic.startswith(b"\x1f\x8b"):
        return "gzip"
    if magic.startswith(b"BZh"):
        return "bz2"
    if magic.startswith(b"PK\x03\x04"):
        return "zip"
    return None


class _Readable(Protocol):
    def read(self, size: int = ..., /) -> bytes: ...


def copy_capped(src: _Readable, dst: IO[bytes], cap: int) -> int:
    """Stream src to dst, raising InputTooLarge as soon as more than ``cap`` bytes appear."""
    total = 0
    while chunk := src.read(_CHUNK):
        total += len(chunk)
        if total > cap:
            raise InputTooLarge(
                f"Decompressed input exceeds the {cap // (1024 * 1024)} MB limit.",
                hint="Split the file, or send a smaller extract.",
                limit_bytes=cap,
            )
        dst.write(chunk)
    return total


def decompress_stream(
    src_path: Path, dst_path: Path, kind: Literal["gzip", "bz2"], cap: int
) -> int:
    opener = gzip.open if kind == "gzip" else bz2.open
    try:
        with opener(src_path, "rb") as src, dst_path.open("wb") as dst:
            return copy_capped(src, dst, cap)
    except (OSError, EOFError) as exc:
        raise InvalidInput(
            f"The {kind} stream is corrupt or truncated: {exc}",
            hint="Re-compress the file, or send it uncompressed.",
        ) from exc


def check_zip(path: Path, cap: int) -> list[zipfile.ZipInfo]:
    """Validate a zip from its central directory without inflating anything."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise InvalidInput(
            f"Not a readable zip archive: {exc}", hint="Re-create the archive or send the file."
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
    dst = dst_dir / ("unzipped" + "".join(Path(name).suffixes[-2:]))
    with zipfile.ZipFile(path) as zf, zf.open(member) as src, dst.open("wb") as out:
        copy_capped(src, out, cap)
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
