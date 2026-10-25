"""/v1/query and /v1/preview over the real app: inputs that are not Parquet fail typed, not 500.

A bare 500 carries no ``error.code`` or hint, so the calling agent cannot tell a bad input from
an outage. Every caller-triggerable failure must be an ``AimartError`` (>= 400, not charged).
"""

from __future__ import annotations

import decimal
import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import aimart_testkit as kit
from aimart_parquet import sources

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    import httpx

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

CSV = b"region,units\nNorth,10\nSouth,20\n"


def _parquet_bytes() -> bytes:
    buf = io.BytesIO()
    pq.write_table(pa.table({"id": [1, 2], "x": ["a", "b"]}), buf)
    return buf.getvalue()


def _parquet_with(column: pa.Array) -> bytes:
    buf = io.BytesIO()
    pq.write_table(pa.table({"id": pa.array(range(len(column))), "h": column}), buf)
    return buf.getvalue()


# Valid Parquet that DuckDB cannot register: 'Unsupported Internal Arrow Type' from
# con.register, which sat outside every handler (a bare 500), or, from the isolated worker,
# 500 internal_error with "Retry", which cannot help.
UNREGISTRABLE = {
    "float16": pa.array([1.5, 2.0], pa.float16()),
    "decimal256": pa.array([decimal.Decimal("1.25"), None], pa.decimal256(50, 2)),
}


def _corrupt_parquet_bytes() -> bytes:
    """A valid footer over a data page whose header is garbage: it fails only when read."""
    data = bytearray(_parquet_bytes())
    meta = pq.ParquetFile(io.BytesIO(bytes(data))).metadata
    offset = meta.row_group(0).column(1).data_page_offset
    data[offset : offset + 20] = b"\xff" * 20
    return bytes(data)


class _CsvHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Length", str(len(CSV)))
        self.end_headers()
        self.wfile.write(CSV)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def csv_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CsvHandler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    monkeypatch.setattr(sources, "ALLOWED_PORTS", frozenset({80, 443, port}))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/bank.csv"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


@pytest.fixture
async def local_app(make_settings: Callable[..., Settings]) -> AsyncIterator[kit.AppHarness]:
    async with kit.running_app(make_settings(allow_private_urls=True), paid=False) as harness:
        yield harness


def _assert_typed_error(r: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    error: dict[str, Any] = r.json()["error"]
    assert error["code"] == code
    assert error["hint"]
    return error


async def test_query_on_a_csv_input_is_a_typed_422(free_app: kit.AppHarness) -> None:
    r = await free_app.http.post(
        "/v1/query",
        json={"content_base64": kit.b64(CSV), "filename": "t.csv", "sql": "SELECT 1"},
    )
    error = _assert_typed_error(r, 422, "invalid_input")
    assert "'data'" in error["message"]
    assert "/v1/convert" in error["hint"]


async def test_query_with_a_csv_joined_table_names_that_table(
    local_app: kit.AppHarness, csv_url: str
) -> None:
    r = await local_app.http.post(
        "/v1/query",
        json={
            "content_base64": kit.b64(_parquet_bytes()),
            "filename": "t.parquet",
            "sql": "SELECT count(*) FROM data JOIN bank ON true",
            "tables": {"bank": {"url": csv_url}},
        },
    )
    error = _assert_typed_error(r, 422, "invalid_input")
    assert "'bank'" in error["message"]
    assert "artifact_id" in error["hint"]


async def test_query_on_corrupt_parquet_is_typed(free_app: kit.AppHarness) -> None:
    r = await free_app.http.post(
        "/v1/query",
        json={
            "content_base64": kit.b64(_corrupt_parquet_bytes()),
            "filename": "t.parquet",
            "sql": "SELECT max(x) FROM data",
        },
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] in {"invalid_input", "query_failed"}


async def test_preview_on_corrupt_parquet_is_a_typed_422(free_app: kit.AppHarness) -> None:
    r = await free_app.http.post(
        "/v1/preview",
        json={"content_base64": kit.b64(_corrupt_parquet_bytes()), "filename": "t.parquet"},
    )
    _assert_typed_error(r, 422, "invalid_input")


async def test_preview_on_a_csv_input_is_a_typed_422(free_app: kit.AppHarness) -> None:
    r = await free_app.http.post(
        "/v1/preview", json={"content_base64": kit.b64(CSV), "filename": "t.csv"}
    )
    _assert_typed_error(r, 422, "invalid_input")


@pytest.mark.parametrize("isolate", [False, True], ids=["in-process", "isolated"])
@pytest.mark.parametrize("kind", sorted(UNREGISTRABLE))
async def test_query_on_a_column_type_duckdb_cannot_read_is_a_typed_422(
    make_settings: Callable[..., Settings], isolate: bool, kind: str
) -> None:
    async with kit.running_app(make_settings(isolate_jobs=isolate), paid=False) as app:
        r = await app.http.post(
            "/v1/query",
            json={
                "content_base64": kit.b64(_parquet_with(UNREGISTRABLE[kind])),
                "filename": "t.parquet",
                "sql": "SELECT id FROM data",
            },
        )
    error = _assert_typed_error(r, 422, "invalid_input")
    shown = {"float16": "halffloat", "decimal256": "decimal256(50, 2)"}[kind]
    assert error["message"] == (
        f"Table 'data' has columns of a type SQL queries cannot read: h ({shown})."
    )
    assert "Cast" in error["hint"]
