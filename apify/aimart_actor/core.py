"""The Actor's work, free of the Apify SDK so the repository's tests run it without the SDK.

Every input goes through the package's own ``Service`` in remote mode, as the hosted service
runs it: the SSRF-guarded fetcher (``sources.fetch_url``: addresses checked at connect time,
every redirect re-checked), one ``max_input_bytes`` budget per conversion, the forkserver child
with its deadline and memory cap, and no ``path`` input (remote mode refuses one, and this
module never builds one: the container's own files are not an input).

Money, as on the hosted service: a failure is never charged and unpaid work is never delivered.

* An input is charged (``file-converted``) only after its Parquet is stored in the run's
  key-value store and its link made; a query (``query-run``) only after its rows are ready.
  Every ``AimartError``, and any other exception, charges nothing and becomes a dataset item
  with the error's code and hint.
* Before any work, the run's remaining budget (the user's maximum charge for the run) must
  cover the event. When it does not, that input and every one after it are reported as
  skipped, and nothing more is charged. The check matters: when an event does not fit, the SDK
  charges it anyway, once, "so that the platform detects the exhausted budget and terminates the
  run" (apify 4.2.0, ``ChargingManager.charge``); checking first keeps the run off that path.
* A charge that does not go through (``Platform.charge`` answers False or raises) withdraws
  what it was for: the stored Parquet is deleted, the query rows are dropped.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import ValidationError

from aimart_parquet.artifacts import LocalArtifactStore
from aimart_parquet.config import Settings
from aimart_parquet.engine.models import ConvertOptions, ConvertResult
from aimart_parquet.engine.tabular import validate_select
from aimart_parquet.errors import AimartError, InputTooLarge, InvalidRequest
from aimart_parquet.service import PRECHECK_SQL_CHARS, Service, SourceRef, internal_error

if TYPE_CHECKING:
    from collections.abc import Mapping
    from decimal import Decimal
    from pathlib import Path

log = logging.getLogger("aimart.actor")

# Event names as they must be typed in Apify Console (Publication > Monetization). The README,
# docs/apify.md and the tests read them from here.
EVENT_CONVERT = "file-converted"
EVENT_QUERY = "query-run"
EVENTS = (EVENT_CONVERT, EVENT_QUERY)
# The platform's own per-item event: when it has a price, every dataset item costs the user that
# price, a failure's item included. docs/apify.md has the owner delete it.
DATASET_ITEM_EVENT = "apify-default-dataset-item"

MAX_URLS = 20
MAX_FILES = 10
# A dataset item must stay under 9 MB (docs.apify.com/storage/dataset, "Limits"); a push over
# it fails after the query was charged. Larger query results are refused before the charge.
MAX_QUERY_JSON_BYTES = 5 * 1024 * 1024
PARQUET_CONTENT_TYPE = "application/vnd.apache.parquet"
INPUT_FIELDS = frozenset({"urls", "files", "options", "sql", "queryLimit"})
FILE_FIELDS = frozenset({"fileName", "contentBase64"})
# A refusal's hint names the inputs a caller can set (SourceRef.takes).
TAKES = ("url", "content_base64")
# Key-value store keys: at most 256 characters of a-zA-Z0-9!-_.'() (docs.apify.com/storage/
# key-value-store, "Limits").
_KEY_UNSAFE = re.compile(r"[^a-zA-Z0-9!\-_.'()]")


class Platform(Protocol):
    """What the work needs from the Apify platform (``actor.ApifyPlatform``; a stub in tests)."""

    def affordable(self, event: str) -> bool:
        """Whether the run's remaining budget covers one ``event`` (and its dataset item)."""

    async def charge(self, event: str, *, key: str) -> bool | None:
        """Charge one ``event``: True when charged, False when refused (the result must then be
        withheld), None when the run is not pay-per-event (nothing to charge)."""

    async def save(self, key: str, path: Path) -> str:
        """Store ``path`` in the run's key-value store under ``key``; return its public URL."""

    async def discard(self, key: str) -> None:
        """Delete the record ``key`` from the run's key-value store."""

    async def push(self, item: dict[str, Any]) -> None:
        """Append ``item`` to the run's default dataset."""


