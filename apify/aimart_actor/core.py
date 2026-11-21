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
* A run can be restarted from the start, with the same input and storages: Apify migrates runs
  between servers "at any time", and a user can resurrect a finished run (docs.apify.com/
  platform/actors/development/builds-and-runs/state-persistence). The SDK forgets an
  idempotency key after three minutes (apify 4.2.0, ``ChargingManager._idempotent_charges``), so
  each input's progress is written to the run's key-value store (``progress-NNN``) before each
  charge and around each push, and a restarted run resumes from it: nothing is charged twice,
  converted twice or reported twice. A charge interrupted by the restart counts as made (the
  conservative side: at worst one result is delivered unpaid, never one charged twice).
* Failures cost the user nothing and the owner compute, so once failing inputs have used
  ``FAILURE_BUDGET_S`` of the run's time in total, the remaining inputs are not started.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
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

# Every input's progress, so that a restarted run resumes instead of charging again. Outside the
# key-value store schema's parquet collection (keyPrefix "parquet-").
PROGRESS_PREFIX = "progress-"
# Seconds of failing work after which the rest of the run's inputs are not started: one input
# that runs into the conversion deadline (120 s). Failures are free for the user and cost the
# owner compute (docs/apify.md, "How it works and what it charges").
FAILURE_BUDGET_S = 120.0

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

    async def recall(self, key: str) -> Any:
        """The JSON record ``key`` of the run's key-value store, or None when there is none."""

    async def remember(self, key: str, value: dict[str, Any]) -> None:
        """Store ``value`` as the JSON record ``key`` of the run's key-value store."""

    async def last_index(self) -> int:
        """The ``index`` of the last item in the run's default dataset; 0 when it is empty."""


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


def brief(item: Mapping[str, Any]) -> dict[str, Any]:
    """What the run's summary needs of a dataset item, kept once the item is reported."""
    error = item.get("error") or {}
    return {
        "status": item["status"],
        "code": error.get("code"),
        "query": item.get("query") is not None,
        "charged": list(item["charged"]),
    }


@dataclass
class Summary:
    converted: int = 0
    failed: int = 0
    skipped: int = 0
    halted: int = 0  # skipped because failures used the run's failure budget
    queries: int = 0
    charged: dict[str, int] = field(default_factory=lambda: dict.fromkeys(EVENTS, 0))

    def add(self, item: Mapping[str, Any]) -> None:
        """Count one item, given as ``brief(item)``."""
        status = item["status"]
        if status == "converted":
            self.converted += 1
        elif status == "skipped" and item["code"] == FAILURE_BUDGET_CODE:
            self.halted += 1
        elif status == "skipped":
            self.skipped += 1
        else:
            self.failed += 1
        if item["query"]:
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
        if self.halted:
            text += (
                f"; {self.halted} skipped: earlier inputs failed after using this run's time "
                "for failures (not charged)"
            )
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


FAILURE_BUDGET_CODE = "failure_budget_reached"


def failure_budget_reached() -> dict[str, Any]:
    return {
        "code": FAILURE_BUDGET_CODE,
        "message": f"Earlier inputs of this run failed after more than {FAILURE_BUDGET_S:.0f} "
        "seconds of work in total, so this input was not started.",
        "hint": "Fix the failed inputs (see their error.hint), then run this one again in a new "
        "run; you were not charged for it.",
    }


def query_interrupted() -> dict[str, Any]:
    return {
        "code": "query_interrupted",
        "message": "The platform restarted the run while this file's query was due, so the "
        "query did not run.",
        "hint": "Run the query again over the file at parquetUrl; you were not charged for it.",
    }


def charge_failed(event: str) -> dict[str, Any]:
    return {
        "code": "charge_failed",
        "message": f"The {event!r} charge could not be recorded, so its result was withheld.",
        "hint": "Run this input again.",
    }


