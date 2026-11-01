"""The pre-quote checks agree with the service.

``Service.check_input`` / ``check_query`` are what REST and MCP run before quoting a price; the
service runs its own checks again once paid. If the two disagree, a request is either refused
that would have worked, or quoted and then refused after the payment was verified.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit
from aimart_parquet.artifacts import build_store
from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.errors import AimartError, QueryRejected
from aimart_parquet.service import PRECHECK_SQL_CHARS, Service, SourceRef

if TYPE_CHECKING:
    from collections.abc import Callable

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

A = "Zz" * 12


def make_service(settings: Settings, **kwargs: Any) -> Service:
    return Service(settings, build_store(settings), **kwargs)


SOURCES = [
    SourceRef(),
    SourceRef(url="https://e.example/a.csv", artifact_id=A),
    SourceRef(url="https://e.example/a", content_base64="YQ==", path="/etc/passwd"),
    SourceRef(path="/etc/passwd"),
]


@pytest.mark.parametrize("src", SOURCES, ids=lambda s: ",".join(s.kinds()) or "none")
@pytest.mark.parametrize("local", [False, True], ids=["remote", "local"])
async def test_check_input_raises_what_the_service_raises(
    src: SourceRef, local: bool, settings: Settings
) -> None:
    service = make_service(settings, local_mode=local)
    if local and src.kinds() == ["path"]:
        return  # a path is a valid local input; the file's existence is decided by reading it
    with pytest.raises(AimartError) as precheck:
        service.check_input(src)
    with pytest.raises(AimartError) as served:
        await service.inspect(src)
    assert (precheck.value.code, precheck.value.message, precheck.value.hint) == (
        served.value.code,
        served.value.message,
        served.value.hint,
    )


@pytest.mark.parametrize(
    "url",
    ["ftp://e.example/a", "http://[::1/a.csv", "https://e.example:25/", "http://10.1.2.3/a"],
)
async def test_check_input_raises_what_the_fetcher_raises(url: str, settings: Settings) -> None:
    service = make_service(settings)
    with pytest.raises(AimartError) as precheck:
        service.check_input(SourceRef(url=url))
    with pytest.raises(AimartError) as served:
        await service.inspect(SourceRef(url=url))
    assert (precheck.value.code, precheck.value.message) == (
        served.value.code,
        served.value.message,
    )


async def test_check_input_follows_allow_private_urls(
    make_settings: Callable[..., Settings],
) -> None:
    make_service(make_settings(allow_private_urls=True)).check_input(
        SourceRef(url="http://127.0.0.1/a.csv")
    )


@pytest.mark.parametrize(
    ("tables", "sql"),
    [
        ({}, "SELECT 1"),
        ({f"t{i}": SourceRef(artifact_id=A) for i in range(9)}, "SELECT 1"),
        ({"Bad": SourceRef(artifact_id=A)}, "SELECT 1"),
        ({"data": SourceRef(artifact_id=A)}, "DROP TABLE data"),
        ({"data": SourceRef(artifact_id=A)}, "SELECT 1; SELECT 2"),
        ({"data": SourceRef(artifact_id=A)}, "   "),
    ],
)
async def test_check_query_raises_what_the_query_raises(
    tables: dict[str, SourceRef], sql: str, settings: Settings, tmp_path: Any
) -> None:
    service = make_service(settings)
    if tables and all(name.islower() for name in tables) and len(tables) == 1:
        # The SQL is checked where the query runs, so it needs a real table to get there.
        artifact = await service.convert(
            SourceRef(content_base64=kit.b64(b"a\n1\n"), filename="x.csv"), ConvertOptions()
        )
        tables = {"data": SourceRef(artifact_id=artifact.artifact.id)}
    with pytest.raises(AimartError) as precheck:
        service.check_query(tables, sql)
    with pytest.raises(AimartError) as served:
        await service.query(tables, sql, limit=1, fmt="json")
    assert (precheck.value.code, precheck.value.message, precheck.value.hint) == (
        served.value.code,
        served.value.message,
        served.value.hint,
    )


async def test_check_query_parses_only_short_sql(settings: Settings) -> None:
    # DuckDB's parser holds the GIL and is cubic on runs of '+' and '-': 1,600 characters took
    # 1.3 s, and the 20,000 the schema allows would take most of an hour in the API process.
    service = make_service(settings)
    tables = {"data": SourceRef(artifact_id=A)}
    with pytest.raises(QueryRejected):
        service.check_query(tables, "DROP TABLE data".ljust(PRECHECK_SQL_CHARS))
    service.check_query(tables, "DROP TABLE data".ljust(PRECHECK_SQL_CHARS + 1))
    service.check_query(tables, "SELECT " + "+-" * 10_000)  # returns at once: not parsed here


async def test_check_query_checks_every_table_input(settings: Settings) -> None:
    service = make_service(settings)
    with pytest.raises(AimartError, match=r"Table 't': provide exactly one input; got 0"):
        service.check_query({"data": SourceRef(artifact_id=A), "t": SourceRef()}, "SELECT 1")
    with pytest.raises(AimartError, match="Only http and https"):
        service.check_query({"t": SourceRef(url="ftp://e.example/t")}, "SELECT 1")


async def test_query_and_check_query_share_one_check_tables(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Integration review: query's head kept its own copy of check_query's table rules, held
    # equal only by the test above. Both now call check_tables, the one source of those rules.
    service = make_service(settings)
    calls: list[str] = []

    class Refused(Exception):
        pass

    def check_tables(tables: Any) -> None:
        calls.append(",".join(tables))
        raise Refused

    monkeypatch.setattr(service, "check_tables", check_tables)
    tables = {"data": SourceRef(artifact_id=A)}
    with pytest.raises(Refused):
        service.check_query(tables, "SELECT 1")
    with pytest.raises(Refused):
        await service.query(tables, "SELECT 1", limit=1, fmt="json")
    assert calls == ["data", "data"]
