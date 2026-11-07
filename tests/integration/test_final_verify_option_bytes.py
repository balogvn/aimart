"""An option is refused before the quote only for the format the engine will read, not the name.

5ecb6ac refused ``encoding``, ``delimiter`` and ``pdf_pages`` before the quote for the format
the request *names*: ``options.format``, else the filename's extension, else a url's path. The
engine decides from the bytes first (``detect_format``: magic bytes beat the name, and a '.csv'
that sniffs as JSON is JSON), it reads a stored artifact under the name it was stored with,
whatever ``filename`` says, and a URL's file is whatever the server sends. So each of these
was refused before the quote with the named format's error, while 07fa450 converted and
settled it, the reader that ran never reading the option: an xlsx named data.csv with encoding
'auto' (which agents send), a JSON array named data.csv with delimiter '"', a url
'export.csv' that serves an xlsx, an upload part named data.csv holding an xlsx, the same over
MCP, and a converted Parquet artifact with ``filename`` 'data.csv'.

Now the format is decided as the engine decides it, from the bytes the request carries
(``detect.format_from_start``): inline base64 whole, an upload from the start of its file part
(``FormPeek.starts``). A url's or a stored artifact's bytes are not known before the quote, so
for them only ``options.format`` decides; the reader refuses the value after the verify,
typed and unpaid, as at 07fa450.
"""

from __future__ import annotations

import functools
import http.server
import json
import threading
from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit
from aimart_parquet import sources
from aimart_parquet.engine.detect import format_from_start
from aimart_parquet.engine.models import InputFormat

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

CSV = b"id,amount\n1,1.5\n2,2.5\n"
JSON_ARRAY = b'[{"id": 1, "amount": 1.5}, {"id": 2, "amount": 2.5}]'
AUTO = "Unknown encoding 'auto'."
DELIMITER = "delimiter '\"' cannot separate fields: it is the quote character. Omit delimiter to "
DELIMITER += "detect it."


