"""A workbook is bomb-checked by the XML of every part, so the decompression cap is not a budget.

Incident (final-isolate review, round 2, at 230dabd): the round-1 fix lowered
``max_decompressed_bytes`` from 512 MB to 256 MB, sized to the job memory cap from single-sheet
measurements. The excel reader applies the same cap to the sum of an xlsx's parts, every sheet
included, though calamine parses one sheet and writes none of them to disk. A 27.5 MB workbook
of a 200-row 'summary' sheet beside two raw-data sheets of 129.6 MB of XML each converted at
07fa450 and became 413 'Zip archive expands to 271774583 bytes, over the 268435456 byte
limit.' for every sheet, before any was parsed: unpaid, but a lost sale, with a hint ('Split
the archive') an agent cannot act on. The job cap, which now counts a decompressed stream's
file, is what bounds a stream's RAM, so the cap is 512 MB again (ADR 0005).
"""

from __future__ import annotations

import base64
import random
import zipfile
from typing import TYPE_CHECKING

import pytest

import aimart_testkit as kit
from aimart_parquet.artifacts import build_store
from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.errors import InputTooLarge
from aimart_parquet.service import Service, SourceRef

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

_MB = 1024 * 1024
_MAIN = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006"
_SHEET_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
_BOOK_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"


def _rows(first: int, count: int, tag: str = "") -> bytes:
    return "".join(
        f'<row r="{r}"><c r="A{r}"><v>{r}</v></c>'
        f'<c r="B{r}" t="inlineStr"><is><t>{tag}x{r % 7}</t></is></c></row>'
        for r in range(first, first + count)
    ).encode()


def _workbook(path: Path, big_sheet_mb: int, big_sheets: int = 2) -> Path:
    """A 200-row 'summary' sheet beside ``big_sheets`` raw sheets of ``big_sheet_mb`` MB of XML.

    The raw sheets repeat a row under a random tag, so they deflate about 75x: a few MB on the
    wire, below the 250x ratio that marks a crafted bomb, and built in well under a second.
    """
    names = ["summary", *(f"raw_{i}" for i in range(big_sheets))]
    head = f'<?xml version="1.0" encoding="UTF-8"?><worksheet {_MAIN}><sheetData>'.encode()
    tail = b"</sheetData></worksheet>"
    rng = random.Random(7)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        zf.writestr(
            "[Content_Types].xml",
            f'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="{_PKG}/content-types">'
            f'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/xl/workbook.xml" ContentType="{_BOOK_TYPE}"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="{_SHEET_TYPE}"/>'
                for i in range(len(names))
            )
            + "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PKG}/relationships">'
            f'<Relationship Id="rId1" Type="{_REL}/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/workbook.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><workbook {_MAIN} xmlns:r="{_REL}"><sheets>'
            + "".join(
                f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
                for i, n in enumerate(names)
            )
            + "</sheets></workbook>",
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PKG}/relationships">'
            + "".join(
                f'<Relationship Id="rId{i + 1}" Type="{_REL}/worksheet" '
                f'Target="worksheets/sheet{i + 1}.xml"/>'
                for i in range(len(names))
            )
            + "</Relationships>",
        )
        zf.writestr("xl/worksheets/sheet1.xml", head + _rows(2, 200) + tail)
        for s in range(big_sheets):
            with zf.open(f"xl/worksheets/sheet{s + 2}.xml", "w") as out:
                out.write(head)
                written = 0
                while written < big_sheet_mb * _MB:
                    block = _rows(1, 1, tag=f"{rng.getrandbits(64):x}") * 40
                    out.write(block)
                    written += len(block)
                out.write(tail)
    return path


@pytest.fixture(scope="module")
def workbook(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = _workbook(tmp_path_factory.mktemp("workbook") / "workbook.xlsx", big_sheet_mb=136)
    with zipfile.ZipFile(path) as zf:
        xml = sum(i.file_size for i in zf.infolist())
    assert 256 * _MB < xml < 512 * _MB  # over round 1's cap, under the one before it
    assert path.stat().st_size < 8 * _MB  # well inside max_input_bytes
    return path


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def _source(path: Path) -> SourceRef:
    return SourceRef(content_base64=_b64(path), filename=path.name)


async def test_a_small_sheet_beside_large_ones_converts_with_the_defaults(
    make_settings: Callable[..., Settings], workbook: Path
) -> None:
    settings = make_settings()
    service = Service(settings, build_store(settings))
    result = await service.convert(_source(workbook), ConvertOptions(sheet="summary"))
    assert result.report.rows == 200
    assert result.report.sheet == "summary"
    assert result.report.available_sheets == ["summary", "raw_0", "raw_1"]


async def test_the_cap_still_refuses_a_workbook_past_it(
    make_settings: Callable[..., Settings], workbook: Path
) -> None:
    # The cap is still a bomb check over every part, from the directory, before inflating.
    settings = make_settings(max_decompressed_bytes=256 * _MB)
    service = Service(settings, build_store(settings))
    with pytest.raises(InputTooLarge, match="over the 268435456 byte limit"):
        await service.convert(_source(workbook), ConvertOptions(sheet="summary"))


async def test_the_workbook_converts_paid_in_an_isolated_job_under_the_default_cap(
    make_settings: Callable[..., Settings], workbook: Path
) -> None:
    settings = make_settings(isolate_jobs=True)
    assert settings.job_memory_limit_mb == 700
    facilitator = kit.FakeFacilitator()
    body = {
        "content_base64": _b64(workbook),
        "filename": "workbook.xlsx",
        "options": {"sheet": "summary"},
    }
    async with kit.running_app(settings, facilitator=facilitator) as h:
        r = await h.paying.post("/v1/convert", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["report"]["rows"] == 200
    assert len(facilitator.settled) == 1
