"""The pre-quote refusals the docs quote are the ones the server sends, word for word.

docs/agents-guide.md section 6 lists what is refused before a price is quoted, quoting each
message, so an agent can recognise a refusal it never signed for. Each quote here is sent to
the real app and must come back exactly, unpaid and with no PAYMENT-REQUIRED header; the
numbers the docs state (the SQL length parsed before the quote, the queue wait) are the code's.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit
from aimart_parquet.api.app import JSON_BODY_LIMIT
from aimart_parquet.config import Settings
from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.service import PRECHECK_SQL_CHARS
from aimart_parquet.sources import ALLOWED_PORTS

if TYPE_CHECKING:
    import httpx

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[2]
A = "Zz" * 12
B64 = kit.b64(b"a,b\n1,x\n")
URL = "https://e.example/a.parquet"


def section(rel: str, heading: str) -> str:
    """One ``## heading`` section with line wrapping collapsed, so quotes can span lines."""
    text = (ROOT / rel).read_text(encoding="utf-8")
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return " ".join(text[start : end if end != -1 else None].split())


GUIDE = section("docs/agents-guide.md", "## 6. Handle errors")
README = section("README.md", "## Limits and errors")


def _convert(**options: Any) -> tuple[str, dict[str, Any]]:
    return "/v1/convert", {"content_base64": B64, "options": options}


def _named(filename: str, **options: Any) -> tuple[str, dict[str, Any]]:
    return "/v1/convert", {"content_base64": B64, "filename": filename, "options": options}