@dataclass(frozen=True)
class Job:
    """One input: a public URL, or inline base64 bytes with an optional file name."""

    index: int
    url: str | None = None
    content_base64: str | None = None
    filename: str | None = None

    @property
    def label(self) -> str:
        """How the dataset item names the input (never the base64 itself)."""
        if self.url is not None:
            return self.url
        return f"file:{self.filename or f'input-{self.index}'}"


@dataclass
class Plan:
    """The run's input, read and checked once. ``refusal`` fails every input, unpaid."""

    jobs: list[Job]
    options: ConvertOptions
    sql: str | None = None
    query_limit: int = 100
    refusal: AimartError | None = None


@dataclass
class Summary:
    converted: int = 0
    failed: int = 0
    skipped: int = 0
    queries: int = 0
    charged: dict[str, int] = field(default_factory=lambda: dict.fromkeys(EVENTS, 0))

    def add(self, item: Mapping[str, Any]) -> None:
        status = item["status"]
        if status == "converted":
            self.converted += 1
        elif status == "skipped":
            self.skipped += 1
        else:
            self.failed += 1
        if item.get("query") is not None:
            self.queries += 1
        for event in item["charged"]:
            self.charged[event] += 1

    @property
    def message(self) -> str:
        text = f"Converted {self.converted} file(s)"
        if self.queries:
            text += f", ran {self.queries} quer{'y' if self.queries == 1 else 'ies'}"
        if self.failed:
            text += f"; {self.failed} failed (not charged)"
        if self.skipped:
            text += f"; {self.skipped} skipped: the run's maximum charge was reached (not charged)"
        return text + "."


def pricing_problem(prices: Mapping[str, Decimal]) -> str | None:
    """Why a pay-per-event run must not start with these prices (event name -> flat USD price,
    as the SDK reads them from the Actor's monetization settings); None when they are sound.

    Each one would charge for nothing or deliver unpaid work, so the run is refused before any
    input is read, and the owner's first test run shows the message:

    * An event of ours missing (misspelt, or priced per discount tier, which apify 4.2.0 does
      not charge): the SDK charges an unlisted event at $0 and goes on, so every output would
      be delivered unpaid.
    * The platform's per-item event (``apify-default-dataset-item``) with a price: every
      dataset item would be charged, the item reporting a failure or a skipped input included,
      and the SDK pushes one item past the run's maximum charge (seen in a local
      ``ACTOR_TEST_PAY_PER_EVENT`` run with ``ACTOR_MAX_TOTAL_CHARGE_USD=4``: $5 charged).
    """
    missing = [event for event in EVENTS if event not in prices]
    if missing:
        return (
            f"This Actor's pricing has no flat-priced event named {', '.join(missing)}, so "
            "nothing was converted and you were not charged. The Actor's owner must add it in "
            "the Actor's monetization settings."
        )
    if prices.get(DATASET_ITEM_EVENT):
        return (
            f"This Actor's pricing charges for every dataset item ({DATASET_ITEM_EVENT}), which "
            "would charge for failed inputs, so nothing was converted and you were not charged. "
            "The Actor's owner must remove that event in the Actor's monetization settings."
        )
    return None


def build_service(root: Path, *, isolate: bool = True) -> Service:
    """The package's ``Service`` in remote mode, with its files under ``root``.

    The package's own limits apply (``Settings`` defaults: 50 MB per input, the 700 MB job cap,
    120 s per conversion, 15 s per query, 1,000 rows). ``.env`` files are not read, and the
    settings that decide what may be fetched or run are fixed here, whatever the environment.
    One job at a time: a run converts its inputs in order, so its memory is one job's.
    """
    settings = Settings(
        _env_file=None,  # pydantic-settings: no .env file, whatever the working directory
        payments_enabled=False,
        allow_private_urls=False,
        isolate_jobs=isolate,
        max_concurrent_jobs=1,
        artifact_dir=root / "artifacts",
        work_dir=root / "work",
        log_json=False,
        hosted_url=None,
    )
    store = LocalArtifactStore(
        settings.artifact_dir,
        ttl_s=settings.artifact_ttl_s,
        max_total_bytes=settings.artifact_max_total_bytes,
        secret=settings.signing_secret.get_secret_value().encode(),
    )
    return Service(settings, store, local_mode=False)


