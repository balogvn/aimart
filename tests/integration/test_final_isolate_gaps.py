"""Two refusals that shipped in e8d228e without tests of their own (AGENTS.md "Known gaps").

1. A time past year 9999 has no Python value. A '9999-12-31 23:59:59-05:00' sentinel is
   10000-01-01 04:59:59 UTC once converted, and preview and query answered a bare 500 when
   ``to_pylist()`` raised OverflowError; ``tabular._python_values`` makes it a 422 with a cast
   hint, unpaid, and the cast the hint names works.
2. DuckDB running out of memory while ``read_csv`` loads a file is a size problem, not a parsing
   one: the generic handler in ``readers/delimited.py`` used to answer 422 with a hint about the
   delimiter. It is 413 ``input_too_large``, unpaid.

Both are asserted on the wire through the paid REST routes: the status, the error code, and that
nothing was settled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import duckdb
import pytest

import aimart_testkit as kit
from aimart_parquet.engine.limits import Limits
from aimart_parquet.errors import InputTooLarge

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

PAST_9999 = b"ts,x\n9999-12-31 23:59:59-05:00,1\n2025-01-15 10:00:00+01:00,2\n"
ROW = "alpha,beta,gamma,delta,epsilon,zeta,eta,theta\n"
WIDE_CSV = ("c1,c2,c3,c4,c5,c6,c7,c8\n" + ROW * (4 * 1024 * 1024 // len(ROW))).encode()


def _body(data: bytes, name: str) -> dict[str, Any]:
    return {"content_base64": kit.b64(data), "filename": name}


async def test_a_time_past_year_9999_is_a_422_in_preview_and_query(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    converted = await paid_app.paying.post("/v1/convert", json=_body(PAST_9999, "s.csv"))
    assert converted.status_code == 200, converted.text
    column = next(c for c in converted.json()["report"]["columns"] if c["name"] == "ts")
    assert column["type"].startswith("timestamp")  # zoned text became an instant (UTC)
    artifact_id = converted.json()["artifact"]["id"]
    assert len(facilitator.settled) == 1

    refused = {
        "preview": await paid_app.paying.post("/v1/preview", json={"artifact_id": artifact_id}),
        "query": await paid_app.paying.post(
            "/v1/query", json={"artifact_id": artifact_id, "sql": "SELECT ts, x FROM data"}
        ),
    }
    for route, r in refused.items():
        assert r.status_code == 422, (route, r.text)  # before: a bare 500
        error = r.json()["error"]
        assert error["code"] == "invalid_input", route
        assert error["message"].startswith("Column 'ts' of type timestamp"), route
        assert "cannot be shown" in error["message"], route
        assert "CAST(x AS VARCHAR)" in error["hint"], route
        assert "not charged" in error["hint"], route
        assert "payment-response" not in r.headers, route
    assert len(facilitator.settled) == 1  # neither refusal was charged

    # The other column, and the cast the hint names, still work (and are paid for).
    cast = await paid_app.paying.post(
        "/v1/query",
        json={
            "artifact_id": artifact_id,
            "sql": "SELECT CAST(ts AS VARCHAR) AS ts, x FROM data ORDER BY x",
        },
    )
    assert cast.status_code == 200, cast.text
    assert cast.json()["rows"][0][0].startswith("10000-01-01 04:59:59")
    assert cast.json()["rows"][1] == ["2025-01-15 09:00:00+00", 2]
    only_x = await paid_app.paying.post(
        "/v1/preview", json={"artifact_id": artifact_id, "columns": ["x"]}
    )
    assert only_x.status_code == 200, only_x.text
    assert only_x.json()["rows"] == [[1], [2]]


async def test_read_csv_out_of_memory_is_a_413(
    make_settings: Callable[..., Settings], facilitator: kit.FakeFacilitator
) -> None:
    # A tiny DuckDB memory limit stands in for a file too big for the real one (1 GB).
    settings = make_settings(duckdb_memory_limit="16MB", duckdb_threads=1)
    async with kit.running_app(settings, facilitator=facilitator) as h:
        r = await h.paying.post("/v1/convert", json=_body(WIDE_CSV, "wide.csv"))
        assert r.status_code == 413, r.text  # before: 422 with a hint about the delimiter
        error = r.json()["error"]
        assert error["code"] == "input_too_large"
        assert error["message"] == "The file needs more memory to read than this server allows."
        assert "not charged" in error["hint"]
        assert "delimiter" not in error["hint"]
        assert "payment-response" not in r.headers
        assert facilitator.settled == []
        # The same file converts with the default limit: the refusal was about memory.
    async with kit.running_app(make_settings(), facilitator=kit.FakeFacilitator()) as h:
        ok = await h.paying.post("/v1/convert", json=_body(WIDE_CSV, "wide.csv"))
        assert ok.status_code == 200, ok.text


def test_the_413_comes_from_duckdb_running_out_inside_read_csv(tmp_path: Path) -> None:
    with pytest.raises(InputTooLarge) as info:
        kit.convert_bytes(tmp_path, "wide.csv", WIDE_CSV, limits=Limits(duckdb_memory_limit="16MB"))
    assert isinstance(info.value.__cause__, duckdb.OutOfMemoryException)
    assert info.value.http_status == 413
