"""The Apify Actor (apify/): its per-input work, its charges, and the files Apify reads.

The work (``aimart_actor.core``) runs here without the Apify SDK, against the package's real
``Service`` (in-process, ``isolate=False``) and a stub of the platform that records every charge,
stored record and dataset item. The rule under test is the hosted service's: a failure is never
charged, and nothing is delivered unpaid. No network: the fetcher is replaced where the service
calls it, as in ``test_service.py``.
"""

from __future__ import annotations

import ast
import base64
import importlib
import io
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import jsonschema
import pyarrow.parquet as pq
import pytest

from aimart_parquet import service as service_module
from aimart_parquet.config import Settings
from aimart_parquet.engine.models import ConvertOptions, InputFormat
from aimart_parquet.errors import InvalidRequest
from aimart_parquet.sources import Fetched

if TYPE_CHECKING:
    from aimart_parquet.service import Service

ROOT = Path(__file__).resolve().parents[2]
ACTOR = ROOT / "apify"
SAMPLE = ROOT / "docs" / "examples" / "sales.csv"
SAMPLE_URL = "https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.csv"
if str(ACTOR) not in sys.path:
    sys.path.insert(0, str(ACTOR))
core = importlib.import_module("aimart_actor.core")

pytestmark = pytest.mark.anyio


@dataclass
class StubPlatform:
    """``core.Platform`` with a budget counted in events, recording everything it is asked."""

    budget: int | None = None  # events the run's maximum charge still covers; None: unlimited
    charge_answers: list[bool | Exception | None] = field(default_factory=list)
    charges: list[tuple[str, str]] = field(default_factory=list)
    saved: dict[str, bytes] = field(default_factory=dict)
    discarded: list[str] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)

    def affordable(self, event: str) -> bool:
        self.asked.append(event)
        return self.budget is None or self.budget >= 1

    async def charge(self, event: str, *, key: str) -> bool | None:
        answer = self.charge_answers.pop(0) if self.charge_answers else True
        if isinstance(answer, Exception):
            raise answer
        if answer:
            self.charges.append((event, key))
            if self.budget is not None:
                self.budget -= 1
        return answer

    async def save(self, key: str, path: Path) -> str:
        self.saved[key] = await anyio.Path(path).read_bytes()
        return f"https://api.apify.test/v2/key-value-stores/s/records/{key}?signature=x"

    async def discard(self, key: str) -> None:
        self.discarded.append(key)
        self.saved.pop(key, None)

    async def push(self, item: dict[str, Any]) -> None:
        # What the platform stores: JSON. A value JSON cannot hold must fail here, as it would.
        self.items.append(json.loads(json.dumps(item)))

    @property
    def events(self) -> list[str]:
        return [event for event, _ in self.charges]


@pytest.fixture
def service(tmp_path: Path) -> Service:
    built: Service = core.build_service(tmp_path, isolate=False)
    return built


@dataclass
class Net:
    """Stands in for the network: url -> the bytes its server sends; ``asked`` records fetches."""

    served: dict[str, bytes]
    asked: list[str] = field(default_factory=list)


@pytest.fixture
def fetched(monkeypatch: pytest.MonkeyPatch) -> Net:
    net = Net(served={SAMPLE_URL: SAMPLE.read_bytes()})

    async def fake_fetch(url: str, dest: Path, **kwargs: Any) -> Fetched:
        net.asked.append(url)
        assert kwargs["allow_private"] is False  # the guard stays on, whatever the input
        body = net.served[url]
        await anyio.Path(dest).write_bytes(body)
        return Fetched(
            path=dest, filename=url.rsplit("/", 1)[-1], content_type=None, size=len(body)
        )

    monkeypatch.setattr(service_module, "fetch_url", fake_fetch)
    return net


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def rows_of(parquet: bytes) -> int:
    return int(pq.ParquetFile(io.BytesIO(parquet)).metadata.num_rows)


# ------------------------------------------------------------------ conversions