def parse_input(raw: object, *, max_query_rows: int) -> Plan:
    """Read the Actor input. Raises ``InvalidRequest`` when there is nothing to convert.

    Only the documented fields are read, and any other field is refused, as the hosted MCP
    server refuses an argument a tool does not declare: a `path` (or any other way to name a
    file on the machine running the Actor) is never an input. The input schema says the same
    (`additionalProperties: false`); this holds where the schema is not applied (`apify run`).
    Problems with `options` or `sql` fail every input, unpaid, before anything is fetched.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise InvalidRequest(
            "The input must be a JSON object.",
            hint='Pass {"urls": ["https://.../data.csv"]}; you were not charged.',
        )
    unknown = sorted(set(raw) - INPUT_FIELDS)
    if unknown:
        raise InvalidRequest(
            f"Unknown input field(s): {', '.join(unknown)}.",
            hint="Use urls (public http/https links) or files (fileName and contentBase64); "
            "files on the machine running the Actor cannot be read. You were not charged.",
        )
    jobs = _jobs(raw.get("urls"), raw.get("files"))
    plan = Plan(jobs=jobs, options=ConvertOptions())
    try:
        plan.options = ConvertOptions.model_validate(raw.get("options") or {})
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "options"
        plan.refusal = InvalidRequest(
            f"options.{where}: {first['msg']}",
            hint="Fix or remove that option (see the input's options field); you were not charged.",
        )
        return plan
    sql = raw.get("sql")
    if sql is not None and not isinstance(sql, str):
        plan.refusal = InvalidRequest(
            "sql must be text.", hint="Pass one SELECT statement over `data`, or leave it empty."
        )
        return plan
    plan.sql = (sql.strip() or None) if sql else None
    limit = raw.get("queryLimit", 100)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= max_query_rows:
        plan.refusal = InvalidRequest(
            f"queryLimit must be a whole number from 1 to {max_query_rows}.",
            hint="Leave it out for 100 rows; you were not charged.",
        )
        return plan
    plan.query_limit = limit
    if plan.sql is not None and len(plan.sql) <= PRECHECK_SQL_CHARS:
        # The service's own rule, before anything is fetched: one read-only SELECT. Longer SQL is
        # parsed only where the query runs, in the job's child with a deadline, as the hosted
        # service does (DuckDB's parser is cubic on some inputs; see PRECHECK_SQL_CHARS).
        try:
            validate_select(plan.sql)
        except AimartError as exc:
            plan.refusal = exc
    return plan


def _jobs(urls: object, files: object) -> list[Job]:
    jobs: list[Job] = []
    if urls is not None:
        if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
            raise InvalidRequest(
                "urls must be a list of links.",
                hint='Pass e.g. ["https://example.com/data.csv"]; you were not charged.',
            )
        if len(urls) > MAX_URLS:
            raise InvalidRequest(
                f"{len(urls)} urls; the limit is {MAX_URLS} per run.",
                hint="Split them over several runs; you were not charged.",
            )
        jobs += [Job(index=0, url=u) for u in urls if u.strip()]
    if files is not None:
        if not isinstance(files, list):
            raise InvalidRequest(
                "files must be a list of {fileName, contentBase64} objects.",
                hint="Or pass the file as a public link in urls; you were not charged.",
            )
        if len(files) > MAX_FILES:
            raise InvalidRequest(
                f"{len(files)} files; the limit is {MAX_FILES} per run.",
                hint="Split them over several runs; you were not charged.",
            )
        for entry in files:
            ok = (
                isinstance(entry, dict)
                and set(entry) <= FILE_FIELDS
                and isinstance(entry.get("contentBase64"), str)
                and isinstance(entry.get("fileName", ""), str)
            )
            if not ok:
                raise InvalidRequest(
                    "Each entry of files needs contentBase64 (text) and may have fileName.",
                    hint='Pass e.g. {"fileName": "data.csv", "contentBase64": "b3JkZXI..."}; '
                    "you were not charged.",
                )
            assert isinstance(entry, dict)  # for the type checker; ``ok`` checked it
            jobs.append(
                Job(index=0, content_base64=entry["contentBase64"], filename=entry.get("fileName"))
            )
    if not jobs:
        raise InvalidRequest(
            "Nothing to convert.",
            hint="Give at least one public link in urls, or one entry in files; "
            "you were not charged.",
        )
    return [
        Job(index=i, url=j.url, content_base64=j.content_base64, filename=j.filename)
        for i, j in enumerate(jobs, start=1)
    ]


def record_key(index: int, filename: str) -> str:
    """The key-value store key of an input's Parquet: unique per run, in the allowed alphabet."""
    stem = _KEY_UNSAFE.sub("_", filename.removesuffix(".parquet")).strip("._")[:200] or "data"
    return f"parquet-{index:03d}-{stem}.parquet"


