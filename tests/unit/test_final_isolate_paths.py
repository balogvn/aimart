"""No caller-visible message quotes a server path, however the native library words its error.

Incident (final security review): converting a malformed Parquet input answered 422 with
pyarrow's own text, "Not a readable Parquet file: Could not open Parquet input source
'/tmp/aimart-artifacts/.work/tmpXXXX/inline': ...", telling callers the server's work layout.
Query and preview already stripped paths with a helper of their own; the Parquet reader of
convert, inspect and the quantum tools did not. The helper now lives in one place
(``errors.without_paths``) and every message built from an exception's text goes through it.
The quantum loader also raised a bare OSError for a file whose pages are corrupt (an untyped
500 from the worker); it is a typed 422 now.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import aimart_testkit as kit
from aimart_parquet.artifacts import build_store
from aimart_parquet.engine.inspect import inspect_parquet
from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.engine.tabular import preview
from aimart_parquet.errors import InvalidInput, without_paths
from aimart_parquet.service import Service, SourceRef

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from aimart_parquet.config import Settings

THRIFT_BOMB = b"PAR1" + b"\x00" * 64 + b"PAR1"  # the reviewer's input


def assert_no_path(text: str, *where: Path) -> None:
    for place in where:
        assert str(place) not in text
        assert str(place.resolve()) not in text
    assert ".work" not in text
    assert "/tmp" not in text
    assert "/var/" not in text
    assert "/Users/" not in text


# ------------------------------------------------------------------------------ the helper
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "Could not open Parquet input source '/tmp/aimart-artifacts/.work/tmp1/inline': x",
            'Could not open Parquet input source "<file>": x',
        ),
        (
            'IO Error: Cannot open file "/srv/x/data.parquet": denied',
            'IO Error: Cannot open file "<file>": denied',
        ),
        (
            "Failed to open /tmp/aimart-job-1/aimart-q-2/inputs/t0.parquet now",
            "Failed to open <file> now",
        ),
        ("line one\n'/secret/second/line'", "line one"),  # the first line only
        ("ratio 3/4, N/A, km/h and 12/31/2025 stay", "ratio 3/4, N/A, km/h and 12/31/2025 stay"),
        ("a lone / and /single stay", "a lone / and /single stay"),
    ],
)
def test_without_paths(raw: str, expected: str) -> None:
    assert without_paths(raw) == expected
    assert without_paths(ValueError(raw)) == expected


def test_without_paths_names_directories_and_the_type_of_a_silent_error(tmp_path: Path) -> None:
    spill = tmp_path / "aimart-q-1"
    text = f"Binder Error: {spill}/inputs/t0.parquet and {spill.resolve()}/x"
    assert without_paths(text, spill) == "Binder Error: <tmp>/inputs/t0.parquet and <tmp>/x"
    assert without_paths(OSError()) == "OSError"


# ------------------------------------------------------------------------------ every caller
@pytest.fixture
def service(make_settings: Callable[..., Settings]) -> Service:
    settings = make_settings()
    return Service(settings, build_store(settings))


@pytest.mark.anyio
async def test_convert_of_a_malformed_parquet_names_no_server_path(service: Service) -> None:
    src = SourceRef(content_base64=kit.b64(THRIFT_BOMB), filename="x.parquet")
    with pytest.raises(InvalidInput) as info:
        await service.convert(src, ConvertOptions())
    assert info.value.message.startswith("Not a readable Parquet file: ")
    assert_no_path(str(info.value.to_dict()), service.settings.artifact_dir)


def test_engine_readers_name_no_server_path(tmp_path: Path) -> None:
    # A file pyarrow cannot open is named in its error: quoted by ParquetFile ("Failed to open
    # local file '/...'"), bare by read_table ("/.../inline").
    folder = tmp_path / "server" / "work"
    folder.mkdir(parents=True)
    missing = folder / "inline"
    from aimart_parquet.engine.readers.columnar import read_parquet_input
    from aimart_parquet.quantum.data import open_schema

    calls: list[Callable[[], object]] = [
        lambda: inspect_parquet(missing),
        lambda: preview(missing, columns=None, offset=0, limit=1, fmt="json", max_chars=100),
        lambda: open_schema(missing),
        lambda: read_parquet_input(None, missing, folder, ConvertOptions(), None, None),  # type: ignore[arg-type]
    ]
    for call in calls:
        with pytest.raises(InvalidInput) as info:
            call()
        assert "Parquet" in info.value.message
        assert "<file>" in info.value.message
        assert_no_path(info.value.message, folder)


def test_quantum_loader_refuses_corrupt_pages_with_a_typed_error(tmp_path: Path) -> None:
    from aimart_parquet.quantum.data import load_matrix

    path = tmp_path / "server" / "prices.parquet"
    path.parent.mkdir()
    table = pa.table({"x": [float(i) for i in range(1000)], "y": [float(i) for i in range(1000)]})
    pq.write_table(table, path, compression="snappy")
    data = bytearray(path.read_bytes())
    data[4:3000] = b"\xff" * (3000 - 4)  # the pages, not the footer
    path.write_bytes(bytes(data))
    with pytest.raises(
        InvalidInput
    ) as info:  # was a bare OSError: "Internal error" from the worker
        load_matrix(path, ["x", "y"])
    assert info.value.http_status == 422
    assert "corrupt" in info.value.message
    assert "not charged" in (info.value.hint or "")
    assert_no_path(info.value.message, path)
