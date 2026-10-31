"""``content_base64`` is checked once, by ``sources.check_base64``, before the quote and after.

``Service.check_input`` used to count a base64 input without looking at it, so "abc" (bad
padding) and "" (nothing to convert) were quoted and, paid, verified before ``decode_base64``
or the reader refused them. Both checks now come from the one helper, so they cannot disagree.
"""

from __future__ import annotations

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

BAD = ["abc", "a", "YQ", "café", "", "!!!!", " \n ", "data:text/csv;base64,", "data:,"]


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
        ("abc", "content_base64 is not valid base64."),
        ("café", "content_base64 is not valid base64."),
        ("", "content_base64 is empty."),
        ("====", "content_base64 is empty."),
        ("data:text/csv;base64,", "content_base64 is empty."),
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
    "value", ["YSxiCjEsMgo=", "data:text/csv;base64,YSxiCjEsMgo=", "YSxi\nCjEs\nMgo="]
)
def test_check_base64_returns_the_bytes(value: str, tmp_path: Path) -> None:
    assert check_base64(value, max_bytes=100) == b"a,b\n1,2\n"
    assert decode_base64(value, tmp_path / "out", max_bytes=100) == 8


def test_check_input_applies_the_input_limit(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings(max_input_bytes=1024 * 1024)
    service = Service(settings, build_store(settings))
    with pytest.raises(InputTooLarge, match="larger than the 1 MB limit"):
        service.check_input(SourceRef(content_base64="A" * (2 * 1024 * 1024)))
    service.check_input(SourceRef(content_base64="YQ=="))  # one byte is enough