def _error(exc: AimartError) -> dict[str, Any]:
    body: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.hint:
        body["hint"] = exc.hint
    if exc.details:
        body["details"] = exc.details
    return body


def _unexpected(exc: Exception, job: Job, step: str) -> AimartError:
    """The package's typed 500 for an exception that is not an ``AimartError``, logged by type
    and frames only (its text can quote the caller's data)."""
    return internal_error(exc, input_index=job.index, step=step)


def limit_reached(event: str) -> dict[str, Any]:
    return {
        "code": "charge_limit_reached",
        "message": f"The run's maximum charge does not cover another {event!r} event.",
        "hint": "Raise the maximum cost per run and run the remaining inputs again; "
        "you were not charged for this.",
    }


def charge_failed(event: str) -> dict[str, Any]:
    return {
        "code": "charge_failed",
        "message": f"The {event!r} charge could not be recorded, so its result was withheld.",
        "hint": "Run this input again.",
    }


def _skipped(job: Job, event: str = EVENT_CONVERT) -> dict[str, Any]:
    return {
        "index": job.index,
        "input": job.label,
        "status": "skipped",
        "error": limit_reached(event),
        "charged": [],
    }


def _failed(job: Job, error: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": job.index,
        "input": job.label,
        "status": "failed",
        "error": error,
        "charged": [],
    }


def _converted(
    job: Job, result: ConvertResult, key: str, url: str, *, charged: bool
) -> dict[str, Any]:
    report = result.report
    return {
        "index": job.index,
        "input": job.label,
        "status": "converted",
        "fileName": result.artifact.filename,
        "parquetKey": key,
        "parquetUrl": url,
        "sizeBytes": result.artifact.size_bytes,
        "sha256": result.artifact.sha256,
        "sourceFormat": report.source_format.value,
        "rows": report.rows,
        "columns": [{"name": c.name, "type": c.type} for c in report.columns],
        "warnings": report.warnings,
        "report": report.model_dump(mode="json", exclude_none=True),
        "charged": [EVENT_CONVERT] if charged else [],
    }