async def test_a_url_input_is_converted_stored_and_charged_once(
    service: Service, fetched: Net
) -> None:
    platform = StubPlatform()
    summary = await core.run({"urls": [SAMPLE_URL]}, service, platform)

    [item] = platform.items
    assert item["status"] == "converted"
    assert item["input"] == SAMPLE_URL
    assert item["fileName"] == "sales.parquet"
    assert item["rows"] == 6
    assert item["parquetKey"] == "parquet-001-sales.parquet"
    assert item["parquetUrl"].endswith("parquet-001-sales.parquet?signature=x")
    assert [c["name"] for c in item["columns"]] == [
        "order_id",
        "order_date",
        "region",
        "units",
        "unit_price",
    ]
    assert item["report"]["source_format"] == "csv"
    assert rows_of(platform.saved["parquet-001-sales.parquet"]) == 6
    assert item["sizeBytes"] == len(platform.saved["parquet-001-sales.parquet"])
    # One event, after the file was stored, under a key unique to this input.
    assert platform.charges == [("file-converted", "parquet-001-sales.parquet-convert")]
    assert item["charged"] == ["file-converted"]
    assert "query" not in item
    assert summary.converted == 1
    assert summary.charged == {"file-converted": 1, "query-run": 0}
    # Nothing is left in the service's own store: the run's key-value store holds the output.
    assert list((service.settings.artifact_dir).glob("*/*")) == []


async def test_a_base64_input_is_converted_under_its_file_name(service: Service) -> None:
    platform = StubPlatform()
    raw = {"files": [{"fileName": "orders.csv", "contentBase64": b64(SAMPLE.read_bytes())}]}
    await core.run(raw, service, platform)

    [item] = platform.items
    assert item["status"] == "converted"
    assert item["input"] == "file:orders.csv"
    assert item["fileName"] == "orders.parquet"
    assert rows_of(platform.saved["parquet-001-orders.parquet"]) == 6
    assert platform.events == ["file-converted"]


async def test_urls_come_first_then_files_one_item_each(service: Service, fetched: Net) -> None:
    platform = StubPlatform()
    raw = {"urls": [SAMPLE_URL], "files": [{"contentBase64": b64(SAMPLE.read_bytes())}]}
    await core.run(raw, service, platform)
    assert [(i["index"], i["input"], i["status"]) for i in platform.items] == [
        (1, SAMPLE_URL, "converted"),
        (2, "file:input-2", "converted"),
    ]
    assert platform.events == ["file-converted", "file-converted"]
    assert set(platform.saved) == {"parquet-001-sales.parquet", "parquet-002-data.parquet"}


async def test_options_reach_the_engine(service: Service, fetched: Net) -> None:
    platform = StubPlatform()
    await core.run({"urls": [SAMPLE_URL], "options": {"infer_types": False}}, service, platform)
    [item] = platform.items
    assert {c["type"] for c in item["columns"]} == {"string"}


# ------------------------------------------------------------------ refusals: never charged


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ({"files": [{"fileName": "x.csv", "contentBase64": "!!!not base64!!!"}]}, None),
        (
            {"files": [{"fileName": "x.bin", "contentBase64": b64(b"\x00\x01\x02garbage" * 50)}]},
            None,
        ),
        ({"urls": ["http://127.0.0.1/data.csv"]}, "url_not_allowed"),
        ({"urls": ["http://169.254.169.254/latest/meta-data/"]}, "url_not_allowed"),
    ],
    ids=["bad-base64", "unreadable-bytes", "loopback-url", "metadata-url"],
)
async def test_a_refused_input_is_reported_and_not_charged(
    service: Service, raw: dict[str, Any], code: str | None
) -> None:
    platform = StubPlatform()
    summary = await core.run(raw, service, platform)

    [item] = platform.items
    assert item["status"] == "failed"
    assert item["charged"] == []
    assert item["error"]["code"]
    assert item["error"]["hint"]  # every refusal says what to do next
    if code is not None:
        assert item["error"]["code"] == code
    assert platform.charges == []
    assert platform.saved == {}
    assert summary.failed == 1
    assert summary.charged == {"file-converted": 0, "query-run": 0}


