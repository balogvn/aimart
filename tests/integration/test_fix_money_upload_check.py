"""RV4: the upload route is validated before it is quoted or verified, like the JSON routes.

Before, every multipart (or urlencoded) request to /v1/convert/upload went straight to the
payment middleware: invalid ``options`` or a missing ``file`` part got a 402, and the paid
retry was verified by the facilitator before the route answered 400.

Kept on purpose: an unpaid form without a file is still quoted. Catalogs replay the advertised
example input, which is a form with ``options`` only, to learn the price.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit

if TYPE_CHECKING:
    import httpx

pytestmark = pytest.mark.anyio

UPLOAD = "/v1/convert/upload"
CSV = b"a,b\n1,2\n3,4\n"
BAD_OPTIONS = '{"bogus_option": 1}'


async def signature(h: kit.AppHarness) -> dict[str, str]:
    """A valid payment header for the upload route, as a paying agent would attach it."""
    import base64
    import json

    from x402 import parse_payment_required

    quote = await h.http.post(UPLOAD, files={"file": ("q.csv", CSV, "text/csv")})
    required = parse_payment_required(base64.b64decode(quote.headers["payment-required"]))
    payload = await kit.paying_client().create_payment_payload(required)
    header = base64.b64encode(
        json.dumps(payload.model_dump(by_alias=True, exclude_none=True)).encode()
    ).decode()
    return {"payment-signature": header}


def assert_refused_unquoted(r: httpx.Response, fake: kit.FakeFacilitator) -> dict[str, Any]:
    assert r.status_code == 400, r.text
    assert "payment-required" not in r.headers
    assert fake.verified == []
    assert fake.settled == []
    error: dict[str, Any] = r.json()["error"]
    assert error["code"] == "invalid_request"
    return error


@pytest.mark.parametrize("options", [BAD_OPTIONS, "{not json"], ids=["unknown-field", "not-json"])
async def test_invalid_options_are_refused_before_the_quote(
    options: str, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    r = await paid_app.http.post(
        UPLOAD, files={"file": ("a.csv", CSV, "text/csv")}, data={"options": options}
    )
    error = assert_refused_unquoted(r, facilitator)
    # The same words the route itself uses for this field.
    assert error["message"] == "options is not valid ConvertOptions JSON."
    assert error["hint"]


async def test_paid_upload_with_invalid_options_is_never_verified(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    headers = await signature(paid_app)
    r = await paid_app.http.post(
        UPLOAD,
        files={"file": ("a.csv", CSV, "text/csv")},
        data={"options": BAD_OPTIONS},
        headers=headers,
    )
    assert_refused_unquoted(r, facilitator)


async def test_paid_upload_without_a_file_is_never_verified(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    headers = await signature(paid_app)
    # multipart, but the only part is not the file
    r = await paid_app.http.post(
        UPLOAD, files={"attachment": ("a.csv", CSV, "text/csv")}, headers=headers
    )
    error = assert_refused_unquoted(r, facilitator)
    assert error["details"]["problems"] == [{"field": "file", "problem": "Field required"}]
    # urlencoded can never carry the file
    r = await paid_app.http.post(UPLOAD, data={"options": "{}"}, headers=headers)
    assert_refused_unquoted(r, facilitator)


async def test_invalid_options_before_a_large_file_are_refused_unread(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    # Options sent before the file (what httpx, requests and curl -F do in that order) are
    # read before any of the file is: a big upload is refused just as early.
    big = b"a,b\n" + b"1,2\n" * 800_000  # 3.2 MB, beyond what is peeked
    headers = await signature(paid_app)
    r = await paid_app.http.post(
        UPLOAD,
        data={"options": BAD_OPTIONS},
        files={"file": ("big.csv", big, "text/csv")},
        headers=headers,
    )
    assert_refused_unquoted(r, facilitator)


async def test_a_valid_upload_is_quoted_paid_and_reaches_the_route_intact(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    # The pre-check reads part of the body: the route must still get every byte of it.
    big = b"a,b\n" + b"".join(f"{i},{i % 7}\n".encode() for i in range(400_000))  # ~3.6 MB
    for name, body in (("small.csv", CSV), ("big.csv", big)):
        kwargs: dict[str, Any] = {
            "files": {"file": (name, body, "text/csv")},
            "data": {"options": '{"sort_by": ["a"]}'},
        }
        assert (await paid_app.http.post(UPLOAD, **kwargs)).status_code == 402
        r = await paid_app.paying.post(UPLOAD, **kwargs)
        assert r.status_code == 200, r.text
        assert r.json()["report"]["rows"] == body.count(b"\n") - 1
    # Streamed in 64 KB messages (a file object), the pre-check stops at its 1 MiB with the
    # file part unfinished: that is not a missing file, and the paid upload must go through.
    r = await paid_app.http.post(
        UPLOAD,
        files={"file": ("big.csv", io.BytesIO(big), "text/csv")},
        headers=await signature(paid_app),
    )
    assert r.status_code == 200, r.text
    assert r.json()["report"]["rows"] == big.count(b"\n") - 1
    assert len(facilitator.settled) == 3


async def test_a_form_without_a_file_is_still_a_price_probe(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    from aimart_parquet.payments.bazaar import EXAMPLES

    example = EXAMPLES["convert_upload"]["input"]
    for kwargs in (
        {"data": example},  # urlencoded, as a catalog replays it
        {"data": example, "files": {"note": ("n.txt", b"x", "text/plain")}},  # multipart
    ):
        r = await paid_app.http.post(UPLOAD, **kwargs)
        assert r.status_code == 402, r.text
        assert kit.payment_required(r)["accepts"][0]["amount"]
    assert facilitator.verified == []


async def test_a_malformed_multipart_body_is_refused_before_the_quote(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    for content_type in ("multipart/form-data", "multipart/form-data; boundary=xyz"):
        r = await paid_app.http.post(
            UPLOAD, content=b"--nope\r\nnot a part", headers={"content-type": content_type}
        )
        assert_refused_unquoted(r, facilitator)
