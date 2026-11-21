"""The CDP key's way from GitHub to the container, and the deploy's checks on it.

Two production secrets (``AIMART_CDP_API_KEY_ID``, ``AIMART_CDP_API_KEY_SECRET``) put CDP's
facilitator first for Base. Each hop has lost a value before: release 0.1.0 called the deploy
without the Modal token (a called workflow gets no environment secret its caller does not
name), and the first deploy's address ended with CR LF. A key that never arrives costs no sale,
only the listing it was set up for, with nothing failing: so every hop is pinned here, and
the deploy says what is wrong with a pasted key without printing it.
"""

from __future__ import annotations

import ast
import base64
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "deploy" / "modal_app.py"
SCRIPT = ROOT / "deploy" / "modal-deploy.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-modal.yml"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
README = ROOT / "deploy" / "README.md"
NAMES = ("AIMART_CDP_API_KEY_ID", "AIMART_CDP_API_KEY_SECRET")

KEY_ID = "4f2a9c1e-7b3d-4e8a-9f60-1c2d3e4f5a6b"
ADDRESS = "0x" + "aB" * 20
URL = "https://someone--aimart-parquet.modal.run"

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def ed25519_secret() -> str:
    key = ed25519.Ed25519PrivateKey.generate()
    raw = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    ) + key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def ecdsa_secret() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


def constants() -> dict[str, object]:
    values: dict[str, object] = {}
    for node in ast.parse(APP.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return values


def workflow() -> dict[str, object]:
    loaded: dict[str, object] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return loaded


# ------------------------------------------------------------------------------ plumbing
def test_the_release_passes_the_cdp_key_to_the_called_deploy_by_name() -> None:
    release = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))["jobs"]["deploy"]
    assert release["uses"] == "./.github/workflows/deploy-modal.yml"
    for name in NAMES:
        assert release["secrets"][name] == "${{ secrets." + name + " }}"
    assert "inherit" not in str(release["secrets"])  # by name: the job passes nothing else


def test_the_deploy_workflow_declares_the_cdp_key_and_hands_it_to_the_script() -> None:
    loaded = workflow()
    triggers = loaded[True] if True in loaded else loaded["on"]  # type: ignore[index]
    declared = triggers["workflow_call"]["secrets"]  # type: ignore[index]
    for name in NAMES:
        assert declared[name] == {"required": False}  # optional: no key, nothing changes
    job = loaded["jobs"]["deploy"]  # type: ignore[index]
    assert job["environment"]["name"] == "production"  # where the values live
    [deploy] = [s for s in job["steps"] if s.get("id") == "deploy"]
    assert deploy["run"] == "deploy/modal-deploy.sh"
    for name in NAMES:
        # A secret, never a variable (variables are printed in every run's log).
        assert deploy["env"][name] == "${{ secrets." + name + " }}"
        assert "vars." + name not in WORKFLOW.read_text(encoding="utf-8")
    # Only the deploy step gets the key: not the smoke test, not the log dump.
    for step in job["steps"]:
        if step is not deploy:
            assert not set(NAMES) & set(step.get("env", {})), step.get("name")


def test_the_app_passes_the_cdp_key_as_a_secret_apart_from_the_configuration() -> None:
    values = constants()
    assert values["SECRET_KEYS"] == NAMES
    assert not set(NAMES) & set(values["CONFIG_KEYS"])  # type: ignore[arg-type]
    assert not set(NAMES) & set(values["RUNTIME_ENV"])  # type: ignore[arg-type]
    source = APP.read_text(encoding="utf-8")
    assert "modal.Secret.from_dict(_secrets())" in source
    # Both functions get it: the web app and the selftest share `secrets`.
    assert source.count("secrets=secrets") == 2
    # The settings the container reads: AIMART_CDP_API_KEY_ID -> cdp_api_key_id.
    from aimart_parquet.config import Settings

    for name in NAMES:
        assert name.removeprefix("AIMART_").lower() in Settings.model_fields