async def test_a_failure_between_good_inputs_charges_only_the_good_ones(
    service: Service, fetched: Net
) -> None:
    platform = StubPlatform()
    raw = {
        "urls": [SAMPLE_URL],
        "files": [
            {"fileName": "broken.xlsx", "contentBase64": b64(b"PK\x03\x04 not a workbook")},
            {"fileName": "ok.csv", "contentBase64": b64(SAMPLE.read_bytes())},
        ],
    }
    summary = await core.run(raw, service, platform)
    assert [i["status"] for i in platform.items] == ["converted", "failed", "converted"]
    assert platform.events == ["file-converted", "file-converted"]
    assert summary.message == "Converted 2 file(s); 1 failed (not charged)."


async def test_an_unexpected_exception_is_a_typed_unpaid_failure(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_: Any, **__: Any) -> Any:
        msg = "/secret/server/path exploded"
        raise RuntimeError(msg)

    monkeypatch.setattr(service, "convert", boom)
    platform = StubPlatform()
    await core.run({"files": [{"contentBase64": b64(SAMPLE.read_bytes())}]}, service, platform)
    [item] = platform.items
    assert item["error"]["code"] == "internal_error"
    assert "/secret" not in json.dumps(item)  # the exception's text never reaches the caller
    assert platform.charges == []


async def test_a_storage_failure_charges_nothing(service: Service) -> None:
    class BrokenStore(StubPlatform):
        async def save(self, key: str, path: Path) -> str:
            msg = "key-value store unavailable"
            raise OSError(msg)

    platform = BrokenStore()
    await core.run({"files": [{"contentBase64": b64(SAMPLE.read_bytes())}]}, service, platform)
    [item] = platform.items
    assert item["status"] == "failed"
    assert platform.charges == []


# ------------------------------------------------------------------ path inputs


@pytest.mark.parametrize(
    "raw",
    [
        {"path": "/etc/passwd"},
        {"paths": ["/etc/passwd"]},
        {"urls": [SAMPLE_URL], "path": "/etc/passwd"},
        {"files": [{"path": "/etc/passwd"}]},
        {"files": [{"contentBase64": "YQ==", "filePath": "/etc/passwd"}]},
    ],
)
async def test_local_path_inputs_are_refused_before_any_work(
    service: Service, fetched: Net, raw: dict[str, Any]
) -> None:
    platform = StubPlatform()
    with pytest.raises(InvalidRequest):
        await core.run(raw, service, platform)
    assert platform.items == []
    assert platform.charges == []
    assert fetched.asked == []


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "/etc/passwd", "../../etc/passwd", "ftp://example.com/a.csv"]
)
async def test_a_url_that_names_a_local_file_is_refused_unpaid(service: Service, url: str) -> None:
    platform = StubPlatform()
    await core.run({"urls": [url]}, service, platform)
    [item] = platform.items
    assert item["status"] == "failed"
    assert item["error"]["code"] == "url_not_allowed"
    assert platform.charges == []
    assert platform.saved == {}


def test_the_service_runs_in_remote_mode_with_the_ssrf_guard_on(service: Service) -> None:
    assert service.local_mode is False  # remote mode refuses SourceRef(path=...)
    assert service.settings.allow_private_urls is False
    assert service.settings.max_concurrent_jobs == 1


def test_the_environment_cannot_open_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIMART_ALLOW_PRIVATE_URLS", "true")
    monkeypatch.setenv("AIMART_ISOLATE_JOBS", "false")
    built = core.build_service(tmp_path)
    assert built.settings.allow_private_urls is False
    assert built.settings.isolate_jobs is True  # the default: crash isolation and the job cap
    defaults = Settings(_env_file=None)
    for limit in ("max_input_bytes", "job_memory_limit_mb", "convert_timeout_s", "max_query_rows"):
        assert getattr(built.settings, limit) == getattr(defaults, limit)


# ------------------------------------------------------------------ queries


QUERY = "SELECT region, sum(units) AS units FROM data GROUP BY region ORDER BY region"


