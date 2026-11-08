"""More requests that can only fail, refused before the quote, and cheaply.

Found by the second review of this branch, each decided from the request alone:

* ``content_base64`` that base64 cannot decode ("a") or that decodes to nothing ("") was
  quoted and, paid, verified before it was refused (400 "not valid base64", 422 "no rows").
* A JSON body ``json.loads`` refuses with a plain ValueError (bytes that are not UTF-8, an
  integer over Python's 4,300-digit limit) was handed on to the quote, and the paid retry got
  FastAPI's untyped ``{"detail": "There was an error parsing the body"}`` after the verify.
* The pre-quote encoding check looked every name up with ``codecs.lookup``, which caches each
  unknown name for good: unpaid requests with megabyte-long names grew the API process's memory
  by their size (40 MiB for 20 requests), and the 400 echoed the whole name back.
"""

from __future__ import annotations

import base64
import codecs
import encodings
import json
import pkgutil
import secrets
from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit
from aimart_parquet.engine.text import encoding_problem

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

A = "Zz" * 12  # well-formed, never issued
CSV = b"a,b\n1,x\n2,y\n"
GOOD: dict[str, dict[str, Any]] = {
    "/v1/convert": {"content_base64": kit.b64(CSV), "filename": "x.csv"},
    "/v1/inspect": {"artifact_id": A},
    "/v1/preview": {"artifact_id": A},
    "/v1/query": {"artifact_id": A, "sql": "SELECT 1"},
}


async def _signature(h: kit.AppHarness, path: str) -> dict[str, str]:
    """A valid payment header for ``path``, signed from the quote for a valid body."""
    from x402 import parse_payment_required

    quote = await h.http.post(path, json=GOOD[path])
    assert quote.status_code == 402, quote.text
    required = parse_payment_required(base64.b64decode(quote.headers["payment-required"]))
    payload = await kit.paying_client().create_payment_payload(required)
    header = base64.b64encode(
        json.dumps(payload.model_dump(by_alias=True, exclude_none=True)).encode()
    ).decode()
    return {"payment-signature": header}


def _refused(r: httpx.Response, status: int, code: str, fake: kit.FakeFacilitator) -> Any:
    assert r.status_code == status, r.text
    assert "payment-required" not in r.headers
    error = r.json()["error"]
    assert error["code"] == code
    assert error["hint"]
    assert fake.verified == []
    assert fake.settled == []
    return error


# ------------------------------------------------------------------------------ content_base64
# Each: the content_base64, then the code and message it is refused with.
BASE64: list[tuple[str, str, str]] = [
    # "abc" was here as incorrect padding; unpadded, it is now read as the 2 bytes it encodes.
    ("YWJjZ", "invalid_request", "content_base64 is not valid base64."),  # no padding completes it
    ("a", "invalid_request", "content_base64 is not valid base64."),
    ("café", "invalid_request", "content_base64 is not valid base64."),  # not ASCII
    ("", "invalid_request", "content_base64 is empty."),
    # No base64 characters at all: "empty" while unknown characters were dropped, now refused.
    ("!!!!", "invalid_request", "content_base64 is not valid base64."),
    ("data:text/csv;base64,", "invalid_request", "content_base64 is empty."),
]


@pytest.mark.parametrize(("value", "code", "message"), BASE64)
@pytest.mark.parametrize("path", ["/v1/convert", "/v1/inspect", "/v1/preview", "/v1/query"])
@pytest.mark.parametrize("paying", [False, True], ids=["unpaid", "paid"])
async def test_rest_refuses_base64_that_can_only_fail_before_the_quote(
    value: str,
    code: str,
    message: str,
    path: str,
    paying: bool,
    paid_app: kit.AppHarness,
    facilitator: kit.FakeFacilitator,
) -> None:
    headers = await _signature(paid_app, path) if paying else {}
    body: dict[str, Any] = {"content_base64": value, "filename": "x.csv"}
    if path == "/v1/query":
        body["sql"] = "SELECT 1"
    r = await paid_app.http.post(path, json=body, headers=headers)
    error = _refused(r, 400, code, facilitator)
    assert error["message"] == message


async def test_base64_over_the_input_limit_is_refused_before_the_quote(
    make_settings: Callable[..., Settings],
) -> None:
    fake = kit.FakeFacilitator()
    settings = make_settings(max_input_bytes=1024 * 1024)
    body = {"content_base64": kit.b64(b"x" * (1024 * 1024 + 1)), "filename": "x.csv"}
    async with kit.running_app(settings, facilitator=fake) as h:
        r = await h.http.post("/v1/convert", json=body)
    error = _refused(r, 413, "input_too_large", fake)
    assert error["message"] == "The file is larger than the 1 MB limit."


async def test_valid_base64_is_still_quoted(paid_app: kit.AppHarness) -> None:
    for value in (
        kit.b64(CSV),
        "data:text/csv;base64," + kit.b64(CSV),
        "YSxi\nCjEs\nMgo=",
        base64.urlsafe_b64encode(CSV).decode().rstrip("="),
    ):
        r = await paid_app.http.post("/v1/convert", json={"content_base64": value})
        assert r.status_code == 402, (value, r.text)


