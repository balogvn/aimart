"""Requests that can only fail, decided from the request alone, are refused before the quote.

Before: only pydantic schema errors were refused before the 402. A body with no input or two,
a malformed artifact_id, an ftp:// URL, ``DROP TABLE data``, ``pdf_pages`` '5-2', an encoding
like 'rot13', a delimiter of '"', or an expected_schema naming one column twice was quoted, and
the paid retry was verified by the facilitator, before a deterministic 4xx. Some of those then
crashed into a bare 500 (``http://[::1/a.csv``, 'rot13', '"', a case-only duplicate column).
Every case below is refused with the same typed error before any 402 or verify, on REST, the
upload form and MCP. An unpaid ``{}`` (and an MCP call with no arguments) stays a price probe.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit

if TYPE_CHECKING:
    import httpx

pytestmark = pytest.mark.anyio

A = "Zz" * 12  # well-formed, never issued
CSV = b"a,b\n1,x\n2,y\n"
B64 = kit.b64(CSV)
URL = "https://e.example/a.parquet"
NEEDS_TWO = "Provide exactly one input; got 2 (url, artifact_id)."

# (path, body, status, code, message or None when the schema words it)
REST: list[tuple[str, dict[str, Any], int, str, str | None]] = [
    (
        "/v1/inspect",
        {"filename": "x.parquet"},
        400,
        "invalid_request",
        "Provide exactly one input; got 0 (none).",
    ),
    ("/v1/inspect", {"url": URL, "artifact_id": A}, 400, "invalid_request", NEEDS_TWO),
    ("/v1/preview", {"artifact_id": A, "url": URL}, 400, "invalid_request", NEEDS_TWO),
    (
        "/v1/convert",
        {"content_base64": B64, "url": URL},
        400,
        "invalid_request",
        "Provide exactly one input; got 2 (url, content_base64).",
    ),
    ("/v1/preview", {"artifact_id": "not valid!"}, 400, "invalid_request", None),
    ("/v1/inspect", {"artifact_id": "short"}, 400, "invalid_request", None),
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
        "/v1/query",
        {"artifact_id": A, "sql": "SELECT 1", "tables": {"t": {"artifact_id": A, "url": URL}}},
        400,
        "invalid_request",
        "Table 't': provide exactly one input; got 2 (url, artifact_id).",
    ),
    (
        "/v1/query",
        {"artifact_id": A, "sql": "SELECT 1", "tables": {"t": {"artifact_id": "bad id"}}},
        400,
        "invalid_request",
        None,
    ),
    ("/v1/query", {"sql": "SELECT 1"}, 400, "invalid_request", "No tables to query."),
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
        "/v1/query",
        {"artifact_id": A, "sql": "COPY data TO '/tmp/x.csv'"},
        400,
        "query_rejected",
        "Only read-only SELECT queries are allowed; got COPY.",
    ),
    ("/v1/query", {"artifact_id": A, "sql": "  ;  "}, 400, "query_rejected", "The SQL is empty."),
    # DuckDB stops reading at a NUL: this ran unfiltered and was charged.
    (
        "/v1/query",
        {"artifact_id": A, "sql": "SELECT * FROM data\x00 WHERE a > 1"},
        400,
        "invalid_request",
        None,
    ),
    (
        "/v1/query",
        {"artifact_id": A, "sql": "SELECT 1", "tables": {"t": {"url": "ftp://e.example/t"}}},
        422,
        "url_not_allowed",
        "Only http and https URLs are supported.",
    ),
    (
        "/v1/convert",
        {"url": "ftp://e.example/a.csv"},
        422,
        "url_not_allowed",
        "Only http and https URLs are supported.",
    ),
    ("/v1/convert", {"url": "http://[::1/a.csv"}, 422, "url_not_allowed", "The URL is malformed."),
    (
        "/v1/inspect",
        {"url": "http://[zz]/a.parquet"},
        422,
        "url_not_allowed",
        "The URL is malformed.",
    ),
    (
        "/v1/convert",
        {"url": "https://e.example:22/a.csv"},
        422,
        "url_not_allowed",
        "Port 22 is not allowed.",
    ),
    (
        "/v1/convert",
        {"url": "http://127.0.0.1/a.csv"},
        422,
        "url_not_allowed",
        "Host '127.0.0.1' resolves to a non-public address.",
    ),
    (
        "/v1/inspect",
        {"url": "http://169.254.169.254/latest"},
        422,
        "url_not_allowed",
        "Host '169.254.169.254' resolves to a non-public address.",
    ),
    (
        "/v1/convert",
        {"url": "https://exämple.com/a.csv"},
        422,
        "url_not_allowed",
        "The URL's host is not ASCII.",
    ),
]
OPTIONS: list[dict[str, Any]] = [
    {"pdf_pages": "0"},
    {"pdf_pages": "5-2"},
    {"pdf_pages": "1,,2"},
    {"encoding": "no-such-codec"},
    {"encoding": "rot13"},
    {"encoding": "base64"},
    {"encoding": "idna"},
    {"delimiter": '"'},
    {"delimiter": "\n"},
    {"delimiter": "\r"},
    {"delimiter": "\x00"},
    {"expected_schema": [{"name": "a", "type": "int64"}, {"name": "a", "type": "string"}]},
    {"expected_schema": [{"name": "a", "type": "int64"}, {"name": "A", "type": "string"}]},
    {"expected_schema": [{"name": "z", "type": "int64"}, {"name": "z", "type": "string"}]},
    {"expected_schema": [{"name": "", "type": "int64"}]},
    {"expected_schema": [{"name": "a\x00", "type": "int64"}]},
    {"null_values": ["\x00"]},
]
REST += [
    (
        "/v1/convert",
        {"content_base64": B64, "filename": "x.csv", "options": o},
        400,
        "invalid_request",
        None,
    )
    for o in OPTIONS
]


def _id(value: Any) -> str | None:
    return json.dumps(value)[:60] if isinstance(value, dict) else None


async def _signature(h: kit.AppHarness, path: str, good: dict[str, Any]) -> dict[str, str]:
    """A valid payment header for ``path``, signed from the quote for a valid body."""
    from x402 import parse_payment_required

    quote = await h.http.post(path, json=good)
    required = parse_payment_required(base64.b64decode(quote.headers["payment-required"]))
    payload = await kit.paying_client().create_payment_payload(required)
    header = base64.b64encode(
        json.dumps(payload.model_dump(by_alias=True, exclude_none=True)).encode()
    ).decode()
    return {"payment-signature": header}


GOOD: dict[str, dict[str, Any]] = {
    "/v1/inspect": {"artifact_id": A},
    "/v1/preview": {"artifact_id": A},
    "/v1/query": {"artifact_id": A, "sql": "SELECT 1"},
    "/v1/convert": {"content_base64": B64, "filename": "x.csv"},
    "/v1/quantum/compress": {"artifact_id": A},
    "/v1/quantum/portfolio": {"artifact_id": A},
}


def _assert_refused(
    r: httpx.Response, status: int, code: str, message: str | None, fake: kit.FakeFacilitator
) -> None:
    assert r.status_code == status, r.text
    assert "payment-required" not in r.headers
    error = r.json()["error"]
    assert error["code"] == code
    assert error["hint"]
    if message is not None:
        assert error["message"] == message
    else:
        assert error["details"]["problems"], error
    assert fake.verified == []
    assert fake.settled == []


@pytest.mark.parametrize(("path", "body", "status", "code", "message"), REST, ids=_id)
async def test_rest_refuses_before_the_quote(
    path: str,
    body: dict[str, Any],
    status: int,
    code: str,
    message: str | None,
    paid_app: kit.AppHarness,
    facilitator: kit.FakeFacilitator,
) -> None:
    r = await paid_app.http.post(path, json=body)
    _assert_refused(r, status, code, message, facilitator)
    assert facilitator.supported_calls == 0  # not even initialized for it


@pytest.mark.parametrize(("path", "body", "status", "code", "message"), REST, ids=_id)
async def test_rest_paid_retry_is_never_verified(
    path: str,
    body: dict[str, Any],
    status: int,
    code: str,
    message: str | None,
    paid_app: kit.AppHarness,
    facilitator: kit.FakeFacilitator,
) -> None:
    headers = await _signature(paid_app, path, GOOD[path])
    r = await paid_app.http.post(path, json=body, headers=headers)
    _assert_refused(r, status, code, message, facilitator)


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/quantum/compress", {"artifact_id": A, "url": URL}),
        ("/v1/quantum/portfolio", {"url": "ftp://e.example/p.parquet"}),
        ("/v1/quantum/portfolio", {"artifact_id": "not valid!"}),
    ],
    ids=_id,
)
async def test_quantum_routes_refuse_before_the_quote(
    path: str, body: dict[str, Any], paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    r = await paid_app.http.post(path, json=body)
    assert 400 <= r.status_code < 500, r.text
    assert "payment-required" not in r.headers
    assert facilitator.verified == []


async def test_a_paid_empty_body_is_refused_and_an_unpaid_one_is_still_quoted(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    for path in GOOD:
        r = await paid_app.http.post(path, json={})
        assert r.status_code == 402, (path, r.text)  # the price probe catalogs send
    headers = await _signature(paid_app, "/v1/inspect", GOOD["/v1/inspect"])
    r = await paid_app.http.post("/v1/inspect", json={}, headers=headers)
    _assert_refused(
        r, 400, "invalid_request", "Provide exactly one input; got 0 (none).", facilitator
    )


async def test_long_sql_is_still_checked_after_payment(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    # DuckDB's parser holds the GIL and is cubic on some inputs (1,600 '+' took 1.3 s, 20,000
    # would take most of an hour), so only short SQL is parsed before the quote. Longer SQL is
    # validated where it runs, in the job's child process: quoted, verified, refused, unpaid.
    from aimart_parquet.engine.models import ConvertOptions
    from aimart_parquet.service import SourceRef

    converted = await paid_app.app.state.service.convert(
        SourceRef(content_base64=B64, filename="x.csv"), ConvertOptions()
    )
    body = {"artifact_id": converted.artifact.id, "sql": "DROP TABLE data -- " + "x" * 300}
    quote = await paid_app.http.post("/v1/query", json=body)
    assert quote.status_code == 402
    r = await paid_app.paying.post("/v1/query", json=body)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "query_rejected"
    assert len(facilitator.verified) == 1
    assert facilitator.settled == []


async def test_valid_requests_are_still_quoted(paid_app: kit.AppHarness) -> None:
    sql = "SELECT a, count(*) FROM data JOIN t USING (a) GROUP BY 1 -- " + "x" * 400
    bodies = [
        ("/v1/convert", {"url": "https://example.com/données.csv"}),
        (
            "/v1/convert",
            {"url": "https://example.com:8443/a.csv", "options": {"pdf_pages": "1-3,7"}},
        ),
        (
            "/v1/convert",
            {"content_base64": B64, "options": {"encoding": "latin-1", "delimiter": ";"}},
        ),
        (
            "/v1/convert",
            {"url": URL, "options": {"expected_schema": [{"name": "a", "type": "int64"}]}},
        ),
        ("/v1/query", {"sql": "SELECT 1", "tables": {"t": {"artifact_id": A}}}),  # no `data`
        ("/v1/query", {"artifact_id": A, "sql": sql, "tables": {"t": {"url": URL}}}),
        ("/v1/query", {"artifact_id": A, "sql": "WITH x AS (SELECT 1) SELECT * FROM x;"}),
        ("/v1/query", {"artifact_id": A, "sql": "DESCRIBE data"}),
    ]
    for path, body in bodies:
        r = await paid_app.http.post(path, json=body)
        assert r.status_code == 402, (path, body, r.text)


# ------------------------------------------------------------------------------ lone surrogates
LONE = "\ud800"  # a valid JSON escape ("\\ud800"), with no UTF-8 form
SURROGATES: list[tuple[str, dict[str, Any], str]] = [
    # Before: paid, a 500 internal_error (UnicodeEncodeError where the token met DuckDB).
    ("/v1/convert", {"content_base64": B64, "options": {"null_values": [LONE]}}, "null_values"),
    ("/v1/convert", {"content_base64": B64, "options": {"sort_by": [LONE]}}, "sort_by"),
    (
        "/v1/convert",
        {"content_base64": B64, "options": {"expected_schema": [{"name": LONE, "type": "int64"}]}},
        "expected_schema",
    ),
    ("/v1/query", {"artifact_id": A, "sql": f"SELECT '{LONE}'"}, "sql"),
    ("/v1/query", {"artifact_id": A, "sql": f"SELECT 1 -- {LONE}" + "x" * 300}, "sql"),
]


@pytest.mark.parametrize(("path", "body", "field"), SURROGATES, ids=lambda v: _id(v) or v)
@pytest.mark.parametrize("paying", [False, True], ids=["unpaid", "paid"])
async def test_lone_surrogates_are_refused_before_the_quote(
    path: str,
    body: dict[str, Any],
    field: str,
    paying: bool,
    paid_app: kit.AppHarness,
    facilitator: kit.FakeFacilitator,
) -> None:
    # Only REST can receive one: json.loads reads the escape, while the MCP transport's JSON
    # parser refuses it as invalid JSON. DuckDB can never be given one.
    headers = {"content-type": "application/json"}
    if paying:
        headers |= await _signature(paid_app, path, GOOD[path])
    r = await paid_app.http.post(path, content=json.dumps(body).encode(), headers=headers)
    _assert_refused(r, 400, "invalid_request", None, facilitator)
    [problem] = r.json()["error"]["details"]["problems"]
    assert field in problem["field"], problem


# ------------------------------------------------------------------------------ deep JSON
@pytest.mark.parametrize("path", ["/v1/query", "/v1/convert"])
@pytest.mark.parametrize("paying", [False, True], ids=["unpaid", "paid"])
async def test_deeply_nested_json_is_a_400_not_a_bare_500(
    path: str, paying: bool, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    # Before: json.loads raised RecursionError out of the pre-quote check, a bare 500.
    headers = {"content-type": "application/json"}
    if paying:
        headers |= await _signature(paid_app, path, GOOD[path])
    deep = b"[" * 200_000 + b"]" * 200_000
    r = await paid_app.http.post(path, content=deep, headers=headers)
    assert r.status_code == 400, r.text
    assert "payment-required" not in r.headers
    error = r.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["details"]["problems"] == [{"field": "", "problem": "JSON nested too deeply"}]
    assert facilitator.verified == []


# ------------------------------------------------------------------------------ upload form
@pytest.mark.parametrize("options", OPTIONS, ids=_id)
async def test_upload_form_options_are_refused_before_the_quote(
    options: dict[str, Any], paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    r = await paid_app.http.post(
        "/v1/convert/upload",
        files={"file": ("a.csv", CSV, "text/csv")},
        data={"options": json.dumps(options)},
    )
    assert r.status_code == 400, r.text
    assert "payment-required" not in r.headers
    assert r.json()["error"]["message"] == "options is not valid ConvertOptions JSON."
    assert facilitator.verified == []


# ------------------------------------------------------------------------------ MCP
MCP: list[tuple[str, dict[str, Any], str | None, str | None]] = [
    # (tool, arguments, error code (None: the SDK's validation text), message)
    (
        "inspect_parquet",
        {"url": None},
        "invalid_request",
        "Provide exactly one input; got 0 (none).",
    ),
    ("inspect_parquet", {"url": URL, "artifact_id": A}, "invalid_request", NEEDS_TWO),
    ("preview_parquet", {"artifact_id": "not valid!"}, None, None),
    (
        "query_parquet",
        {"artifact_id": A, "sql": "SELECT 1", "tables": {"data": A}},
        "invalid_request",
        "Table name 'data' is used twice.",
    ),
    (
        "query_parquet",
        {"artifact_id": A, "sql": "SELECT 1", "tables": {"t": "bad id!"}},
        None,
        None,
    ),
    ("query_parquet", {"sql": "SELECT 1"}, "invalid_request", "No tables to query."),
    (
        "query_parquet",
        {"artifact_id": A, "sql": "DROP TABLE data"},
        "query_rejected",
        "Only read-only SELECT queries are allowed; got DROP.",
    ),
    ("query_parquet", {"artifact_id": A, "sql": "SELECT 1\x00; DROP TABLE data"}, None, None),
    (
        "convert_to_parquet",
        {"url": "ftp://e.example/a.csv"},
        "url_not_allowed",
        "Only http and https URLs are supported.",
    ),
    (
        "convert_to_parquet",
        {"url": "http://[::1/a.csv"},
        "url_not_allowed",
        "The URL is malformed.",
    ),
    (
        "convert_to_parquet",
        {"content_base64": B64, "url": URL},
        "invalid_request",
        "Provide exactly one input; got 2 (url, content_base64).",
    ),
    ("quantum_compress_features", {"artifact_id": A, "url": URL}, "invalid_request", NEEDS_TWO),
    (
        "quantum_optimize_portfolio",
        {"url": "http://10.0.0.1/p.parquet"},
        "url_not_allowed",
        "Host '10.0.0.1' resolves to a non-public address.",
    ),
]
MCP += [("convert_to_parquet", {"content_base64": B64, "options": o}, None, None) for o in OPTIONS]
MCP_GOOD: dict[str, dict[str, Any]] = {
    "inspect_parquet": {"artifact_id": A},
    "preview_parquet": {"artifact_id": A},
    "query_parquet": {"artifact_id": A, "sql": "SELECT 1"},
    "convert_to_parquet": {"content_base64": B64, "filename": "x.csv"},
    "quantum_compress_features": {"artifact_id": A},
    "quantum_optimize_portfolio": {"artifact_id": A},
}


def _assert_tool_refused(result: Any, code: str | None, message: str | None) -> None:
    assert result.is_error
    structured = result.structured_content or {}
    assert "accepts" not in structured  # not a PaymentRequired
    if code is None:
        assert "validation error" in result.content[0].text
        return
    error = structured["error"]
    assert error["code"] == code
    assert error["hint"]
    assert error["message"] == message
    assert json.loads(result.content[0].text) == structured


@pytest.mark.parametrize(("tool", "args", "code", "message"), MCP, ids=_id)
async def test_mcp_refuses_before_the_quote(
    tool: str,
    args: dict[str, Any],
    code: str | None,
    message: str | None,
    paid_app: kit.AppHarness,
    facilitator: kit.FakeFacilitator,
) -> None:
    async with kit.mcp_over_http(paid_app.app) as client:
        result = await client.call_tool(tool, args)
    _assert_tool_refused(result, code, message)
    assert facilitator.supported_calls == 0
    assert facilitator.verified == []


@pytest.mark.parametrize(("tool", "args", "code", "message"), MCP, ids=_id)
async def test_mcp_paid_call_is_never_verified(
    tool: str,
    args: dict[str, Any],
    code: str | None,
    message: str | None,
    paid_app: kit.AppHarness,
    facilitator: kit.FakeFacilitator,
) -> None:
    async with kit.mcp_over_http(paid_app.app) as client:
        quote = await client.call_tool(tool, MCP_GOOD[tool])
        meta = await kit.mcp_payment_meta(kit.paying_client(), quote.structured_content)
        result = await client.call_tool(tool, args, meta=meta)
    _assert_tool_refused(result, code, message)
    assert facilitator.verified == []
    assert facilitator.settled == []


async def test_mcp_paid_call_without_arguments_is_refused_and_unpaid_is_quoted(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    async with kit.mcp_over_http(paid_app.app) as client:
        quote = await client.call_tool("inspect_parquet", {})
        assert quote.is_error
        assert "accepts" in quote.structured_content  # the probe still gets its price
        meta = await kit.mcp_payment_meta(kit.paying_client(), quote.structured_content)
        result = await client.call_tool("inspect_parquet", {}, meta=meta)
    _assert_tool_refused(result, "invalid_request", "Provide exactly one input; got 0 (none).")
    assert facilitator.verified == []