async def test_a_query_runs_over_each_file_and_is_charged_once_each(
    service: Service, fetched: Net
) -> None:
    platform = StubPlatform()
    raw = {"urls": [SAMPLE_URL], "sql": QUERY}
    summary = await core.run(raw, service, platform)

    [item] = platform.items
    assert item["sql"] == QUERY
    assert item["query"]["columns"] == ["region", "units"]
    totals: dict[str, int] = {}
    for line in SAMPLE.read_text().splitlines()[1:]:
        region, units = line.split(",")[2:4]
        totals[region] = totals.get(region, 0) + int(units)
    assert item["query"]["rows"] == [[region, totals[region]] for region in sorted(totals)]
    assert item["query"]["truncated"] is False
    assert platform.charges == [
        ("file-converted", "parquet-001-sales.parquet-convert"),
        ("query-run", "parquet-001-sales.parquet-query"),
    ]
    assert item["charged"] == ["file-converted", "query-run"]
    assert summary.message == "Converted 1 file(s), ran 1 query."


async def test_query_limit_caps_the_rows(service: Service, fetched: Net) -> None:
    platform = StubPlatform()
    await core.run(
        {"urls": [SAMPLE_URL], "sql": "SELECT * FROM data", "queryLimit": 2}, service, platform
    )
    [item] = platform.items
    assert len(item["query"]["rows"]) == 2
    assert item["query"]["truncated"] is True


async def test_a_failed_query_is_not_charged_and_the_conversion_still_is(
    service: Service, fetched: Net
) -> None:
    platform = StubPlatform()
    await core.run(
        {"urls": [SAMPLE_URL], "sql": "SELECT no_such_column FROM data"}, service, platform
    )
    [item] = platform.items
    assert item["status"] == "converted"
    assert "query" not in item
    assert item["queryError"]["code"] == "query_failed"
    assert platform.events == ["file-converted"]


@pytest.mark.parametrize(
    "sql", ["DELETE FROM data", "SELECT 1; SELECT 2", "SELEC * FROM data", "COPY data TO 'x.csv'"]
)
async def test_sql_that_can_only_fail_refuses_the_run_before_any_fetch(
    service: Service, fetched: Net, sql: str
) -> None:
    platform = StubPlatform()
    await core.run({"urls": [SAMPLE_URL, SAMPLE_URL], "sql": sql}, service, platform)
    assert [i["status"] for i in platform.items] == ["failed", "failed"]
    assert {i["error"]["code"] for i in platform.items} == {"query_rejected"}
    assert fetched.asked == []
    assert platform.charges == []


async def test_a_query_result_too_large_for_a_dataset_item_is_not_charged(
    service: Service, fetched: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "MAX_QUERY_JSON_BYTES", 50)
    platform = StubPlatform()
    await core.run({"urls": [SAMPLE_URL], "sql": "SELECT * FROM data"}, service, platform)
    [item] = platform.items
    assert item["queryError"]["code"] == "input_too_large"
    assert platform.events == ["file-converted"]


# ------------------------------------------------------------------ the run's maximum charge


async def test_no_budget_means_no_work_and_no_charge(service: Service, fetched: Net) -> None:
    platform = StubPlatform(budget=0)
    summary = await core.run({"urls": [SAMPLE_URL, SAMPLE_URL]}, service, platform)
    assert [i["status"] for i in platform.items] == ["skipped", "skipped"]
    assert {i["error"]["code"] for i in platform.items} == {"charge_limit_reached"}
    assert fetched.asked == []
    assert platform.charges == []
    assert summary.skipped == 2
    assert "maximum charge was reached" in summary.message


async def test_the_run_stops_at_its_maximum_charge(service: Service, fetched: Net) -> None:
    platform = StubPlatform(budget=2)
    await core.run({"urls": [SAMPLE_URL] * 3, "sql": QUERY}, service, platform)
    first, second, third = platform.items
    # File 1 and its query fit; file 2 is converted, its query no longer fits; file 3 is not
    # even fetched.
    assert first["charged"] == ["file-converted", "query-run"]
    assert second["status"] == "skipped"
    assert third["status"] == "skipped"
    assert platform.events == ["file-converted", "query-run"]
    assert fetched.asked == [SAMPLE_URL]


