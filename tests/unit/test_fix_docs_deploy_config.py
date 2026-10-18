"""The deploy workflows, the smoke expectations and deploy/README.md must describe one deployment.

``deploy.yml`` overwrites the service's environment and secrets on every deploy, so anything it
does not pass does not exist in production, and anything the README does not list cannot be set
by an operator who follows it. The review of 6f99145 found the synthetic check blind to the
quantum switch (its smoke step never passed ``AIMART_ENABLE_QUANTUM``), that variable missing from
the README, and ``GET /v1/demand`` impossible to switch on (no way to pass ``AIMART_ADMIN_TOKEN``).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SMOKE_FLAGS = ROOT / "deploy" / "smoke-flags.sh"
DEPLOY_README = ROOT / "deploy" / "README.md"

needs_bash = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("openssl") is None, reason="needs bash + openssl"
)


def workflow(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return loaded


def steps(name: str) -> list[dict[str, Any]]:
    jobs = workflow(name)["jobs"]
    return [step for job in jobs.values() for step in job.get("steps", [])]


def step_running(name: str, needle: str) -> dict[str, Any]:
    found = [s for s in steps(name) if needle in s.get("run", "")]
    assert len(found) == 1, f"{name}: expected one step running {needle!r}"
    return found[0]


def step_with_id(name: str, step_id: str) -> dict[str, Any]:
    return next(s for s in steps(name) if s.get("id") == step_id)


def variables_read(name: str) -> set[str]:
    return set(re.findall(r"\$\{\{\s*vars\.([A-Z0-9_]+)\s*\}\}", (WORKFLOWS / name).read_text()))


def documented_variables() -> set[str]:
    """Every backticked name in the first column of deploy/README's variables table."""
    text = DEPLOY_README.read_text(encoding="utf-8")
    table = text[text.index("| Variable | Required | Value |") :].split("\n\n", 1)[0]
    names: set[str] = set()
    for row in table.splitlines()[2:]:
        names |= set(re.findall(r"`([A-Z0-9_]+)`", row.split("|")[1]))
    return names


def smoke_flag_inputs() -> set[str]:
    """Environment variables smoke-flags.sh reads (code lines only, not its header)."""
    code = [ln for ln in SMOKE_FLAGS.read_text().splitlines() if not ln.lstrip().startswith("#")]
    reads = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", "\n".join(code)))
    return reads - {"GITHUB_OUTPUT"}


# ------------------------------------------------------------------ D5 / D7: every variable documented
@pytest.mark.parametrize("name", ["deploy.yml", "synthetic.yml"])
def test_every_github_variable_a_workflow_reads_is_documented(name: str) -> None:
    missing = variables_read(name) - documented_variables()
    assert not missing, f"{name} reads vars missing from deploy/README.md: {sorted(missing)}"


def test_readme_says_hand_set_settings_are_overwritten() -> None:
    deploy_step = step_with_id("deploy.yml", "deploy")
    assert deploy_step["with"]["env_vars_update_strategy"] == "overwrite"
    text = " ".join(DEPLOY_README.read_text(encoding="utf-8").split())
    assert "env_vars_update_strategy: overwrite" in text
    assert "gcloud run services update" in text  # names the thing that gets undone


# ------------------------------------------------------------------ D6: both smoke runs, same inputs
def test_smoke_flags_header_lists_every_input() -> None:
    header = SMOKE_FLAGS.read_text().split("set -euo pipefail", 1)[0]
    inputs = header[header.index("# Inputs (environment):") :]
    for name in smoke_flag_inputs():
        assert re.search(rf"^#\s+{name}\s", inputs, re.M), name


@pytest.mark.parametrize("name", ["deploy.yml", "synthetic.yml"])
def test_each_smoke_run_passes_every_smoke_flags_input(name: str) -> None:
    env = step_running(name, "deploy/smoke-flags.sh").get("env", {})
    missing = smoke_flag_inputs() - set(env)
    assert not missing, f"{name} calls smoke-flags.sh without {sorted(missing)}"


def test_both_smoke_runs_read_the_same_variables() -> None:
    def from_vars(name: str) -> dict[str, str]:
        env = step_running(name, "deploy/smoke-flags.sh")["env"]
        return {k: v for k, v in env.items() if k in smoke_flag_inputs() and "vars." in v}

    deploy, synthetic = from_vars("deploy.yml"), from_vars("synthetic.yml")
    # deploy.yml takes the base URL from its config step; everything else is the same variable.
    deploy.pop("AIMART_PUBLIC_BASE_URL", None)
    synthetic.pop("AIMART_PUBLIC_BASE_URL", None)
    assert deploy == synthetic


