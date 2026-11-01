"""An upload form past Starlette's own limits is refused, typed, before the quote.

Starlette's form parser refuses a non-file part over 1 MiB, more than 1000 fields or more than
1000 files, with an untyped 400 {"detail": "Part exceeded maximum size of 1024KB."}. FormPeek
read only the first 1 MiB of a form, so an ``options`` field over 1 MiB (a long encoding name,
or valid ConvertOptions with 2,000 long column names) was never seen whole: it was quoted, and
paid and streamed as uvicorn delivers it, verified and only then refused, with no code or hint.
FormPeek now applies the same limits, reading a field open at its 1 MiB to the field's end.
"""

from __future__ import annotations

import base64
import inspect
import io
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from starlette.requests import Request

import aimart_testkit as kit
from aimart_parquet.api.middleware import MAX_FIELD_BYTES, MAX_FIELDS, MAX_FILES

if TYPE_CHECKING:
    import httpx

pytestmark = pytest.mark.anyio

UPLOAD = "/v1/convert/upload"
CSV = b"a,b\n1,2\n"
BIG_ENCODING = json.dumps({"encoding": "x" * (MAX_FIELD_BYTES + 100 * 1024)})
BIG_VALID = json.dumps(
    {"expected_schema": [{"name": f"c{i}_" + "n" * 600, "type": "string"} for i in range(2000)]}
)
FIELD_TOO_BIG = "Form field 'options' is larger than 1024 KB, the most one field may hold."
ROOT = Path(__file__).resolve().parents[2]


def test_the_guide_quotes_these_refusals() -> None:
    guide = " ".join((ROOT / "docs" / "agents-guide.md").read_text(encoding="utf-8").split())
    for problem in (
        FIELD_TOO_BIG,
        f"The form has more than {MAX_FIELDS} fields.",
        f"The form has more than {MAX_FILES} files.",
    ):
        assert f'"{problem}"' in guide


def test_the_limits_are_starlettes() -> None:
    defaults = {k: p.default for k, p in inspect.signature(Request.form).parameters.items()}
    assert defaults["max_part_size"] == MAX_FIELD_BYTES
    assert defaults["max_fields"] == MAX_FIELDS
    assert defaults["max_files"] == MAX_FILES


async def _signature(h: kit.AppHarness) -> dict[str, str]:
    from x402 import parse_payment_required

    quote = await h.http.post(UPLOAD, files={"file": ("q.csv", b"a\n1\n")})
    required = parse_payment_required(base64.b64decode(quote.headers["payment-required"]))
    payload = await kit.paying_client().create_payment_payload(required)
    header = base64.b64encode(
        json.dumps(payload.model_dump(by_alias=True, exclude_none=True)).encode()
    ).decode()
    return {"payment-signature": header}


def _refused(r: httpx.Response, problem: str, fake: kit.FakeFacilitator) -> None:
    assert r.status_code == 400, r.text
    assert "payment-required" not in r.headers
    error = r.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["hint"]
    assert [p["problem"] for p in error["details"]["problems"]] == [problem]
    assert fake.verified == []
    assert fake.settled == []


@pytest.mark.parametrize("options", [BIG_ENCODING, BIG_VALID], ids=["bad-encoding", "valid"])
@pytest.mark.parametrize("how", ["unpaid", "paid", "paid-streamed"])
async def test_options_over_1_mib_are_refused_before_the_quote(
    options: str, how: str, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    headers = await _signature(paid_app) if how != "unpaid" else {}
    # As uvicorn delivers a body, in chunks, so the peek used to stop at 1 MiB: a file object.
    file: Any = io.BytesIO(CSV) if how == "paid-streamed" else CSV
    r = await paid_app.http.post(
        UPLOAD, files={"file": ("x.csv", file)}, data={"options": options}, headers=headers
    )
    _refused(r, FIELD_TOO_BIG, facilitator)


async def test_options_of_exactly_1_mib_are_still_quoted_and_served(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    head = '{"encoding": "utf-8"'
    options = head + " " * (MAX_FIELD_BYTES - len(head) - 1) + "}"  # valid JSON, 1 MiB exactly
    assert len(options.encode()) == MAX_FIELD_BYTES
    unpaid = await paid_app.http.post(
        UPLOAD, files={"file": ("x.csv", CSV)}, data={"options": options}
    )
    assert unpaid.status_code == 402, unpaid.text
    r = await paid_app.http.post(
        UPLOAD,
        files={"file": ("x.csv", io.BytesIO(CSV))},
        data={"options": options},
        headers=await _signature(paid_app),
    )
    assert r.status_code == 200, r.text
    assert len(facilitator.settled) == 1


@pytest.mark.parametrize("paying", [False, True], ids=["unpaid", "paid"])
async def test_too_many_fields_or_files_are_refused_before_the_quote(
    paying: bool, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    headers = await _signature(paid_app) if paying else {}
    fields = {f"f{i}": "1" for i in range(MAX_FIELDS + 1)}
    r = await paid_app.http.post(
        UPLOAD, files={"file": ("x.csv", io.BytesIO(CSV))}, data=fields, headers=headers
    )
    _refused(r, f"The form has more than {MAX_FIELDS} fields.", facilitator)
    files = [("file", (f"x{i}.csv", b"a\n")) for i in range(MAX_FILES + 1)]
    r = await paid_app.http.post(UPLOAD, files=files, headers=headers)
    _refused(r, f"The form has more than {MAX_FILES} files.", facilitator)


async def test_an_urlencoded_field_over_1_mib_is_refused_before_the_quote(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    r = await paid_app.http.post(UPLOAD, data={"options": BIG_ENCODING})
    _refused(r, FIELD_TOO_BIG, facilitator)


async def test_without_payments_the_route_answers_the_same_limits_typed(
    free_app: kit.AppHarness,
) -> None:
    # No FormPeek without payments: Starlette's own refusal, which was {"detail": ...}.
    r = await free_app.http.post(
        UPLOAD, files={"file": ("x.csv", io.BytesIO(CSV))}, data={"options": BIG_VALID}
    )
    assert r.status_code == 400, r.text
    error = r.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["details"]["problems"] == [
        {"field": "", "problem": "Part exceeded maximum size of 1024KB."}
    ]
    hidden = await free_app.http.get("/v1/demand")  # an HTTPException that is not a 400
    assert hidden.status_code == 404
    assert hidden.json() == {"detail": "Not Found"}  # keeps FastAPI's answer