async def test_a_query_that_no_longer_fits_keeps_the_conversion(
    service: Service, fetched: Net
) -> None:
    platform = StubPlatform(budget=1)
    await core.run({"urls": [SAMPLE_URL, SAMPLE_URL], "sql": QUERY}, service, platform)
    first, second = platform.items
    assert first["status"] == "converted"
    assert first["charged"] == ["file-converted"]
    assert first["queryError"]["code"] == "charge_limit_reached"
    assert "query" not in first
    assert second["status"] == "skipped"
    assert platform.events == ["file-converted"]


async def test_a_conversion_whose_charge_is_refused_is_withdrawn(
    service: Service, fetched: Net
) -> None:
    platform = StubPlatform(charge_answers=[False])
    await core.run({"urls": [SAMPLE_URL, SAMPLE_URL]}, service, platform)
    first, second = platform.items
    assert first["status"] == "skipped"
    assert "parquetUrl" not in first
    assert platform.discarded == ["parquet-001-sales.parquet"]
    assert platform.saved == {}
    assert second["status"] == "skipped"  # the run stops at the limit
    assert platform.charges == []


async def test_a_charge_that_errors_withdraws_the_output(service: Service, fetched: Net) -> None:
    platform = StubPlatform(charge_answers=[RuntimeError("API unavailable")])
    await core.run({"urls": [SAMPLE_URL]}, service, platform)
    [item] = platform.items
    assert item["status"] == "failed"
    assert item["error"]["code"] == "charge_failed"
    assert "parquetUrl" not in item
    assert platform.saved == {}
    assert platform.charges == []


async def test_a_query_whose_charge_is_refused_drops_its_rows(
    service: Service, fetched: Net
) -> None:
    platform = StubPlatform(charge_answers=[True, False])
    await core.run({"urls": [SAMPLE_URL], "sql": QUERY}, service, platform)
    [item] = platform.items
    assert "query" not in item
    assert item["queryError"]["code"] == "charge_limit_reached"
    assert item["charged"] == ["file-converted"]


async def test_a_run_that_is_not_pay_per_event_delivers_and_lists_no_charge(
    service: Service, fetched: Net
) -> None:
    """Before the owner sets prices a run charges nothing; its items must not claim charges."""
    platform = StubPlatform(charge_answers=[None, None])
    summary = await core.run({"urls": [SAMPLE_URL], "sql": QUERY}, service, platform)
    [item] = platform.items
    assert item["status"] == "converted"
    assert "query" in item
    assert item["charged"] == []
    assert platform.saved
    assert summary.charged == {"file-converted": 0, "query-run": 0}


@pytest.mark.parametrize(
    ("prices", "problem"),
    [
        ({"file-converted": "0.02", "query-run": "0.005"}, None),
        ({"file-converted": "0.02", "query-run": "0.005", "apify-actor-start": "0.00005"}, None),
        ({"file-converted": "0.02", "query-run": "0.005", "apify-default-dataset-item": "0"}, None),
        ({"file-converted": "0.02"}, "query-run"),
        ({"file_converted": "0.02", "query-run": "0.005"}, "file-converted"),
        ({}, "file-converted, query-run"),
        (
            {"file-converted": "0.02", "query-run": "0.005", "apify-default-dataset-item": "0.001"},
            "apify-default-dataset-item",
        ),
    ],
)
def test_prices_that_would_charge_for_nothing_or_give_work_away_refuse_the_run(
    prices: dict[str, str], problem: str | None
) -> None:
    from decimal import Decimal

    found = core.pricing_problem({k: Decimal(v) for k, v in prices.items()})
    if problem is None:
        assert found is None
    else:
        assert found is not None
        assert problem in found
        assert "you were not charged" in found


# ------------------------------------------------------------------ the input as a whole


