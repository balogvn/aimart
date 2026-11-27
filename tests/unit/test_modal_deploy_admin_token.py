"""The admin token's way from GitHub to the container on Modal: the owner's demand report.

``GET /v1/demand`` shows what agents tried to buy and what they asked for in their own words
(``demand.py``). It is off unless ``AIMART_ADMIN_TOKEN`` is set, and the token only reached
Cloud Run (through Secret Manager): on Modal, where the service runs, the report could not be
switched on at all. It now travels as the CDP key does: a production-environment secret, passed
by name by the release, handed only to the deploy step, put in the container as a Modal secret
apart from the configuration, and checked for shape without ever being printed.
"""

from __future__ import annotations

import os
import subprocess

import pytest
import yaml
from tests.unit.test_modal_deploy_cdp import (
    ADDRESS,
    APP,
    README,
    RELEASE,
    SCRIPT,
    URL,
    WORKFLOW,
    constants,
    needs_bash,
    workflow,
)

NAME = "AIMART_ADMIN_TOKEN"
TOKEN = "f3" * 32  # 64 characters, as `openssl rand -hex 32` makes them


def test_the_release_passes_the_admin_token_to_the_called_deploy_by_name() -> None:
    release = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))["jobs"]["deploy"]
    assert release["secrets"][NAME] == "${{ secrets." + NAME + " }}"


def test_the_deploy_workflow_declares_the_admin_token_and_only_the_deploy_step_gets_it() -> None:
    loaded = workflow()
    triggers = loaded[True] if True in loaded else loaded["on"]  # type: ignore[index]
    assert triggers["workflow_call"]["secrets"][NAME] == {"required": False}  # type: ignore[index]
    job = loaded["jobs"]["deploy"]  # type: ignore[index]
    [deploy] = [s for s in job["steps"] if s.get("id") == "deploy"]
    assert deploy["env"][NAME] == "${{ secrets." + NAME + " }}"
    assert "vars." + NAME not in WORKFLOW.read_text(encoding="utf-8")
    for step in job["steps"]:
        if step is not deploy:
            assert NAME not in step.get("env", {}), step.get("name")


def test_the_app_passes_the_admin_token_as_a_secret_the_settings_read() -> None:
    values = constants()
    assert NAME in values["SECRET_KEYS"]  # type: ignore[operator]
    assert NAME not in values["CONFIG_KEYS"]  # type: ignore[operator]
    assert NAME not in values["RUNTIME_ENV"]  # type: ignore[operator]
    assert "modal.Secret.from_dict(_secrets())" in APP.read_text(encoding="utf-8")
    from aimart_parquet.config import Settings

    assert "admin_token" in Settings.model_fields  # AIMART_ADMIN_TOKEN -> admin_token


def test_the_readme_has_the_owner_read_the_report_without_seeing_the_token() -> None:
    text = README.read_text(encoding="utf-8")
    section = text[text.index("### Demand report") :]
    section = section[: section.index("\n## ")]
    # Made, kept and used through the Keychain: never typed, printed or put in a variable list.
    assert "openssl rand -hex 32" in section
    assert "security add-generic-password" in section
    assert "security find-generic-password -s aimart-admin-token -w" in section
    assert "pbcopy < /dev/null" in section
    assert "admin token set: GET /v1/demand is on" in section
    # The report holds agents' own words: it is never sent to a public log.
    assert "never in a GitHub Actions log" in section


# ------------------------------------------------------------------------------ the checks
def check(**env: str) -> subprocess.CompletedProcess[str]:
    base = {"PATH": os.environ["PATH"], "AIMART_PUBLIC_BASE_URL": URL, "AIMART_PAY_TO_EVM": ADDRESS}
    return subprocess.run(
        ["bash", str(SCRIPT), "--check"],
        env={**base, **env},
        capture_output=True,
        text=True,
        check=False,
    )


@needs_bash
def test_a_token_pasted_with_a_line_break_passes_and_is_never_printed() -> None:
    result = check(**{NAME: f" {TOKEN}\r\n"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "admin token set: GET /v1/demand is on" in result.stdout
    assert TOKEN not in result.stdout + result.stderr
    assert TOKEN[:16] not in result.stdout + result.stderr


@needs_bash
def test_no_token_leaves_the_report_off_and_says_so() -> None:
    result = check()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no admin token: GET /v1/demand is off" in result.stdout


@needs_bash
@pytest.mark.parametrize(
    ("value", "says"),
    [
        ("short-token", "too short to keep the demand report private"),
        (TOKEN[:20] + " " + TOKEN[20:], "holds a space, a line break or a quote"),
        ('"' + TOKEN + '"', "holds a space, a line break or a quote"),
    ],
    ids=["short", "inner-space", "quoted"],
)
def test_a_wrong_token_stops_the_deploy_saying_what_is_wrong_and_not_what_it_is(
    value: str, says: str
) -> None:
    result = check(**{NAME: value})
    assert result.returncode != 0
    assert says in result.stdout + result.stderr
    assert value.strip('"') not in result.stdout + result.stderr
    assert f"it has {len(value)}" in result.stdout + result.stderr
