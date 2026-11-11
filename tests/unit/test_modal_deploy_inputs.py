"""deploy/modal-deploy.sh checks its inputs before it builds anything or calls Modal.

The first Modal deploy stopped with "Token validation failed", Modal's answer to any wrong token
pair, which does not say what is wrong; its wallet address ended with CR LF (see
``test_deploy_input_whitespace.py``). The script now trims every input, says what is wrong with
a token pair (swapped, a whole command pasted, a proxy token) without printing either value, and
``--check`` runs only these checks.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODAL_DEPLOY = ROOT / "deploy" / "modal-deploy.sh"
DEPLOY_README = ROOT / "deploy" / "README.md"

ADDRESS = "0x" + "aB" * 20
URL = "https://someone--aimart-parquet.modal.run"
TOKEN_ID = "ak-" + "A1b2C3" * 4
TOKEN_SECRET = "as-" + "Z9y8X7" * 4

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def check(**env: str) -> subprocess.CompletedProcess[str]:
    """Run the deploy script's input checks only; nothing is built and Modal is not called."""
    base = {
        "PATH": os.environ["PATH"],
        "AIMART_PUBLIC_BASE_URL": URL,
        "AIMART_PAY_TO_EVM": ADDRESS,
        "MODAL_TOKEN_ID": TOKEN_ID,
        "MODAL_TOKEN_SECRET": TOKEN_SECRET,
    }
    merged = {k: v for k, v in {**base, **env}.items() if v != "<unset>"}
    return subprocess.run(
        ["bash", str(MODAL_DEPLOY), "--check"],
        env=merged,
        capture_output=True,
        text=True,
        check=False,
    )


def said(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


@needs_bash
def test_the_check_passes_clean_inputs_and_prints_no_token() -> None:
    result = check()
    assert result.returncode == 0, said(result)
    assert f"payee {ADDRESS}," in result.stdout
    assert TOKEN_ID not in said(result)
    assert TOKEN_SECRET not in said(result)


@needs_bash
def test_the_check_trims_what_was_pasted_with_line_breaks() -> None:
    """The first deploy's inputs: the address ended with CR LF."""
    result = check(
        AIMART_PAY_TO_EVM=ADDRESS + "\r\n",
        AIMART_PUBLIC_BASE_URL=URL + "/\n",
        MODAL_TOKEN_ID=TOKEN_ID + "\n",
        MODAL_TOKEN_SECRET=" " + TOKEN_SECRET + "\r\n",
    )
    assert result.returncode == 0, said(result)
    assert "\r" not in result.stdout
    assert f"payee {ADDRESS}," in result.stdout
    assert f"URL {URL}," in result.stdout


@needs_bash
@pytest.mark.parametrize(
    ("env", "says"),
    [
        ({"MODAL_TOKEN_ID": TOKEN_SECRET, "MODAL_TOKEN_SECRET": TOKEN_ID}, "swapped"),
        (
            {"MODAL_TOKEN_ID": f"modal token set --token-id {TOKEN_ID} --token-secret x"},
            "only the piece that starts with ak-",
        ),
        ({"MODAL_TOKEN_SECRET": f"--token-secret {TOKEN_SECRET}"}, "starts with as-"),
        ({"MODAL_TOKEN_ID": "wk-" + "Q" * 20}, "Proxy Auth Token"),
        ({"MODAL_TOKEN_SECRET": "ws-" + "Q" * 20}, "Proxy Auth Token"),
        ({"MODAL_TOKEN_ID": "A1b2C3" * 4}, "must start with ak-"),
        ({"MODAL_TOKEN_SECRET": "<unset>"}, "both MODAL_TOKEN_ID"),
        ({"MODAL_TOKEN_ID": "\n"}, "both MODAL_TOKEN_ID"),
    ],
)
def test_the_check_says_what_is_wrong_with_a_token_pair(env: dict[str, str], says: str) -> None:
    result = check(**env)
    assert result.returncode == 2, said(result)
    assert says in result.stderr
    assert TOKEN_ID not in said(result)  # never echo a credential, even a wrong one
    assert TOKEN_SECRET not in said(result)


@needs_bash
def test_without_tokens_ci_fails_and_a_local_profile_is_trusted() -> None:
    none = {"MODAL_TOKEN_ID": "<unset>", "MODAL_TOKEN_SECRET": "<unset>"}
    in_ci = check(**none, GITHUB_ACTIONS="true")
    assert in_ci.returncode == 2
    assert "MODAL_TOKEN_ID" in in_ci.stderr
    assert "production environment" in in_ci.stderr
    local = check(**none)  # `modal token new` wrote ~/.modal.toml: nothing to check here
    assert local.returncode == 0, said(local)


@needs_bash
@pytest.mark.parametrize(
    ("env", "says"),
    [
        ({"AIMART_PAY_TO_EVM": ADDRESS[:-1]}, "it has 41"),
        ({"AIMART_PAY_TO_EVM": "<unset>"}, "the address that is paid"),
        ({"AIMART_PUBLIC_BASE_URL": "<unset>"}, "AIMART_PUBLIC_BASE_URL"),
        ({"AIMART_PUBLIC_BASE_URL": "someone--aimart-parquet.modal.run"}, "https://"),
    ],
)
def test_the_check_refuses_a_wrong_address_or_url(env: dict[str, str], says: str) -> None:
    result = check(**env)
    assert result.returncode == 2, said(result)
    assert says in result.stderr


def test_the_readme_says_which_piece_goes_in_which_secret() -> None:
    readme = " ".join(DEPLOY_README.read_text(encoding="utf-8").split())
    assert "`MODAL_TOKEN_ID` (it starts with `ak-`)" in readme
    assert "`MODAL_TOKEN_SECRET` (it starts with `as-`)" in readme
    assert "deploy/modal-deploy.sh --check" in readme