def _xlsx(tmp_path: Path) -> bytes:
    return kit.messy_workbook(tmp_path / "w.xlsx").read_bytes()


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def www(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A local server: export.csv is an xlsx (an "Export as CSV" link), feed.csv a JSON array."""
    root = tmp_path / "www"
    root.mkdir()
    (root / "export.csv").write_bytes(_xlsx(tmp_path))
    (root / "feed.csv").write_bytes(JSON_ARRAY)
    (root / "a.csv").write_bytes(CSV)
    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Quiet, directory=str(root))
    )
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    monkeypatch.setattr(sources, "ALLOWED_PORTS", frozenset({80, 443, 8080, 8443, port}))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


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


# (bytes, filename, options): the engine reads these bytes as a format that ignores the option.
MISLABELLED = [
    ("xlsx", "data.csv", {"encoding": "auto"}),
    ("xlsx", "data.csv", {"encoding": "binary"}),
    ("xlsx", "data.csv", {"encoding": "utf8mb4"}),
    ("json", "data.csv", {"delimiter": '"'}),
]


@pytest.mark.parametrize(("kind", "name", "options"), MISLABELLED, ids=str)
async def test_inline_bytes_of_another_format_convert_and_settle(
    kind: str,
    name: str,
    options: dict[str, Any],
    paid_app: kit.AppHarness,
    facilitator: kit.FakeFacilitator,
    tmp_path: Path,
) -> None:
    """Before: 400 before the quote, with the named format's error (07fa450: 200, settled)."""
    data = _xlsx(tmp_path) if kind == "xlsx" else JSON_ARRAY
    body = {"content_base64": kit.b64(data), "filename": name, "options": options}
    r = await paid_app.paying.post("/v1/convert", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["report"]["rows"] > 0
    r = await paid_app.paying.post(
        "/v1/convert/upload",
        data={"options": json.dumps(options)},
        files={"file": (name, data, "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    is_error, structured, quoted = await _mcp(paid_app, body)
    assert quoted
    assert not is_error, structured
    assert len(facilitator.verified) == len(facilitator.settled) == 3


@pytest.mark.parametrize(
    ("path", "options"),
    [("/export.csv", {"encoding": "auto"}), ("/feed.csv", {"delimiter": '"'})],
    ids=str,
)
async def test_a_url_whose_file_is_another_format_converts_and_settles(
    path: str, options: dict[str, Any], www: str, make_settings: Callable[..., Settings]
) -> None:
    """Before: 400 before the quote, from the url's '.csv' (07fa450: 200, settled)."""
    fake = kit.FakeFacilitator()
    body = {"url": www + path, "options": options}
    async with kit.running_app(make_settings(allow_private_urls=True), facilitator=fake) as h:
        quote = await h.http.post("/v1/convert", json=body)
        assert quote.status_code == 402, quote.text
        r = await h.paying.post("/v1/convert", json=body)
        assert r.status_code == 200, r.text
        is_error, structured, quoted = await _mcp(h, body)
    assert quoted
    assert not is_error, structured
    assert len(fake.verified) == len(fake.settled) == 2


async def test_a_url_of_the_named_format_is_refused_by_its_reader_unpaid(
    www: str, make_settings: Callable[..., Settings]
) -> None:
    """The url's bytes are not known before the quote: its reader refuses the value, typed."""
    fake = kit.FakeFacilitator()
    body = {"url": www + "/a.csv", "options": {"encoding": "auto"}}
    async with kit.running_app(make_settings(allow_private_urls=True), facilitator=fake) as h:
        quote = await h.http.post("/v1/convert", json=body)
        assert quote.status_code == 402, quote.text
        r = await h.paying.post("/v1/convert", json=body)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"] == AUTO
    assert len(fake.verified) == 1
    assert fake.settled == []


async def test_a_stored_artifact_is_read_under_its_own_name(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    """Before: 400 before the quote from ``filename`` 'data.csv', which the engine never reads
    for an artifact (07fa450: 200, settled)."""
    first = await paid_app.paying.post(
        "/v1/convert", json={"content_base64": kit.b64(CSV), "filename": "a.csv"}
    )
    assert first.status_code == 200, first.text
    body = {
        "artifact_id": first.json()["artifact"]["id"],
        "filename": "data.csv",
        "options": {"encoding": "auto"},
    }
    r = await paid_app.paying.post("/v1/convert", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["report"]["rows"] == 2
    is_error, structured, quoted = await _mcp(paid_app, body)
    assert quoted
    assert not is_error, structured
    assert len(facilitator.verified) == len(facilitator.settled) == 3


def _long_csv() -> bytes:
    """A CSV past the start an upload is judged by (``FormPeek`` keeps its first KB)."""
    return b"id,amount\n" + b"".join(b"%d,%d.5\n" % (i, i) for i in range(400))


# (bytes, filename, options, message): bytes of the format whose reader cannot use the value.
REFUSED = [
    pytest.param(CSV, "data.csv", {"encoding": "auto"}, AUTO, id="csv"),
    pytest.param(_long_csv(), "data.csv", {"encoding": "auto"}, AUTO, id="csv-past-1kb"),
    pytest.param(CSV, None, {"delimiter": '"'}, DELIMITER, id="unnamed-csv"),
    pytest.param(JSON_ARRAY, "data.json", {"encoding": "auto"}, AUTO, id="json"),
]


@pytest.mark.parametrize(("data", "name", "options", "message"), REFUSED)
@pytest.mark.parametrize("paying", [False, True], ids=["unpaid", "paid"])
async def test_bytes_of_the_reading_format_are_refused_before_the_quote(
    data: bytes,
    name: str | None,
    options: dict[str, Any],
    message: str,
    paying: bool,
    paid_app: kit.AppHarness,
    facilitator: kit.FakeFacilitator,
) -> None:
    """The bytes decide as the name did when the two agree; with no name ('unnamed-csv') the
    request was quoted and verified before the reader's 400."""
    client = paid_app.paying if paying else paid_app.http
    body: dict[str, Any] = {"content_base64": kit.b64(data), "options": options}
    if name is not None:
        body["filename"] = name
    responses = [
        await client.post("/v1/convert", json=body),
        await client.post(
            "/v1/convert/upload",
            data={"options": json.dumps(options)},
            files={"file": (name or "upload", data, "application/octet-stream")},
        ),
    ]
    for r in responses:
        assert r.status_code == 400, r.text
        assert "payment-required" not in r.headers
        error = r.json()["error"]
        assert error["code"] == "invalid_request"
        assert error["message"] == message
        assert "Retry" not in error["hint"]
    is_error, structured, quoted = await _mcp(paid_app, body)
    assert is_error
    assert not quoted
    assert structured["error"]["message"] == message
    assert facilitator.verified == []
    assert facilitator.settled == []


async def test_an_upload_whose_start_does_not_decide_is_quoted(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator, tmp_path: Path
) -> None:
    """A large xlsx named data.csv: its start is a zip, which only its end says is a workbook."""
    data = _xlsx(tmp_path)
    assert len(data) > 1024
    r = await paid_app.paying.post(
        "/v1/convert/upload",
        data={"options": json.dumps({"encoding": "auto"})},
        files={"file": ("data.csv", data, "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    assert len(facilitator.settled) == 1


def test_format_from_start_is_the_engines_decision_or_none(tmp_path: Path) -> None:
    xlsx = _xlsx(tmp_path)
    assert format_from_start(CSV, "a.csv", None, whole=True) is InputFormat.CSV
    assert format_from_start(CSV, None, InputFormat.PDF, whole=False) is InputFormat.PDF
    assert format_from_start(JSON_ARRAY, "a.csv", None, whole=True) is InputFormat.JSON
    pdf_start = b"%PDF-1.4\n" + b"a,b\n" * 300
    assert format_from_start(pdf_start, "a.csv", None, whole=False) is InputFormat.PDF
    # A zip (an office document, or a member to extract), gzip or bz2: decided past the start.
    assert format_from_start(xlsx, "a.csv", None, whole=True) is None
    assert format_from_start(b"\x1f\x8b\x08\x00" + CSV, "a.csv.gz", None, whole=True) is None
    assert format_from_start(b"BZh91AY&SY", "a.csv.bz2", None, whole=True) is None
    # Only a start: a header after junk is a PDF when the trailer, at the end, says so.
    junk_pdf = b"x" * 10 + b"%PDF-1.4\n" + b"a,b\n" * 300
    assert format_from_start(junk_pdf, "a.csv", None, whole=False) is None
    assert format_from_start(_long_csv()[:1024], "a.csv", None, whole=False) is InputFormat.CSV
    assert format_from_start(CSV, "a.csv", None, whole=False) is None  # too short to judge
    # Unrecognised binary bytes with no name are an UnsupportedFormat for the engine to raise.
    assert format_from_start(b"\x00\x01\x02" * 400, None, None, whole=True) is None