@pytest.mark.parametrize(
    "raw",
    [
        {},
        None,
        {"urls": []},
        {"urls": ["   "]},
        {"urls": "https://example.com/a.csv"},
        {"urls": [SAMPLE_URL] * 21},
        {"files": [{"contentBase64": "YQ=="}] * 11},
        {"files": [{"fileName": "a.csv"}]},
        [SAMPLE_URL],
    ],
)
async def test_an_input_naming_nothing_convertible_is_refused(service: Service, raw: Any) -> None:
    platform = StubPlatform()
    with pytest.raises(InvalidRequest) as refused:
        await core.run(raw, service, platform)
    assert refused.value.hint
    assert platform.items == []


@pytest.mark.parametrize(
    "options", [{"no_such_option": 1}, {"format": "docx"}, {"row_group_size": 5}]
)
async def test_invalid_options_fail_every_input_unpaid_and_unfetched(
    service: Service, fetched: Net, options: dict[str, Any]
) -> None:
    platform = StubPlatform()
    await core.run({"urls": [SAMPLE_URL, SAMPLE_URL], "options": options}, service, platform)
    assert [i["status"] for i in platform.items] == ["failed", "failed"]
    assert all(i["error"]["message"].startswith("options.") for i in platform.items)
    assert fetched.asked == []
    assert platform.charges == []


@pytest.mark.parametrize("limit", [0, 1001, True, "10"])
async def test_query_limit_out_of_range_fails_unpaid(service: Service, limit: Any) -> None:
    platform = StubPlatform()
    raw = {
        "files": [{"contentBase64": b64(SAMPLE.read_bytes())}],
        "sql": "SELECT 1",
        "queryLimit": limit,
    }
    await core.run(raw, service, platform)
    [item] = platform.items
    assert item["status"] == "failed"
    assert platform.charges == []


def test_record_keys_stay_in_the_key_value_store_alphabet() -> None:
    key = core.record_key(7, "données été (2026) / ünï.csv.parquet")
    assert re.fullmatch(r"[a-zA-Z0-9!\-_.'()]{1,256}", key)
    assert key.startswith("parquet-007-")
    assert core.record_key(1, ".parquet") == "parquet-001-data.parquet"
    assert len(core.record_key(999, "x" * 500 + ".parquet")) <= 256


# ------------------------------------------------------------------ the files Apify reads


def load(name: str) -> Any:
    return json.loads((ACTOR / ".actor" / name).read_text())


def test_actor_json_points_at_files_that_exist() -> None:
    actor = load("actor.json")
    base = ACTOR / ".actor"
    for ref in (
        actor["dockerfile"],
        actor["readme"],
        actor["input"],
        actor["output"],
        actor["storages"]["dataset"],
        actor["storages"]["keyValueStore"],
    ):
        assert (base / ref).resolve().is_file(), ref
    assert actor["actorSpecification"] == 1
    assert re.fullmatch(r"\d+\.\d+", actor["version"])
    # The package's job cap (700 MB) plus the Actor process and the forkserver fit in 2 GB.
    assert actor["minMemoryMbytes"] >= 2048


async def test_every_dataset_item_shape_matches_the_dataset_schema(
    service: Service, fetched: Net
) -> None:
    """The platform validates each push against `fields` and discards the whole push when one
    item fails, after the input was charged."""
    schema = load("dataset_schema.json")["fields"]
    platform = StubPlatform(budget=2)
    raw = {
        "urls": ["http://127.0.0.1/x.csv", SAMPLE_URL, SAMPLE_URL, SAMPLE_URL],
        "sql": "SELECT * FROM data",
    }
    await core.run(raw, service, platform)
    assert {i["status"] for i in platform.items} == {"converted", "failed", "skipped"}
    for item in platform.items:
        jsonschema.Draft7Validator(schema).validate(item)
        unknown = set(item) - set(schema["properties"])
        assert not unknown, f"fields the dataset schema does not describe: {unknown}"


