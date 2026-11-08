"""A gzip/bz2/zip stream is bounded at 256 MB again; a workbook's XML has its own 512 MB cap.

Incident (final-isolate review, rounds 1-3): one ``max_decompressed_bytes`` capped both a
stream's decompressed file and the sum of a workbook's XML parts. Round 1 lowered it to 256 MB
to bound streams, which refused a 27.5 MB workbook whose 200-row sheet converted before
(test_final_isolate_workbook.py), so round 2 restored 512 MB, and a 300 MB gzip CSV was then
inflated into the RAM-backed work directory until the job's 700 MB watchdog stopped it. The
workbook check now uses ``max_workbook_xml_bytes`` (512 MB), and streams are back at 256 MB.
"""

from __future__ import annotations

import gzip
import zlib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import aimart_testkit as kit
from aimart_parquet.config import Settings
from aimart_parquet.engine.convert import unwrap
from aimart_parquet.engine.limits import Limits
from aimart_parquet.errors import InputTooLarge

if TYPE_CHECKING:
    from collections.abc import Callable

_MB = 1024 * 1024
STREAM_MB = 300


@pytest.fixture(scope="module")
def big_gzip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A CSV that decompresses to 300 MB, about 0.3 MB on the wire."""
    path = tmp_path_factory.mktemp("stream") / "big.csv.gz"
    block = b"1,2\n" * (_MB // 4)
    with gzip.open(path, "wb", compresslevel=9) as out:
        out.write(b"a,b\n")
        for _ in range(STREAM_MB):
            out.write(block)
    assert path.stat().st_size < 2 * _MB
    return path


def _body(path: Path) -> dict[str, str]:
    return {"content_base64": kit.b64(path.read_bytes()), "filename": path.name}


def test_the_defaults_split_streams_from_workbooks() -> None:
    settings = Settings(_env_file=None)
    assert settings.max_decompressed_bytes == 256 * _MB
    assert settings.max_workbook_xml_bytes == 512 * _MB
    limits = Limits.from_settings(settings)
    assert limits.max_decompressed_bytes == 256 * _MB
    assert limits.max_workbook_xml_bytes == 512 * _MB
    # The dataclass defaults are the settings defaults. Isolated jobs hold DuckDB under the job
    # cap (perf/csv-memory, merged after this test: 400 MiB and 300 MiB of spill under 700),
    # so only an in-process service applies duckdb_memory_limit as configured.
    assert Limits() == Limits.from_settings(Settings(_env_file=None, isolate_jobs=False))
    assert (limits.duckdb_memory_limit, limits.duckdb_max_temp_size) == ("400MiB", "300MiB")


def test_a_300_mb_stream_stops_at_256_mb(big_gzip: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(InputTooLarge, match=r"^Decompressed input exceeds the 256 MB limit\.$"):
        unwrap(big_gzip, "big.csv.gz", work, Limits.from_settings(Settings(_env_file=None)))
    # Stopped as the 257th MB arrived: what reached the RAM-backed disk is at most the cap
    # (beside it, the 0.3 MB input copied into the work directory).
    assert (work / "decompressed").stat().st_size <= 256 * _MB


@pytest.mark.anyio
async def test_a_300_mb_stream_is_an_unpaid_413_on_the_wire(
    big_gzip: Path, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    assert paid_app.settings.max_decompressed_bytes == 256 * _MB  # the default, unchanged
    r = await paid_app.paying.post("/v1/convert", json=_body(big_gzip))
    assert r.status_code == 413, r.text
    error = r.json()["error"]
    assert error["code"] == "input_too_large"
    assert error["message"] == "Decompressed input exceeds the 256 MB limit."
    assert error["details"] == {"limit_bytes": 256 * _MB}
    assert "payment-response" not in r.headers
    assert facilitator.settled == []


def _bomb(path: Path, byte: bytes) -> Path:
    """300 MB of text that is not UTF-8, ``byte`` over and over, in under 1 MB of gzip of one
    member (a gzip of more than one is unpacked, as at d83c738)."""
    row = b"1," + byte * 1021 + b"\n"
    block = row * (_MB // len(row))
    deflate = zlib.compressobj(6, zlib.DEFLATED, 31)  # 31: one gzip member
    with path.open("wb") as out:
        out.write(deflate.compress(b"a,b\n"))
        for _ in range(STREAM_MB):
            out.write(deflate.compress(block))
        out.write(deflate.flush())
    assert path.stat().st_size < _MB
    return path


@pytest.fixture(scope="module")
def bombs(tmp_path_factory: pytest.TempPathFactory) -> Callable[[bytes], Path]:
    root = tmp_path_factory.mktemp("bombs")
    return lambda byte: _bomb(root / f"{byte.hex()}.csv.gz", byte)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("byte", "encoding"),
    # Each is three bytes of UTF-8: 0x80 named cp1252 is '€'; 0xE9 detected (as cp874, from a
    # sample of nothing else) is Thai. At 4510071 both ran into the watchdog.
    [(b"\xe9", None), (b"\x80", "cp1252")],
    ids=["detected", "named"],
)
async def test_a_300_mb_stream_of_text_that_is_not_utf8_stops_at_256_mb(
    bombs: Callable[[bytes], Path],
    make_settings: Callable[..., Settings],
    byte: bytes,
    encoding: str | None,
) -> None:
    # The isolated child at the production defaults: the 256 MB stream cap and a 700 MB job.
    # Since 1886f60 such text was transcoded from the gzip, its input alone capped: about 450 MB
    # of UTF-8 were written before the job's watchdog stopped it, a 413 that blamed the job's
    # memory ('The job exceeded its memory budget (700 MB) ...') where d83c738 refused the
    # stream itself. The stream is counted to its end before any UTF-8 is written.
    settings = make_settings(isolate_jobs=True)
    assert (settings.job_memory_limit_mb, settings.max_decompressed_bytes) == (700, 256 * _MB)
    facilitator = kit.FakeFacilitator()
    body: dict[str, object] = dict(_body(bombs(byte)))
    if encoding:
        body["options"] = {"encoding": encoding}
    async with kit.running_app(settings, facilitator=facilitator) as h:
        r = await h.paying.post("/v1/convert", json=body)
    assert r.status_code == 413, r.text
    error = r.json()["error"]
    assert error["code"] == "input_too_large"
    assert error["message"] == "Decompressed input exceeds the 256 MB limit."
    assert error["hint"] == "Split the file, or send a smaller extract."
    assert error["details"] == {"limit_bytes": 256 * _MB}
    assert "payment-response" not in r.headers
    assert facilitator.settled == []


def test_the_docs_give_both_caps_as_configured() -> None:
    root = Path(__file__).resolve().parents[2]
    settings = Settings(_env_file=None)
    stream = settings.max_decompressed_bytes // _MB
    workbook = settings.max_workbook_xml_bytes // _MB

    def text(name: str) -> str:
        return " ".join((root / name).read_text(encoding="utf-8").split())

    for name in ("SECURITY.md", "deploy/README.md"):
        assert f"`AIMART_MAX_DECOMPRESSED_BYTES` ({stream} MB)" in text(name), name
        assert f"`AIMART_MAX_WORKBOOK_XML_BYTES` ({workbook} MB)" in text(name), name
    assert f"`max_decompressed_bytes` ({stream} MB)" in text("docs/architecture.md")
    assert f"`max_workbook_xml_bytes` ({workbook} MB)" in text("docs/architecture.md")
    assert (
        f"| decompressed size | {stream} MB for a gzip, bz2 or zip stream; {workbook} MB for the "
        "XML of all the sheets of an xlsx or ods |"
    ) in text("README.md")
    guide = text("docs/agents-guide.md")
    assert f'"Decompressed input exceeds the {stream} MB limit."' in guide
    assert f"over the {settings.max_decompressed_bytes} byte limit." in guide
    assert f"over the {settings.max_workbook_xml_bytes} byte limit for a workbook." in guide
