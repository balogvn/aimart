"""``content_base64`` is checked once, by ``sources.check_base64``, before the quote and after.

``Service.check_input`` used to count a base64 input without looking at it, so "a" (one data
character) and "" (nothing to convert) were quoted and, paid, verified before ``decode_base64``
or the reader refused them. Both checks now come from the one helper, so they cannot disagree.

The decoder used to drop every character it did not know (``validate=False``): URL-safe base64
lost its '-' and '_', every later 6-bit group shifted, and a 5-row CSV with 4 such characters
was converted, and charged for, as 1 row of garbage. Now every character counts: the URL-safe
alphabet and missing padding are read as what they unambiguously mean, anything else is refused.
"""

from __future__ import annotations

import base64
import os
from typing import TYPE_CHECKING

import pytest

from aimart_parquet.artifacts import build_store
from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.errors import AimartError, InputTooLarge, InvalidRequest
from aimart_parquet.service import Service, SourceRef
from aimart_parquet.sources import check_base64, decode_base64

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

BAD = [
    "a",
    "YWJjZ",  # 4k + 1 data characters: no padding can complete it
    "café",
    "",
    "!!!!",
    " \n ",
    "data:text/csv;base64,",
    "data:,",
    "data:text/csv,a%2Cb%0A1%2C2",  # percent-encoded text, not base64
    "data:text/csv;base64",  # no comma: no data
    "YSxi*CjEsMgo=",
    "YSxiCjEs.Mgo=",
    "YSxiCjEsMgo=YQ==",  # data after the padding
    "====",
]


@pytest.mark.parametrize("value", BAD)
async def test_check_input_raises_what_convert_raises(value: str, settings: Settings) -> None:
    service = Service(settings, build_store(settings))
    src = SourceRef(content_base64=value, filename="x.csv")
    with pytest.raises(AimartError) as precheck:
        service.check_input(src)
    with pytest.raises(AimartError) as served:
        await service.convert(src, ConvertOptions())
    assert isinstance(precheck.value, InvalidRequest)
    assert (precheck.value.code, precheck.value.message, precheck.value.hint) == (
        served.value.code,
        served.value.message,
        served.value.hint,
    )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("a", "content_base64 is not valid base64."),
        ("café", "content_base64 is not valid base64."),
        ("YSxi*CjEsMgo=", "content_base64 is not valid base64."),
        # Before: '*' and '.' were dropped, and 4 of them shifted the bytes with no error.
        ("YSxi*CjE.sMg*o.", "content_base64 is not valid base64."),
        # Before: "empty", as the padding alone was dropped. It is no base64 at all.
        ("====", "content_base64 is not valid base64."),
        ("", "content_base64 is empty."),
        (" \r\n", "content_base64 is empty."),
        ("data:text/csv;base64,", "content_base64 is empty."),
        ("data:text/csv,a%2Cb", "content_base64 is a data: URL that is not base64-encoded."),
        ("data:YSxiCjEsMgo=", "content_base64 is a data: URL that is not base64-encoded."),
    ],
)
def test_check_base64_refusals(value: str, message: str, tmp_path: Path) -> None:
    with pytest.raises(InvalidRequest) as info:
        check_base64(value, max_bytes=100)
    assert info.value.message == message
    with pytest.raises(InvalidRequest):
        decode_base64(value, tmp_path / "out", max_bytes=100)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "value",
    [
        "YSxiCjEsMgo=",
        "data:text/csv;base64,YSxiCjEsMgo=",
        "DATA:text/csv;charset=utf-8;BASE64,YSxiCjEsMgo=",
        # base64 named other than as the header's exact end: decoded at 07fa450, then refused.
        "data:text/csv; base64,YSxiCjEsMgo=",
        "data:text/csv;base64;charset=utf-8,YSxiCjEsMgo=",
        "data:text/csv;base64 ,YSxiCjEsMgo=",
        "data:;base64,YSxiCjEsMgo=",
        "YSxi\nCjEs\nMgo=",
        "YSxi\r\nCjEs\r\nMgo=\r\n",
        " YSxi\tCjEsMgo= ",
        "YSxiCjEsMgo",  # unpadded, as Node's 'base64url' and JWT tools write it
    ],
)
def test_check_base64_returns_the_bytes(value: str, tmp_path: Path) -> None:
    assert check_base64(value, max_bytes=100) == b"a,b\n1,2\n"
    assert decode_base64(value, tmp_path / "out", max_bytes=100) == 8


def test_url_safe_base64_round_trips_byte_for_byte() -> None:
    # Before: 987 of 2,000 random binary payloads (what xlsx, sav or parquet look like) decoded,
    # with no error, to other bytes; none decoded correctly.
    rng = __import__("random").Random(7)
    for size in range(1, 400):
        raw = rng.randbytes(size)
        safe = base64.urlsafe_b64encode(raw).decode()
        for value in (safe, safe.rstrip("="), base64.encodebytes(raw).decode()):
            assert check_base64(value, max_bytes=1000) == raw, value


def test_the_reviewers_url_safe_csv_decodes_to_what_was_sent() -> None:
    # 5 rows, 4 URL-safe characters in its encoding: it decoded to 102 bytes for 105 sent.
    rows = "".join(f"{i},café ¿{i * 7}?,{i * 3.5}\n" for i in range(5))
    raw = f"id,label,value\n{rows}".encode()
    value = base64.urlsafe_b64encode(raw).decode()
    assert sum(value.count(c) for c in "-_") % 4 == 0 < sum(value.count(c) for c in "-_")
    assert check_base64(value, max_bytes=1000) == raw


def test_a_large_payload_is_still_quick() -> None:
    # Run before the quote in the API process, on up to the 16 MiB a JSON body holds.
    import time

    raw = os.urandom(12 * 1024 * 1024)
    value = base64.encodebytes(raw).decode()  # line-wrapped: the slowest accepted form
    started = time.perf_counter()
    assert check_base64(value, max_bytes=50 * 1024 * 1024) == raw
    assert time.perf_counter() - started < 2.0


def test_check_input_applies_the_input_limit(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings(max_input_bytes=1024 * 1024)
    service = Service(settings, build_store(settings))
    with pytest.raises(InputTooLarge, match="larger than the 1 MB limit"):
        service.check_input(SourceRef(content_base64="A" * (2 * 1024 * 1024)))
    service.check_input(SourceRef(content_base64="YQ=="))  # one byte is enough


@pytest.mark.parametrize("value", ["A" * 140, "A" * 1000, "AAAA\n" * 50])
def test_text_too_long_for_the_limit_is_refused_before_it_is_copied(value: str) -> None:
    # 100 bytes encode to 136 characters; line breaks do not count, but twice that is refused
    # before the text is copied at all.
    with pytest.raises(InputTooLarge) as info:
        check_base64(value, max_bytes=100)
    assert info.value.details["limit_bytes"] == 100
    assert check_base64("AAAA\n" * 34, max_bytes=102) == b"\x00" * 102