# (path, body, status, code, the message the guide quotes)
MESSAGES: list[tuple[str, dict[str, Any], int, str, str]] = [
    (
        "/v1/inspect",
        {"filename": "x"},
        400,
        "invalid_request",
        "Provide exactly one input; got 0 (none).",
    ),
    (
        "/v1/inspect",
        {"url": URL, "artifact_id": A},
        400,
        "invalid_request",
        "Provide exactly one input; got 2 (url, artifact_id).",
    ),
    ("/v1/query", {"sql": "SELECT 1"}, 400, "invalid_request", "No tables to query."),
    (
        "/v1/query",
        {"artifact_id": A, "sql": "SELECT 1", "tables": {"data": {"artifact_id": A}}},
        400,
        "invalid_request",
        "Table name 'data' is used twice.",
    ),
    (
        "/v1/query",
        {"artifact_id": A, "sql": "SELECT 1", "tables": {"t": {}}},
        400,
        "invalid_request",
        "Table 't': provide exactly one input; got 0 (none).",
    ),
    (
        "/v1/convert",
        {"url": "ftp://e.example/a.csv"},
        422,
        "url_not_allowed",
        "Only http and https URLs are supported.",
    ),
    ("/v1/convert", {"url": "http://[::1/a.csv"}, 422, "url_not_allowed", "The URL is malformed."),
    ("/v1/convert", {"url": "https:///a.csv"}, 422, "url_not_allowed", "The URL has no host."),
    (
        "/v1/convert",
        {"url": "https://e.example:22/a.csv"},
        422,
        "url_not_allowed",
        "Port 22 is not allowed.",
    ),
    (
        "/v1/convert",
        {"url": "https://exämple.com/a.csv"},
        422,
        "url_not_allowed",
        "The URL's host is not ASCII.",
    ),
    (
        "/v1/convert",
        {"url": "https://data.ex ample.com/a.csv"},
        422,
        "url_not_allowed",
        "The URL's host contains a space or a control character.",
    ),
    (
        "/v1/convert",
        {"url": "http://127.0.0.1/a.csv"},
        422,
        "url_not_allowed",
        "Host '127.0.0.1' resolves to a non-public address.",
    ),
    (
        "/v1/query",
        {"artifact_id": A, "sql": "DROP TABLE data"},
        400,
        "query_rejected",
        "Only read-only SELECT queries are allowed; got DROP.",
    ),
    (
        "/v1/query",
        {"artifact_id": A, "sql": "SELECT 1; SELECT 2"},
        400,
        "query_rejected",
        "Send exactly one statement; got 2.",
    ),
    (
        "/v1/convert",
        {"content_base64": "ab*c"},  # "abc" was the example; unpadded, it is now 2 bytes
        400,
        "invalid_request",
        "content_base64 is not valid base64.",
    ),
    (
        "/v1/convert",
        {"content_base64": "data:text/csv,a%2Cb"},
        400,
        "invalid_request",
        "content_base64 is a data: URL that is not base64-encoded.",
    ),
    ("/v1/convert", {"content_base64": ""}, 400, "invalid_request", "content_base64 is empty."),
    (
        "/v1/preview",
        {"content_base64": B64, "filename": "x.csv"},
        422,
        "invalid_input",
        "Not a readable Parquet file: Parquet magic bytes not found in footer. Either the file "
        "is corrupted or this is not a parquet file.",
    ),
    (
        "/v1/query",
        {"content_base64": B64, "sql": "SELECT * FROM data"},
        422,
        "invalid_input",
        "Table 'data' is not a readable Parquet file.",
    ),
    (
        *_named("x.pdf", pdf_pages="5-2"),
        400,
        "invalid_request",
        "pdf_pages range '5-2' is invalid: pages start at 1, and a range runs low to high.",
    ),
    (*_named("x.csv", encoding="utf-9"), 400, "invalid_request", "Unknown encoding 'utf-9'."),
    (
        *_named("x.json", encoding="rot13"),
        400,
        "invalid_request",
        "'rot13' is not a text encoding.",
    ),
    (
        *_named("x.tsv", delimiter='"'),
        400,
        "invalid_request",
        "delimiter '\"' cannot separate fields: it is the quote character. Omit delimiter to "
        "detect it.",
    ),
]
# Bodies JSON cannot be read from, and the one problem the guide quotes for each.
UNREADABLE: list[tuple[bytes, str]] = [
    (b'{"artifact_id": ', "JSON decode error"),
    (b"[" * 100_000 + b"]" * 100_000, "JSON nested too deeply"),
    ('{"artifact_id": "caf\xe9"}'.encode("latin-1"), "JSON body is not valid UTF-8"),
    (b'{"limit": ' + b"1" * 5000 + b"}", "JSON integer longer than 4300 digits"),
]
# (path, body, the field-level problem the guide quotes, after pydantic's "Value error, ")
PROBLEMS: list[tuple[str, dict[str, Any], str]] = [
    (
        "/v1/preview",
        {"artifact_id": "not valid!"},
        "String should match pattern '^[A-Za-z0-9_-]{22,64}$'",
    ),
    # pdf_pages, encoding and delimiter were schema problems here, refused for any file; they
    # are now the reader's own error, for a file named as its format (in MESSAGES).
    (
        *_convert(
            expected_schema=[{"name": "a", "type": "int64"}, {"name": "a", "type": "string"}]
        ),
        "expected_schema names column 'a' twice.",
    ),
    (*_convert(null_values=["\x00"]), "must not contain a NUL character (\\x00)"),
]