def test_the_selftest_checks_the_cdp_key_and_its_failure_fails_the_deploy() -> None:
    source = APP.read_text(encoding="utf-8")
    selftest = source[source.index("async def selftest") :]
    assert '"cdp": await cdp.check_key(settings)' in selftest
    # Any exception in the selftest crosses to the deployer as one line, and fails the run.
    assert (
        'raise RuntimeError(f"selftest failed: {type(exc).__name__}: {exc}") from None' in selftest
    )
    script = SCRIPT.read_text(encoding="utf-8")
    assert script.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in script
    assert '"selftest").remote()' in script


def test_the_readme_walks_the_owner_through_the_key() -> None:
    text = README.read_text(encoding="utf-8")
    section = text[text.index("### List the service in the CDP Bazaar") :]
    section = section[: section.index("\n### ", 5)]
    for needle in (
        "portal.cdp.coinbase.com",
        "Settings → Environments → production",
        *NAMES,
        "Deploy to Modal",
        "Make one paid call",
        "/platform/v2/x402/discovery/merchant?payTo=",
        "agentic.market",
        "1,000",
        "$0.001",
    ):
        assert needle in section, needle


# ------------------------------------------------------------------------------ the checks
def check(**env: str) -> subprocess.CompletedProcess[str]:
    base = {
        "PATH": os.environ["PATH"],
        "AIMART_PUBLIC_BASE_URL": URL,
        "AIMART_PAY_TO_EVM": ADDRESS,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), "--check"],
        env={**base, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def said(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


@needs_bash
@pytest.mark.parametrize(("make", "kind"), [(ed25519_secret, "Ed25519"), (ecdsa_secret, "ECDSA")])
@pytest.mark.parametrize("mode", ["mainnet", "testnet"])
def test_a_well_shaped_key_passes_and_is_never_printed(make: object, kind: str, mode: str) -> None:
    secret = make()  # type: ignore[operator]
    # Pasted with a line break, as the first deploy's address was: trimmed, then checked.
    result = check(
        AIMART_CDP_API_KEY_ID=f"{KEY_ID}\r\n",
        AIMART_CDP_API_KEY_SECRET=f" {secret}\n",
        AIMART_NETWORK_MODE=mode,
    )
    assert result.returncode == 0, said(result)
    assert f"CDP key ({kind}) has the right shape" in result.stdout
    if mode == "mainnet":
        assert "CDP settles Base first" in result.stdout
    else:
        assert "unused until AIMART_NETWORK_MODE is mainnet" in result.stdout
    for line in secret.splitlines():
        if "-----" not in line:
            assert line not in said(result)
    assert KEY_ID not in said(result)


@needs_bash
def test_no_key_is_fine_and_says_who_settles() -> None:
    result = check()
    assert result.returncode == 0, said(result)
    assert "no CDP key: Base settles through xpay, then Primer" in result.stdout


@needs_bash
@pytest.mark.parametrize(
    ("env", "says"),
    [
        ({"AIMART_CDP_API_KEY_ID": KEY_ID}, "set both"),
        ({"AIMART_CDP_API_KEY_SECRET": "<ed25519>"}, "set both"),
        (
            {"AIMART_CDP_API_KEY_ID": "<ed25519>", "AIMART_CDP_API_KEY_SECRET": KEY_ID},
            "look swapped",
        ),
        (
            {"AIMART_CDP_API_KEY_ID": "my-key", "AIMART_CDP_API_KEY_SECRET": "<ed25519>"},
            "does not look like a CDP API key ID",
        ),
        (
            {"AIMART_CDP_API_KEY_ID": KEY_ID, "AIMART_CDP_API_KEY_SECRET": '{"id": "x"}'},
            "whole downloaded key file",
        ),
        (
            {"AIMART_CDP_API_KEY_ID": KEY_ID, "AIMART_CDP_API_KEY_SECRET": "<ed25519-cut>"},
            "does not look like a CDP secret",
        ),
    ],
)
def test_a_wrong_key_stops_the_deploy_saying_what_is_wrong_and_not_what_it_is(
    env: dict[str, str], says: str
) -> None:
    secret = ed25519_secret()
    filled = {
        k: v.replace("<ed25519-cut>", secret[:60]).replace("<ed25519>", secret)
        for k, v in env.items()
    }
    result = check(**filled)
    assert result.returncode == 2, said(result)
    assert says in result.stderr
    assert secret[:40] not in said(result)
    assert KEY_ID not in said(result)