async def process(
    job: Job, plan: Plan, service: Service, platform: Platform
) -> tuple[dict[str, Any], bool]:
    """Convert one input (and query it); return its dataset item and whether to stop the run.

    The run stops only when its maximum charge cannot cover the next event.
    """
    if plan.refusal is not None:
        return _failed(job, _error(plan.refusal)), False
    if not platform.affordable(EVENT_CONVERT):
        return _skipped(job), True
    src = SourceRef(
        url=job.url, content_base64=job.content_base64, filename=job.filename, takes=TAKES
    )
    artifact_id: str | None = None
    try:
        try:
            service.check_convert(src, plan.options)
            result = await service.convert(src, plan.options)
            artifact_id = result.artifact.id
            _, path = service.store.get(artifact_id)
            key = record_key(job.index, result.artifact.filename)
            url = await platform.save(key, path)
        except AimartError as exc:
            return _failed(job, _error(exc)), False
        except Exception as exc:  # noqa: BLE001 - every failure becomes an unpaid item
            return _failed(job, _error(_unexpected(exc, job, "convert"))), False
        try:
            charged = await platform.charge(EVENT_CONVERT, key=f"{key}-convert")
        except Exception as exc:  # noqa: BLE001
            _log_charge_error(exc, job)
            await _withdraw(platform, key, job)
            return _failed(job, charge_failed(EVENT_CONVERT)), False
        if charged is False:
            await _withdraw(platform, key, job)
            return _skipped(job), True
        item = _converted(job, result, key, url, charged=charged is True)
        stop = False
        if plan.sql is not None:
            stop = await _query(job, plan, service, platform, artifact_id, key, item)
        return item, stop
    finally:
        if artifact_id is not None:
            service.store.delete(artifact_id)


async def _query(
    job: Job,
    plan: Plan,
    service: Service,
    platform: Platform,
    artifact_id: str,
    key: str,
    item: dict[str, Any],
) -> bool:
    """Run ``plan.sql`` over the converted file; fill ``item`` with ``query`` or ``queryError``.

    A query that fails leaves the conversion delivered and charged, and the query unpaid.
    """
    assert plan.sql is not None
    item["sql"] = plan.sql
    if not platform.affordable(EVENT_QUERY):
        item["queryError"] = limit_reached(EVENT_QUERY)
        return True
    try:
        result = await service.query(
            {"data": SourceRef(artifact_id=artifact_id)},
            plan.sql,
            limit=plan.query_limit,
            fmt="json",
        )
        rows = result.model_dump(mode="json", exclude_none=True)
        size = len(json.dumps(rows, ensure_ascii=False).encode())
        if size > MAX_QUERY_JSON_BYTES:
            raise InputTooLarge(
                f"The query result is {size // 1024} KiB; a dataset item holds at most "
                f"{MAX_QUERY_JSON_BYTES // (1024 * 1024)} MiB of it.",
                hint="Select fewer columns or rows (LIMIT, or a smaller queryLimit), or "
                "aggregate; the query was not charged.",
            )
    except AimartError as exc:
        item["queryError"] = _error(exc)
        return False
    except Exception as exc:  # noqa: BLE001
        item["queryError"] = _error(_unexpected(exc, job, "query"))
        return False
    try:
        charged = await platform.charge(EVENT_QUERY, key=f"{key}-query")
    except Exception as exc:  # noqa: BLE001
        _log_charge_error(exc, job)
        item["queryError"] = charge_failed(EVENT_QUERY)
        return False
    if charged is False:
        item["queryError"] = limit_reached(EVENT_QUERY)
        return True
    item["query"] = rows
    if charged:
        item["charged"].append(EVENT_QUERY)
    return False


def _log_charge_error(exc: Exception, job: Job) -> None:
    # By type and frames only (internal_error): an API error's text can quote its request.
    internal_error(exc, input_index=job.index, step="charge")


async def _withdraw(platform: Platform, key: str, job: Job) -> None:
    """Delete a stored Parquet whose charge did not go through. If even that fails, say so
    loudly: the file stays reachable by its link until the run's storage expires."""
    try:
        await platform.discard(key)
    except Exception as exc:  # noqa: BLE001
        internal_error(exc, input_index=job.index, step="withdraw")
        log.error("an unpaid output could not be deleted", extra={"input_index": job.index})


async def run(raw: object, service: Service, platform: Platform) -> Summary:
    """Process every input in order, one dataset item each. Raises ``InvalidRequest`` (nothing
    fetched, nothing charged) when the input names nothing to convert or is malformed."""
    plan = parse_input(raw, max_query_rows=service.settings.max_query_rows)
    summary = Summary()
    stop = False
    for job in plan.jobs:
        if stop:
            item = _skipped(job)
        else:
            item, stop = await process(job, plan, service, platform)
        await platform.push(item)
        summary.add(item)
    return summary