def test_the_input_schema_is_what_the_code_reads() -> None:
    schema = load("input_schema.json")
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == core.INPUT_FIELDS
    assert set(schema["properties"]["files"]["items"]["properties"]) == core.FILE_FIELDS
    assert schema["properties"]["urls"]["maxItems"] == core.MAX_URLS
    assert schema["properties"]["files"]["maxItems"] == core.MAX_FILES
    defaults = Settings(_env_file=None)
    assert schema["properties"]["queryLimit"]["maximum"] == defaults.max_query_rows
    # Apify's daily test runs the Actor on its prefill: the sample must be real and convertible.
    assert schema["properties"]["urls"]["prefill"] == [SAMPLE_URL]
    assert SAMPLE_URL.endswith("/".join(SAMPLE.parts[-3:]))
    # Agents read the descriptions (mcp.apify.com shows them as the tool's arguments): the
    # options text must name every option and every format the package takes.
    options_text = schema["properties"]["options"]["description"]
    for name in ConvertOptions.model_fields:
        assert name in options_text, name
    urls_text = schema["properties"]["urls"]["description"] + options_text
    for fmt in InputFormat:
        assert fmt.value in urls_text, fmt


def test_output_and_key_value_store_schemas_name_the_keys_the_code_writes() -> None:
    kvs = load("key_value_store_schema.json")["collections"]["parquet"]
    assert core.record_key(1, "x.parquet").startswith(kvs["keyPrefix"])
    assert kvs["contentTypes"] == [core.PARQUET_CONTENT_TYPE]
    output = load("output_schema.json")["properties"]
    assert output["results"]["template"] == "{{links.apiDefaultDatasetUrl}}/items"
    assert "collection=parquet" in output["parquetFiles"]["template"]


def test_the_core_does_not_import_the_apify_sdk() -> None:
    """CI has no Apify SDK; the work must stay importable and testable without it."""
    tree = ast.parse((ACTOR / "aimart_actor" / "core.py").read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert "apify" not in imported
    assert "apify" not in sys.modules or "aimart_actor.actor" in sys.modules


def test_the_entry_point_is_guarded_for_the_forkserver() -> None:
    main = (ACTOR / "aimart_actor" / "__main__.py").read_text()
    assert 'if __name__ == "__main__":' in main
    dockerfile = (ACTOR / "Dockerfile").read_text()
    assert 'CMD ["python", "-m", "aimart_actor"]' in dockerfile


def test_the_image_installs_pinned_hashed_packages() -> None:
    dockerfile = (ACTOR / "Dockerfile").read_text()
    assert "--require-hashes" in dockerfile
    assert re.search(r"^FROM apify/actor-python:3\.13@sha256:[0-9a-f]{64}$", dockerfile, re.M)
    text = (ACTOR / "requirements.txt").read_text()
    pins = dict(re.findall(r"^([a-z0-9._-]+)==(\S+) \\$", text, re.M))
    assert pins["aimart-parquet"] == "0.1.2"
    assert pins["apify"] == "4.2.0"
    # AGENTS.md: re-check the multi-member gzip loss before moving DuckDB past 1.5.6.
    assert pins["duckdb"] == "1.5.6"
    requirements = [line for line in text.splitlines() if re.match(r"^[a-z0-9]", line)]
    assert len(requirements) == len(pins)  # every requirement is an exact pin with hashes
    assert text.count("--hash=sha256:") >= len(pins)


def test_the_store_page_and_the_owner_guide_state_the_real_events_and_prices() -> None:
    """Text is interface: the README's prices are the hosted service's, the event names are the
    code's, and docs/apify.md tells the owner to type exactly those."""
    defaults = Settings(_env_file=None)
    readme = (ACTOR / "README.md").read_text()
    guide = (ROOT / "docs" / "apify.md").read_text()
    for text in (readme, guide):
        assert f"`{core.EVENT_CONVERT}`" in text
        assert f"`{core.EVENT_QUERY}`" in text
        assert defaults.price_convert in text
        assert defaults.price_query in text
    assert SAMPLE_URL in readme
    assert defaults.public_base_url not in readme
    assert "https://balogvn--aimart-parquet.us-east.modal.run" in readme
    assert core.DATASET_ITEM_EVENT in guide  # the owner removes it: it would charge failures
    assert "apify-actor-start" in guide
    discovery = (ROOT / "docs" / "discovery.md").read_text()
    assert "(apify.md)" in discovery