def _skipped(job: Job, error: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "index": job.index,
        "input": job.label,
        "status": "skipped",
        "error": error or limit_reached(EVENT_CONVERT),
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


@dataclass
class Progress:
    """Each input's progress in the run's key-value store, for a restarted run to resume from.

    One record per input (``progress-NNN``), tagged with the run input's fingerprint so that
    records left by another input (a local run without purge) are ignored. States:

    * ``charging``: written just before a charge, with the item as it is if the charge went
      through. A restart finds it when the charge's outcome is unknown, and counts the charge as
      made: the item is delivered, nothing is charged again.
    * ``ready``: the item, final, about to be pushed. A restart pushes it unless the dataset's
      last item shows the push went through.
    * ``pushed``: the item is in the dataset; only its ``brief`` is kept.
    """

    platform: Platform
    fingerprint: str
    _last_index: int | None = None

    @staticmethod
    def key(index: int) -> str:
        return f"{PROGRESS_PREFIX}{index:03d}"

    async def load(self, index: int) -> dict[str, Any] | None:
        record = await self.platform.recall(self.key(index))
        if not isinstance(record, dict) or record.get("input") != self.fingerprint:
            return None
        return record

    async def mark(self, index: int, state: str, **fields: Any) -> None:
        record = {"input": self.fingerprint, "state": state, **fields}
        await self.platform.remember(self.key(index), record)

    async def was_pushed(self, index: int) -> bool:
        """Whether the item of input ``index`` is in the dataset (items are pushed in order)."""
        if self._last_index is None:
            self._last_index = await self.platform.last_index()
        return self._last_index >= index


def fingerprint(raw: object) -> str:
    text = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


async def process(
    job: Job, plan: Plan, service: Service, platform: Platform, progress: Progress
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
        # The item as delivered if the run is restarted before the charge's outcome is known.
        draft = _converted(job, result, key, url, charged=True)
        if plan.sql is not None:
            draft |= {"sql": plan.sql, "queryError": query_interrupted()}
        try:
            await progress.mark(job.index, "charging", item=draft, stop=None)
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
            stop = await _query(job, plan, service, platform, progress, artifact_id, key, item)
        return item, stop
    finally:
        if artifact_id is not None:
            service.store.delete(artifact_id)


async def _query(
    job: Job,
    plan: Plan,
    service: Service,
    platform: Platform,
    progress: Progress,
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
        draft = item | {"query": rows, "charged": [*item["charged"], EVENT_QUERY]}
        await progress.mark(job.index, "charging", item=draft, stop=None)
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
    fetched, nothing charged) when the input names nothing to convert or is malformed.

    A restarted run (same input, same storages) resumes from each input's ``Progress``.
    """
    plan = parse_input(raw, max_query_rows=service.settings.max_query_rows)
    progress = Progress(platform, fingerprint(raw))
    summary = Summary()
    stop: dict[str, Any] | None = None  # why the remaining inputs are skipped
    failing_s = 0.0  # time spent on inputs that failed, this attempt and earlier ones
    for job in plan.jobs:
        record = await progress.load(job.index)
        seconds = 0.0
        if record is not None and record["state"] == "pushed":
            summary.add(record["brief"])
            stop, failing_s = record["stop"], failing_s + record["seconds"]
            continue
        if record is not None:  # an earlier attempt prepared this item: deliver it as it is
            item, stop, seconds = record["item"], record["stop"], record.get("seconds", 0.0)
            if record["state"] == "ready" and await progress.was_pushed(job.index):
                await _reported(progress, job, item, stop, seconds)
                summary.add(brief(item))
                failing_s += seconds
                continue
        else:
            if stop is None and failing_s > FAILURE_BUDGET_S:
                stop = failure_budget_reached()
            if stop is not None:
                item = _skipped(job, stop)
            else:
                started = time.monotonic()
                item, limit = await process(job, plan, service, platform, progress)
                if item["status"] == "failed":
                    seconds = time.monotonic() - started
                if limit:
                    stop = limit_reached(EVENT_CONVERT)
        failing_s += seconds
        await progress.mark(job.index, "ready", item=item, stop=stop, seconds=seconds)
        await platform.push(item)
        await _reported(progress, job, item, stop, seconds)
        summary.add(brief(item))
    return summary


async def _reported(
    progress: Progress, job: Job, item: dict[str, Any], stop: Any, seconds: float
) -> None:
    await progress.mark(job.index, "pushed", brief=brief(item), stop=stop, seconds=seconds)
