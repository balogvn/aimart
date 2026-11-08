"""A compressed input whose data cannot be inflated is a typed 422, never a bare 500.

Incident (csv-followup, found probing 1886f60's gzip paths at d83c738): Python's gzip and
zipfile modules let ``zlib.error`` through for deflate data zlib refuses ('invalid block type',
'invalid distance too far back'), zipfile raises ``BadZipFile`` for a member whose CRC fails,
``RuntimeError`` for an encrypted one and ``NotImplementedError`` for deflate64. None of them is
an ``OSError``, all ``decompress_stream`` caught, and ``extract_single_member`` caught nothing:
each was the 500 whose hint says to retry, for a file that can never convert. The merged gzip
paths (``gzip_head``, ``open_gzip``, which ``to_utf8`` transcodes from) let the same error
through. They are refused as corrupt, with the same path-free messages as every other stream
error, or (encrypted, deflate64) with what to send instead.
"""

from __future__ import annotations

import gzip
import io
import zipfile
from typing import TYPE_CHECKING

import pytest

import aimart_testkit as kit
from aimart_parquet.engine import safety
from aimart_parquet.errors import InvalidInput

if TYPE_CHECKING:
    from pathlib import Path

TEXT = b"a,b\n" + b"".join(b"%d,%d\n" % (i, i * 7) for i in range(100_000))
GOOD = gzip.compress(TEXT)
# A valid gzip header, then bytes that are not deflate data at all.
NOT_DEFLATE = b"\x1f\x8b\x08\x00" + b"\0" * 6 + b"not deflate at all, really not"
# A byte flipped inside the deflate data, past what the first KB inflates from.
FLIPPED = GOOD[:200] + bytes([GOOD[200] ^ 0xFF]) + GOOD[201:]


def _zip(data: bytes = TEXT) -> bytearray:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.csv", data)
    return bytearray(buf.getvalue())


def _bad_deflate_zip() -> bytes:
    z = _zip()
    z[30 + len("a.csv") + 50] ^= 0xFF  # inside the member's deflate data
    return bytes(z)


def _bad_crc_zip() -> bytes:
    z = _zip(b"a,b\n1,2\n")
    z[14] ^= 0xFF  # the local header's CRC-32: the data inflates, the check fails
    central = z.rfind(b"PK\x01\x02")
    z[central + 16] ^= 0xFF  # and the central directory's, so the two agree
    return bytes(z)


@pytest.mark.parametrize(
    ("name", "data", "kind"),
    [
        ("a.csv.gz", NOT_DEFLATE, "gzip"),
        ("a.json.gz", NOT_DEFLATE, "gzip"),
        ("a.csv.gz", FLIPPED, "gzip"),
        ("a.zip", _bad_deflate_zip(), "zip"),
        ("a.zip", _bad_crc_zip(), "zip"),
    ],
    ids=["gzip-not-deflate", "gzip-json-not-deflate", "gzip-flipped", "zip-deflate", "zip-crc"],
)
def test_data_that_cannot_be_inflated_is_corrupt(
    tmp_path: Path, name: str, data: bytes, kind: str
) -> None:
    # Before the fix zlib's 'invalid block type' and 'invalid distance too far back', or
    # zipfile's bad CRC, escaped untyped: the 500 that says to retry.
    with pytest.raises(
        InvalidInput, match=rf"^The {kind} stream is corrupt or truncated: "
    ) as info:
        kit.convert_bytes(tmp_path, name, data)
    assert info.value.http_status == 422  # >= 400: never charged
    assert str(tmp_path) not in info.value.message


def test_the_streamed_gzip_readers_say_corrupt_too(tmp_path: Path) -> None:
    # What to_utf8 transcodes a gzip of non-UTF-8 text through, and what keep_gzip decides the
    # format from: before, both let zlib.error out.
    path = kit.write(tmp_path / "x.gz", NOT_DEFLATE)
    with safety.open_gzip(path, 10**6) as fh, pytest.raises(InvalidInput, match="gzip stream"):
        fh.read()
    assert safety.gzip_head(path, 1024) is None  # unwrap then says what is wrong


def test_a_cp1252_gzip_corrupt_past_its_head_is_corrupt(tmp_path: Path) -> None:
    # Transcoded straight from the stream (1886f60): the corruption is met mid-transcode.
    text = "a,b\n" + "".join(f"{i},Zürich {i}\n" for i in range(100_000))
    good = gzip.compress(text.encode("cp1252"))
    bad = good[: len(good) // 2] + bytes([good[len(good) // 2] ^ 0xFF]) + good[len(good) // 2 + 1 :]
    with pytest.raises(InvalidInput, match=r"^The gzip stream is corrupt or truncated: "):
        kit.convert_bytes(tmp_path, "c.csv.gz", bad)


def test_an_encrypted_zip_member_is_refused_with_what_to_do(tmp_path: Path) -> None:
    # Before: zipfile's RuntimeError ("File <ZipInfo ...> is encrypted, password required for
    # extraction") escaped untyped.
    z = _zip(b"a,b\n1,2\n")
    z[6] |= 0x01  # the local header's 'encrypted' flag
    central = z.rfind(b"PK\x01\x02")
    z[central + 8] |= 0x01  # and the central directory's
    with pytest.raises(
        InvalidInput, match=r"^The zip's data file is password-protected\.$"
    ) as info:
        kit.convert_bytes(tmp_path, "a.zip", bytes(z))
    assert info.value.hint is not None
    assert "without a password" in info.value.hint


def test_a_zip_member_in_a_method_zipfile_lacks_is_refused(tmp_path: Path) -> None:
    # Deflate64 (method 9), which Windows Explorer writes for large files: NotImplementedError
    # ("That compression method is not supported") escaped untyped before.
    z = _zip(b"a,b\n1,2\n")
    z[8] = 9  # the local header's method
    central = z.rfind(b"PK\x01\x02")
    z[central + 10] = 9  # and the central directory's
    with pytest.raises(InvalidInput, match=r"^Could not extract the zip's data file: ") as info:
        kit.convert_bytes(tmp_path, "a.zip", bytes(z))
    assert info.value.hint is not None
    assert "deflate" in info.value.hint
