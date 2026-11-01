"""inspect, preview and query refuse inline bytes that can never open as Parquet before the quote.

They read their input with ``pq.ParquetFile``, which judges a file by its last 8 bytes before
anything else. A ``content_base64`` that fails that (most often a CSV sent to preview or query
before it was converted) was quoted and, paid, verified, then refused with 422 invalid_input.
The bytes are decoded before the quote anyway, so the same 422 now comes first, in the same
words: the tests below hold the pre-quote error to what the service itself answers.
"""

from __future__ import annotations

import base64
import io
import json
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import aimart_testkit as kit
from aimart_parquet.artifacts import build_store
from aimart_parquet.errors import AimartError, InvalidInput
from aimart_parquet.service import Service, SourceRef

if TYPE_CHECKING:
    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

_buf = io.BytesIO()
pq.write_table(pa.table({"a": [1, 2], "b": ["x", "y"]}), _buf)
PARQUET = _buf.getvalue()
CSV = b"a,b\n1,x\n2,y\n"

NOT_PARQUET = {
    "csv": CSV,
    "one byte": b"a",
    "seven bytes": b"abcPAR1",
    "magic only": b"PAR1",
    "gzip": b"\x1f\x8b\x08\x00" + b"\x00" * 20,
    "truncated parquet": PARQUET[:-1],
    "encrypted footer": PARQUET[:-4] + b"PARE",  # no server holds the keys
}
# What only the engine can judge, or what opens: these must still be quoted.
MAYBE_PARQUET = {
    "parquet": PARQUET,
    "no leading magic": b"XXXX" + PARQUET[4:],  # pyarrow never reads it: this file opens
    "empty footer": b"\x00\x00\x00\x00PAR1",
}


def _error(exc: AimartError) -> tuple[str, str, str]:
    return exc.code, exc.message, exc.hint


async def _served(service: Service, op: str, raw: bytes) -> AimartError | None:
    src = SourceRef(content_base64=kit.b64(raw), filename="x.parquet")
    try:
        if op == "inspect":
            await service.inspect(src)
        elif op == "preview":
            await service.preview(src, columns=None, offset=0, limit=5, fmt="json")
        else:
            await service.query({"data": src}, "SELECT * FROM data", limit=5, fmt="json")
    except AimartError as exc:
        return exc
    return None


def _prechecked(service: Service, op: str, raw: bytes) -> AimartError | None:
    src = SourceRef(content_base64=kit.b64(raw), filename="x.parquet")
    try:
        if op == "query":
            service.check_query({"data": src}, "SELECT * FROM data")
        else:
            service.check_parquet_input(src)
    except AimartError as exc:
        return exc
    return None


@pytest.mark.parametrize("op", ["inspect", "preview", "query"])
@pytest.mark.parametrize("name", sorted(NOT_PARQUET))
async def test_the_precheck_refuses_what_the_service_refuses_in_its_words(
    name: str, op: str, settings: Settings
) -> None:
    service = Service(settings, build_store(settings))
    raw = NOT_PARQUET[name]
    before = _prechecked(service, op, raw)
    after = await _served(service, op, raw)
    assert isinstance(before, InvalidInput)
    assert after is not None
    assert _error(before) == _error(after)


@pytest.mark.parametrize("op", ["inspect", "preview", "query"])
@pytest.mark.parametrize("name", sorted(MAYBE_PARQUET))
def test_what_might_open_is_left_to_the_engine(name: str, op: str, settings: Settings) -> None:
    service = Service(settings, build_store(settings))
    assert _prechecked(service, op, MAYBE_PARQUET[name]) is None


def test_convert_still_takes_any_file(settings: Settings) -> None:
    service = Service(settings, build_store(settings))
    for raw in NOT_PARQUET.values():
        assert service.check_input(SourceRef(content_base64=kit.b64(raw))) == raw


async def test_bad_sql_is_still_reported_first_as_the_engine_does(settings: Settings) -> None:
    service = Service(settings, build_store(settings))
    src = SourceRef(content_base64=kit.b64(CSV))
    with pytest.raises(AimartError) as before:
        service.check_query({"data": src}, "DROP TABLE data")
    with pytest.raises(AimartError) as after:
        await service.query({"data": src}, "DROP TABLE data", limit=5, fmt="json")
    assert before.value.code == "query_rejected"
    assert _error(before.value) == _error(after.value)


# ------------------------------------------------------------------------------ over REST
BODIES: dict[str, dict[str, Any]] = {
    "/v1/inspect": {"content_base64": kit.b64(CSV), "filename": "x.csv"},
    "/v1/preview": {"content_base64": kit.b64(CSV), "filename": "x.csv", "limit": 5},
    "/v1/query": {"content_base64": kit.b64(CSV), "filename": "x.csv", "sql": "SELECT * FROM data"},
}
MESSAGES = {
    "/v1/inspect": "Not a readable Parquet file: Parquet magic bytes not found in footer. Either "
    "the file is corrupted or this is not a parquet file.",
    "/v1/query": "Table 'data' is not a readable Parquet file.",
}
MESSAGES["/v1/preview"] = MESSAGES["/v1/inspect"]


async def _signature(h: kit.AppHarness, path: str) -> dict[str, str]:
    from x402 import parse_payment_required

    quote = await h.http.post(path, json={})
    required = parse_payment_required(base64.b64decode(quote.headers["payment-required"]))
    payload = await kit.paying_client().create_payment_payload(required)
    header = base64.b64encode(
        json.dumps(payload.model_dump(by_alias=True, exclude_none=True)).encode()
    ).decode()
    return {"payment-signature": header}


@pytest.mark.parametrize("path", sorted(BODIES))
@pytest.mark.parametrize("paying", [False, True], ids=["unpaid", "paid"])
async def test_rest_refuses_a_csv_sent_to_a_parquet_route_before_the_quote(
    path: str, paying: bool, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    headers = await _signature(paid_app, path) if paying else {}
    r = await paid_app.http.post(path, json=BODIES[path], headers=headers)
    assert r.status_code == 422, r.text
    assert "payment-required" not in r.headers
    error = r.json()["error"]
    assert (error["code"], error["message"]) == ("invalid_input", MESSAGES[path])
    assert "onvert" in error["hint"]
    assert facilitator.verified == []
    assert facilitator.settled == []


async def test_the_same_csv_is_still_quoted_for_convert(paid_app: kit.AppHarness) -> None:
    r = await paid_app.http.post("/v1/convert", json=BODIES["/v1/inspect"])
    assert r.status_code == 402, r.text


@pytest.mark.parametrize("path", sorted(BODIES))
async def test_inline_parquet_is_still_served_and_paid(
    path: str, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    body = dict(BODIES[path], content_base64=kit.b64(b"XXXX" + PARQUET[4:]), filename="x.parquet")
    r = await paid_app.paying.post(path, json=body)
    assert r.status_code == 200, r.text
    assert len(facilitator.settled) == 1
