"""An upload whose file part is empty is refused before the quote, as an empty content_base64 is.

Incident (final-validate review, round 3): ``content_base64 is empty.`` became a 400 before the
quote, but an upload form whose file part was complete and held no bytes was still quoted and,
paid, verified before the 422 "The file contains no rows.". FormPeek knew the part had ended;
it did not record that nothing was in it.
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import pytest

import aimart_testkit as kit

pytestmark = pytest.mark.anyio

UPLOAD = "/v1/convert/upload"
EMPTY = {
    "code": "invalid_request",
    "message": "The uploaded file is empty.",
    "hint": "Upload the bytes of a non-empty file; you were not charged.",
}


async def _signature(h: kit.AppHarness) -> dict[str, str]:
    from x402 import parse_payment_required

    quote = await h.http.post(UPLOAD, files={"file": ("q.csv", b"a\n1\n")})
    required = parse_payment_required(base64.b64decode(quote.headers["payment-required"]))
    payload = await kit.paying_client().create_payment_payload(required)
    header = base64.b64encode(
        json.dumps(payload.model_dump(by_alias=True, exclude_none=True)).encode()
    ).decode()
    return {"payment-signature": header}


@pytest.mark.parametrize("how", ["unpaid", "paid", "paid-streamed"])
async def test_an_empty_file_part_is_refused_before_the_quote(
    how: str, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    headers = await _signature(paid_app) if how != "unpaid" else {}
    file: Any = io.BytesIO(b"") if how == "paid-streamed" else b""
    r = await paid_app.http.post(
        UPLOAD,
        files={"file": ("empty.csv", file)},
        data={"options": '{"header": true}'},
        headers=headers,
    )
    assert r.status_code == 400, r.text
    assert "payment-required" not in r.headers
    assert r.json() == {"error": EMPTY}
    assert facilitator.verified == []
    assert facilitator.settled == []


async def test_the_last_file_part_of_a_name_decides(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    # The route reads the last part of a name, as FormPeek's fields do: an empty part before a
    # full one is not the file converted, and the request is quoted as usual.
    files = [("file", ("a.csv", b"")), ("file", ("b.csv", b"a\n1\n"))]
    r = await paid_app.http.post(UPLOAD, files=files)
    assert r.status_code == 402, r.text
    files.reverse()
    r = await paid_app.http.post(UPLOAD, files=files)
    assert r.status_code == 400, r.text
    assert r.json() == {"error": EMPTY}
    assert facilitator.verified == []


async def test_a_one_byte_file_is_still_quoted(paid_app: kit.AppHarness) -> None:
    r = await paid_app.http.post(UPLOAD, files={"file": ("x.csv", b"a")})
    assert r.status_code == 402, r.text


async def test_without_payments_the_route_gives_the_same_refusal(
    free_app: kit.AppHarness,
) -> None:
    r = await free_app.http.post(UPLOAD, files={"file": ("empty.csv", b"")})
    assert r.status_code == 400, r.text
    assert r.json() == {"error": EMPTY}