@pytest.mark.parametrize(("value", "code", "message"), BASE64)
@pytest.mark.parametrize("paying", [False, True], ids=["unpaid", "paid"])
async def test_mcp_refuses_base64_that_can_only_fail_before_the_quote(
    value: str,
    code: str,
    message: str,
    paying: bool,
    paid_app: kit.AppHarness,
    facilitator: kit.FakeFacilitator,
) -> None:
    args = {"content_base64": value, "filename": "x.csv"}
    async with kit.mcp_over_http(paid_app.app) as client:
        meta = None
        if paying:
            quote = await client.call_tool("convert_to_parquet", GOOD["/v1/convert"])
            meta = await kit.mcp_payment_meta(kit.paying_client(), quote.structured_content)
        result = await client.call_tool("convert_to_parquet", args, meta=meta)
    assert result.is_error
    structured = result.structured_content or {}
    assert "accepts" not in structured  # not a PaymentRequired
    assert structured["error"]["code"] == code
    assert structured["error"]["message"] == message
    assert facilitator.verified == []
    assert facilitator.settled == []


# ------------------------------------------------------------------------------ JSON ValueError
LATIN1 = f'{{"artifact_id": "{A}", "sql": "SELECT * FROM data WHERE c = \'caf\xe9\'"}}'
UNREADABLE: list[tuple[str, bytes, str]] = [
    ("not-utf8", LATIN1.encode("latin-1"), "JSON body is not valid UTF-8"),
    (
        "long-int",
        f'{{"artifact_id": "{A}", "sql": "SELECT 1", "limit": {"1" * 5000}}}'.encode(),
        "JSON integer longer than 4300 digits",
    ),
]


@pytest.mark.parametrize(("label", "raw", "problem"), UNREADABLE, ids=[u[0] for u in UNREADABLE])
@pytest.mark.parametrize("paying", [False, True], ids=["unpaid", "paid"])
async def test_json_the_parser_refuses_is_a_typed_400_before_the_quote(
    label: str,
    raw: bytes,
    problem: str,
    paying: bool,
    paid_app: kit.AppHarness,
    facilitator: kit.FakeFacilitator,
) -> None:
    headers = {"content-type": "application/json"}
    if paying:
        headers |= await _signature(paid_app, "/v1/query")
    r = await paid_app.http.post("/v1/query", content=raw, headers=headers)
    error = _refused(r, 400, "invalid_request", facilitator)
    assert error["message"] == "The request body is invalid."
    assert error["details"]["problems"] == [{"field": "", "problem": problem}], label


# ------------------------------------------------------------------------------ encoding names
def _cached_name_bytes() -> int:
    return sum(len(k) for k in encodings._cache if isinstance(k, str))


async def test_long_encoding_names_are_refused_without_a_codec_lookup(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator, monkeypatch: pytest.MonkeyPatch
) -> None:
    looked_up: list[int] = []
    real = codecs.lookup

    def spy(name: str) -> codecs.CodecInfo:
        looked_up.append(len(name))
        return real(name)

    monkeypatch.setattr(codecs, "lookup", spy)
    before = _cached_name_bytes()
    for _ in range(4):
        name = "x" + secrets.token_hex(512 * 1024)  # 1 MiB, a fresh cache miss every time
        body = {"content_base64": kit.b64(CSV), "options": {"encoding": name}}
        r = await paid_app.http.post("/v1/convert", json=body)
        error = _refused(r, 400, "invalid_request", facilitator)
        [p] = error["details"]["problems"]
        assert p == {
            "field": "options.encoding",
            "problem": "String should have at most 64 characters",
        }
        assert len(r.content) < 1000  # the name is not echoed back
    assert not any(n > 64 for n in looked_up)
    assert _cached_name_bytes() - before < 1024


async def test_long_encoding_names_in_the_upload_form_are_not_echoed(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    name = "x" + secrets.token_hex(100_000)
    r = await paid_app.http.post(
        "/v1/convert/upload",
        files={"file": ("a.csv", CSV, "text/csv")},
        data={"options": json.dumps({"encoding": name})},
    )
    error = _refused(r, 400, "invalid_request", facilitator)
    assert error["message"] == "options is not valid ConvertOptions JSON."
    assert "at most 64 characters" in error["hint"]
    assert len(r.content) < 1000


async def test_long_encoding_names_over_mcp_are_refused_unquoted(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    args = {"content_base64": kit.b64(CSV), "options": {"encoding": "x" * 100_000}}
    async with kit.mcp_over_http(paid_app.app) as client:
        result = await client.call_tool("convert_to_parquet", args)
    assert result.is_error
    assert "accepts" not in (result.structured_content or {})
    assert "at most 64 characters" in result.content[0].text
    assert facilitator.verified == []


def _stdlib_codec_names() -> list[str]:
    """Every name the running interpreter's ``encodings`` package has for a codec a text reader
    can use (its aliases, their targets and its codec modules), longest first."""
    names = set(encodings.aliases.aliases) | set(encodings.aliases.aliases.values())
    names |= {module.name for module in pkgutil.iter_modules(encodings.__path__)}
    return sorted((n for n in names if encoding_problem(n) is None), key=lambda n: (-len(n), n))


async def test_every_stdlib_codec_name_still_fits(paid_app: kit.AppHarness) -> None:
    # This test sent 'pc_multilingual_850_euro', 3.13's longest name (24 characters), and failed
    # on 3.12, which has no such alias: the names are the interpreter's, so they are read from
    # the one running. A 402 for the longest ones shows the 64-character cap and the codec check
    # both let them through, and every shorter name fits the cap too.
    names = _stdlib_codec_names()
    assert len(names) > 100, names  # read from the package, not an empty list that proves nothing
    for name in (*names[:3], "ISO-8859-15", "utf_16_le", "cp1252", "latin-1"):
        body = {"content_base64": kit.b64(CSV), "options": {"encoding": name}}
        r = await paid_app.http.post("/v1/convert", json=body)
        assert r.status_code == 402, (name, r.text)
