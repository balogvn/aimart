"""Cloud Run's request timeout must outlast the longest paid call the settings allow.

``deploy.yml`` deployed with ``--timeout=120``, but a paid call may wait for a job slot
(``Settings.queue_wait_for``), download its inputs (one ``fetch_timeout_s``) and run its job
(the operation's timeout), with ``SETTLE_MARGIN_S`` to verify and settle: up to the 300 s
payment window. Past 120 s the front end answered 504 while the call ran on, and the x402
middleware settles whatever finishes below 400: a caller could be charged for a response it
never received. The timeout is the payment window now, and Cloud Run allows at most 3600 s.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from aimart_parquet.config import PAYMENT_WINDOW_S, SETTLE_MARGIN_S, Settings

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / ".github" / "workflows" / "deploy.yml"
CLOUD_RUN_MAX_TIMEOUT_S = 3600


def _deploy_timeout() -> int:
    workflow = yaml.safe_load(DEPLOY.read_text(encoding="utf-8"))
    flags = [
        step["with"]["flags"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "flags" in step.get("with", {})
    ]
    assert len(flags) == 1, flags
    [timeout] = re.findall(r"--timeout=(\d+)\b", flags[0])
    return int(timeout)


def _longest_paid_call_s(settings: Settings) -> float:
    """As ``Payments._check_payment_window`` counts a call: wait, downloads, job, settle."""
    jobs = (
        settings.convert_timeout_s,  # convert, convert_upload
        settings.query_timeout_s,  # inspect, preview
        settings.query_timeout_s + 5,  # query: Service.query's outer deadline
        settings.quantum_timeout_s,  # the quantum tools, when enabled
    )
    return max(
        settings.queue_wait_for(job) + settings.fetch_timeout_s + job + SETTLE_MARGIN_S
        for job in jobs
    )


def test_the_request_timeout_outlasts_every_paid_call() -> None:
    timeout = _deploy_timeout()
    settings = Settings(_env_file=None, enable_quantum=True, payments_enabled=False)  # type: ignore[call-arg]
    longest = _longest_paid_call_s(settings)
    assert longest == PAYMENT_WINDOW_S  # the queue wait is what the window leaves each call
    assert timeout >= longest  # before: 120
    assert timeout <= CLOUD_RUN_MAX_TIMEOUT_S


def test_the_deploy_readme_states_the_timeout_deploy_sets() -> None:
    readme = " ".join((ROOT / "deploy" / "README.md").read_text(encoding="utf-8").split())
    assert f"{_deploy_timeout()} s request timeout" in readme


# Modal's own request limit (150 s, then a redirect) is checked in test_modal_deploy.py.