@needs_bash
def test_smoke_flags_require_the_quantum_tools_when_enabled() -> None:
    base = {"PATH": os.environ["PATH"], "AIMART_PAY_TO_EVM": "0x" + "ab" * 20}

    def flags(**extra: str) -> list[str]:
        out = subprocess.run(
            ["bash", str(SMOKE_FLAGS)],
            env={**base, **extra},
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.split()

    assert "--require-operation" not in flags()
    on = flags(AIMART_ENABLE_QUANTUM="true")
    assert on.count("--require-operation") == 2
    assert "quantum_compress" in on
    assert "quantum_portfolio" in on


# ------------------------------------------------------------------ D7: the admin token secret
def run_config_step(tmp_path: Path, **variables: str) -> dict[str, str]:
    """Run deploy.yml's "Check inputs" script as Actions would, and parse its outputs."""
    script = step_with_id("deploy.yml", "config")["run"]
    output = tmp_path / "github_output"
    output.write_text("")
    env = {
        "PATH": os.environ["PATH"],
        "GITHUB_OUTPUT": str(output),
        "IMAGE_DIGEST": "sha256:" + "0" * 64,
        "EXPECT_VERSION": "",
        "GCP_PROJECT_ID": "proj",
        "GCP_WORKLOAD_IDENTITY_PROVIDER": "projects/1/locations/global/workloadIdentityPools/p/providers/x",
        "GCP_SERVICE_ACCOUNT": "deployer@proj.iam.gserviceaccount.com",
        "GCP_RUNTIME_SERVICE_ACCOUNT": "runtime@proj.iam.gserviceaccount.com",
        "AIMART_PUBLIC_BASE_URL": "https://aimart.example.com",
        "AIMART_PAY_TO_EVM": "0x" + "ab" * 20,
    }
    for name in step_with_id("deploy.yml", "config")["env"]:
        env.setdefault(name, "")  # Actions passes an unset variable as ""
    env.update(variables)
    subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True)
    outputs: dict[str, str] = {}
    lines = iter(output.read_text().splitlines())
    for line in lines:
        if "<<" in line:
            key, delim = line.split("<<", 1)
            body: list[str] = []
            for inner in lines:
                if inner == delim:
                    break
                body.append(inner)
            outputs[key] = "\n".join(body)
        else:
            key, _, value = line.partition("=")
            outputs[key] = value
    return outputs


def test_deploy_step_takes_its_secrets_from_the_config_step() -> None:
    secrets = step_with_id("deploy.yml", "deploy")["with"]["secrets"]
    assert secrets.strip() == "${{ steps.config.outputs.secrets }}"
    assert step_with_id("deploy.yml", "deploy")["with"]["secrets_update_strategy"] == "overwrite"
    env = step_with_id("deploy.yml", "config")["env"]
    assert env["GCP_SECRET_ADMIN_TOKEN"] == "${{ vars.GCP_SECRET_ADMIN_TOKEN }}"


@needs_bash
def test_admin_token_secret_is_mounted_only_when_configured(tmp_path: Path) -> None:
    off = run_config_step(tmp_path)["secrets"].splitlines()
    assert off == [
        "AIMART_SIGNING_KEY_MLDSA=aimart-signing-key-mldsa:latest",
        "AIMART_SIGNING_SECRET=aimart-signing-secret:latest",
    ]
    on = run_config_step(tmp_path, GCP_SECRET_ADMIN_TOKEN="aimart-admin-token")["secrets"]
    assert on.splitlines() == [*off, "AIMART_ADMIN_TOKEN=aimart-admin-token:latest"]


@needs_bash
def test_secret_names_are_validated(tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        run_config_step(tmp_path, GCP_SECRET_ADMIN_TOKEN="bad name,AIMART_X=y")


FAKE_GCLOUD = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GCLOUD_LOG"
case "$*" in
  *"projects describe"*) echo 123456 ;;
  *"secrets versions add"*) cat > /dev/null ;;
esac
"""


def run_setup(tmp_path: Path, **variables: str) -> tuple[str, list[str]]:
    """Run deploy/setup-gcp.sh against a gcloud stand-in that records every call."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD)
    gcloud.chmod(0o755)
    log = tmp_path / "gcloud.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GCLOUD_LOG": str(log),
        "PROJECT_ID": "proj",
        **variables,
    }
    out = subprocess.run(
        ["bash", str(ROOT / "deploy" / "setup-gcp.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout, log.read_text().splitlines()


@needs_bash
def test_setup_script_creates_the_admin_token_secret_only_when_asked(tmp_path: Path) -> None:
    stdout, calls = run_setup(tmp_path)
    assert not [c for c in calls if "admin-token" in c]
    assert "GCP_SECRET_ADMIN_TOKEN" not in stdout

    stdout, calls = run_setup(tmp_path, SECRET_ADMIN_TOKEN="aimart-admin-token")
    admin = [c for c in calls if "aimart-admin-token" in c]
    # Generated and piped on stdin (never in argv), readable by the runtime SA, printed by name.
    assert any("secrets versions add aimart-admin-token --data-file=-" in c for c in admin)
    assert any(
        "secrets add-iam-policy-binding aimart-admin-token" in c
        and "secretAccessor" in c
        and "aimart-runtime@" in c
        for c in admin
    )
    assert "GCP_SECRET_ADMIN_TOKEN=aimart-admin-token" in stdout


@needs_bash
def test_setup_script_rejects_a_bad_admin_token_secret_name(tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        run_setup(tmp_path, SECRET_ADMIN_TOKEN="bad name")


def test_discovery_doc_explains_how_to_switch_on_the_demand_report() -> None:
    text = (ROOT / "docs" / "discovery.md").read_text(encoding="utf-8")
    demand = " ".join(text[text.index("demand signals") :].split("\n### ", 1)[0].split())
    assert "GCP_SECRET_ADMIN_TOKEN" in demand
    assert "demand_signal" in demand
    assert "without" in demand.lower()  # what you get with no token: the log lines