def _refused(r: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    assert "payment-required" not in r.headers
    error: dict[str, Any] = r.json()["error"]
    assert error["code"] == code
    return error


@pytest.mark.parametrize(("path", "body", "status", "code", "message"), MESSAGES)
async def test_the_guide_quotes_each_refusal_the_server_sends(
    path: str,
    body: dict[str, Any],
    status: int,
    code: str,
    message: str,
    paid_app: kit.AppHarness,
) -> None:
    assert f'"{message}"' in GUIDE
    r = await paid_app.http.post(path, json=body)
    assert _refused(r, status, code)["message"] == message


@pytest.mark.parametrize(("path", "body", "problem"), PROBLEMS)
async def test_the_guide_quotes_each_schema_problem_the_server_sends(
    path: str, body: dict[str, Any], problem: str, paid_app: kit.AppHarness
) -> None:
    assert f'"{problem}"' in GUIDE
    r = await paid_app.http.post(
        path, content=json.dumps(body).encode(), headers={"content-type": "application/json"}
    )
    error = _refused(r, 400, "invalid_request")
    assert f'"{error["message"]}"' in GUIDE  # "The request body is invalid."
    [got] = error["details"]["problems"]
    assert got["problem"].removeprefix("Value error, ") == problem


@pytest.mark.parametrize(("raw", "problem"), UNREADABLE, ids=[p for _, p in UNREADABLE])
async def test_the_guide_quotes_each_unreadable_body_problem(
    raw: bytes, problem: str, paid_app: kit.AppHarness
) -> None:
    assert f'"{problem}"' in GUIDE
    r = await paid_app.http.post(
        "/v1/query", content=raw, headers={"content-type": "application/json"}
    )
    [got] = _refused(r, 400, "invalid_request")["details"]["problems"]
    assert got["problem"] == problem


async def test_the_guide_quotes_the_input_hints_each_surface_sends(
    paid_app: kit.AppHarness,
) -> None:
    # Each hint names only the inputs of the route or tool that sent it.
    r = await paid_app.http.post("/v1/inspect", json={"filename": "x"})
    hints = [_refused(r, 400, "invalid_request")["hint"]]
    async with kit.mcp_over_http(paid_app.app) as client:
        for tool, args in [
            ("convert_to_parquet", {"filename": "x.csv"}),
            ("inspect_parquet", {"url": URL, "artifact_id": A}),
        ]:
            result = await client.call_tool(tool, args)
            assert result.is_error
            hints.append((result.structured_content or {})["error"]["hint"])
    assert hints == [
        "Set one of: url, content_base64, artifact_id.",
        "Set one of: url, content_base64, artifact_id.",
        "Set one of: url, artifact_id.",
    ]
    for hint in hints:
        assert f'"{hint}"' in GUIDE


async def test_the_guide_quotes_the_upload_form_refusal(paid_app: kit.AppHarness) -> None:
    r = await paid_app.http.post(
        "/v1/convert/upload",
        files={"file": ("a.csv", b"a\n1\n", "text/csv")},
        # {"encoding": "rot13"} was the example; the schema no longer refuses it for any file.
        data={"options": json.dumps({"row_group_size": 1})},
    )
    message = _refused(r, 400, "invalid_request")["message"]
    assert f'"{message}"' in GUIDE


def test_the_docs_state_the_limits_the_code_applies() -> None:
    for text in (GUIDE, README):
        assert f"SQL of up to {PRECHECK_SQL_CHARS} characters" in text
    # This compared both texts with a fixed queue_wait_s (10 s). The wait is now what the
    # payment window leaves each operation (Settings.queue_wait_for), so each is compared.
    s = Settings()
    waits = (
        f"{s.queue_wait_for(s.convert_timeout_s):g} s to convert, "
        f"{s.queue_wait_for(s.query_timeout_s):g} s to inspect or preview, "
        f"{s.queue_wait_for(s.query_timeout_s + 5):g} s to query"
    )
    assert f"as long as a call waits for one (what its payment window leaves it: {waits})" in GUIDE
    assert (
        f"a call waits for a free one as long as its payment window leaves it ({waits})" in README
    )
    assert f"{sys.get_int_max_str_digits()} digits" in GUIDE
    assert f"the request body may be {JSON_BODY_LIMIT // 2**20} MB" in GUIDE
    longest = ConvertOptions.model_json_schema()["properties"]["encoding"]["anyOf"][0]["maxLength"]
    assert f"longer than {longest} characters" in GUIDE
    fetch = f"{Settings().fetch_timeout_s:g} s of downloading for all of a call's `url` inputs"
    assert fetch in README
    ports = sorted(ALLOWED_PORTS)
    listed = ", ".join(map(str, ports[:-1])) + f" and {ports[-1]}"
    assert f"(ports {listed} are)" in GUIDE
