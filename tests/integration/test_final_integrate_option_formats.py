"""An option a file's format never reads is not refused before the quote; one it reads is.

``pdf_pages``, ``encoding`` and ``delimiter`` became ConvertOptions field validators, so they
refused every format: a 2-row CSV with ``{"pdf_pages": "0"}`` or ``"1-3,"``, and an xlsx with
``{"encoding": "binary"}`` or ``{"delimiter": '"'}``, converted and settled at 07fa450 and were
a 400 before the quote at bebfe33, on REST, the upload form and MCP alike: a lost sale on a
value that format never reads.

Now each is refused before the quote only when the request names a format whose reader reads
it (``options.format``, else the filename's extension, else a url's path), with the error that
reader raises. With no name and no format, the reader refuses it after the verify, typed and
unpaid: ``delimiter`` '"' reached csv.reader there once, a bare 500. And an SPSS portable
file (.por) with any ``encoding`` was a 500: pyreadstat's read_por takes none.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from fpdf import FPDF

import aimart_testkit as kit

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.anyio

CSV = b"id,amount\n1,1.5\n2,2.5\n"
PAGES = "pdf_pages range '0' is invalid: pages start at 1, and a range runs low to high."
DELIMITER = "delimiter '\"' cannot separate fields: it is the quote character. Omit delimiter to "
DELIMITER += "detect it."


def _pdf(path: Path) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=9)
    with pdf.table() as table:
        table.row(["Region", "Units"])
        for i in range(4):
            table.row([f"R{i}", str(10 + i)])
    pdf.output(str(path))
    return path.read_bytes()


def _files(tmp_path: Path) -> dict[str, bytes]:
    return {
        "a.csv": CSV,
        "r.xlsx": kit.messy_workbook(tmp_path / "w.xlsx").read_bytes(),
        "t.pdf": _pdf(tmp_path / "t.pdf"),
        "s.por": kit.survey_file(tmp_path / "s.por", "por").read_bytes(),
    }


# (filename, options): the reviewer's four lost sales, and more of the same kind.
UNREAD: list[tuple[str, dict[str, Any]]] = [
    ("a.csv", {"pdf_pages": "0"}),
    ("a.csv", {"pdf_pages": "1-3,"}),
    ("r.xlsx", {"encoding": "binary"}),
    ("r.xlsx", {"delimiter": '"'}),
    ("t.pdf", {"encoding": "rot13", "delimiter": "\n"}),
    ("a.csv", {"format": "csv", "pdf_pages": "5-2"}),
    # pyreadstat's read_por takes no encoding: any encoding was a TypeError, a 500 (07fa450 too).
    ("s.por", {"encoding": "cp1252"}),
]
# (filename, options, message): a value the named format's reader reads and can never use.
READ: list[tuple[str | None, dict[str, Any], str]] = [
    ("t.pdf", {"pdf_pages": "0"}, PAGES),
    ("a.csv", {"encoding": "rot13"}, "'rot13' is not a text encoding."),
    ("a.csv", {"delimiter": '"'}, DELIMITER),
    ("a.csv.gz", {"encoding": "no-such-codec"}, "Unknown encoding 'no-such-codec'."),
    ("data.json", {"encoding": "base64"}, "'base64' is not a text encoding."),
    (None, {"format": "tsv", "delimiter": '"'}, DELIMITER),
    ("x.bin", {"format": "pdf", "pdf_pages": "0"}, PAGES),
]


def _id(value: Any) -> str:
    return json.dumps(value)[:40]


async def _mcp(h: kit.AppHarness, args: dict[str, Any]) -> tuple[bool, dict[str, Any], bool]:
    """(is_error, structured content, whether a price was quoted) for a paid MCP convert."""
    async with kit.mcp_over_http(h.app) as client:
        result = await client.call_tool("convert_to_parquet", args)
        structured = result.structured_content or {}
        quoted = "accepts" in structured
        if quoted:
            meta = await kit.mcp_payment_meta(kit.paying_client(), structured)
            result = await client.call_tool("convert_to_parquet", args, meta=meta)
    return result.is_error, result.structured_content or {}, quoted


@pytest.mark.parametrize(("name", "options"), UNREAD, ids=_id)
async def test_an_option_the_format_never_reads_converts_and_settles(
    name: str, options: dict[str, Any], paid_app: kit.AppHarness, tmp_path: Path
) -> None:
    fake = paid_app.facilitator
    assert fake is not None
    data = _files(tmp_path)[name]
    body = {"content_base64": kit.b64(data), "filename": name, "options": options}
    r = await paid_app.paying.post("/v1/convert", json=body)
    assert r.status_code == 200, r.text  # before: 400 invalid_request, before the quote
    r = await paid_app.paying.post(
        "/v1/convert/upload",
        data={"options": json.dumps(options)},
        files={"file": (name, data, "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    is_error, structured, quoted = await _mcp(paid_app, body)
    assert quoted
    assert not is_error, structured
    assert len(fake.verified) == len(fake.settled) == 3


@pytest.mark.parametrize(("name", "options", "message"), READ, ids=_id)
@pytest.mark.parametrize("paying", [False, True], ids=["unpaid", "paid"])
async def test_an_option_the_named_format_cannot_use_is_refused_before_the_quote(
    name: str | None,
    options: dict[str, Any],
    message: str,
    paying: bool,
    paid_app: kit.AppHarness,
) -> None:
    fake = paid_app.facilitator
    assert fake is not None
    client = paid_app.paying if paying else paid_app.http
    body: dict[str, Any] = {"content_base64": kit.b64(CSV), "options": options}
    if name is not None:
        body["filename"] = name
    responses = [
        await client.post("/v1/convert", json=body),
        await client.post(
            "/v1/convert/upload",
            data={"options": json.dumps(options)},
            files={"file": (name or "upload", CSV, "application/octet-stream")},
        ),
    ]
    for r in responses:
        assert r.status_code == 400, r.text
        assert "payment-required" not in r.headers
        error = r.json()["error"]
        assert error["code"] == "invalid_request"
        assert error["message"] == message
        assert error["hint"]
    is_error, structured, quoted = await _mcp(paid_app, body)
    assert is_error
    assert not quoted
    assert structured["error"]["message"] == message
    assert fake.verified == []
    assert fake.settled == []


async def test_a_url_names_its_format_by_its_path(paid_app: kit.AppHarness) -> None:
    body = {"url": "https://e.example/exports/a.csv", "options": {"encoding": "rot13"}}
    r = await paid_app.http.post("/v1/convert", json=body)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"] == "'rot13' is not a text encoding."
    body["url"] = "https://e.example/exports/a.xlsx"
    r = await paid_app.http.post("/v1/convert", json=body)
    assert r.status_code == 402  # an xlsx never reads encoding: quoted


# (bytes, options): with no name or format the value is only found by the reader that reads it.
UNNAMED: list[tuple[str, dict[str, Any], str]] = [
    ("a.csv", {"delimiter": '"'}, DELIMITER),  # before: csv.reader's ValueError, a bare 500
    ("a.csv", {"encoding": "rot13"}, "'rot13' is not a text encoding."),
    ("t.pdf", {"pdf_pages": "0"}, PAGES),
]


@pytest.mark.parametrize(("name", "options", "message"), UNNAMED, ids=_id)
async def test_without_a_name_the_reader_refuses_the_value_unpaid(
    name: str, options: dict[str, Any], message: str, paid_app: kit.AppHarness, tmp_path: Path
) -> None:
    fake = paid_app.facilitator
    assert fake is not None
    body = {"content_base64": kit.b64(_files(tmp_path)[name]), "options": options}
    quote = await paid_app.http.post("/v1/convert", json=body)
    assert quote.status_code == 402  # the format is not known before the file is read
    r = await paid_app.paying.post("/v1/convert", json=body)
    assert r.status_code == 400, r.text
    error = r.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["message"] == message
    assert len(fake.verified) == 1
    assert fake.settled == []
