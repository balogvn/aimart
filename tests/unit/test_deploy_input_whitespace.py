"""Deploy inputs pasted with a stray line break, or a wrong token, must fail early or not at all.

The first Modal deploy's ``AIMART_PAY_TO_EVM`` was pasted from a phone message and ended with
``\\r\\n``, and its Modal token was rejected with "Token validation failed", which does not say
what is wrong. A value ending in a bare ``\\n`` was worse: Python's ``$`` also matches before a
final newline, so ``Settings`` accepted the address and the service would have named a payee with
a line break in it, which nobody can pay; ``smoke-flags.sh`` printed the break as a blank flag.

Now every layer drops whitespace around a pasted value: ``Settings`` and ``smoke-flags.sh``
here, ``modal-deploy.sh`` in ``test_modal_deploy_inputs.py``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from aimart_parquet.config import NetworkMode, Settings

ROOT = Path(__file__).resolve().parents[2]
SMOKE_FLAGS = ROOT / "deploy" / "smoke-flags.sh"

ADDRESS = "0x" + "aB" * 20
URL = "https://someone--aimart-parquet.modal.run"

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


# ------------------------------------------------------------------ the service's own settings
@pytest.mark.parametrize(
    "pasted", [ADDRESS + "\r\n", ADDRESS + "\n", f"  {ADDRESS} ", "\t" + ADDRESS + "\n\n"]
)
def test_settings_drop_whitespace_around_a_pasted_address(pasted: str) -> None:
    # Before: "\n" was kept (the payee had a line break in it); the others refused to start.
    settings = Settings(_env_file=None, pay_to_evm=pasted)  # type: ignore[call-arg]
    assert settings.pay_to_evm == ADDRESS


def test_settings_drop_whitespace_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [key for key in os.environ if key.startswith("AIMART_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AIMART_PAY_TO_EVM", ADDRESS + "\r\n")
    monkeypatch.setenv("AIMART_PUBLIC_BASE_URL", URL + "/ \n")
    monkeypatch.setenv("AIMART_NETWORK_MODE", "mainnet\n")
    monkeypatch.setenv("AIMART_PRICE_CONVERT", "$0.05\n")
    monkeypatch.setenv("AIMART_JOB_MEMORY_LIMIT_MB", "640\r\n")
    monkeypatch.setenv("AIMART_ENABLE_QUANTUM", "true \n")
    monkeypatch.setenv("AIMART_SIGNING_SECRET", "hmac-key\n")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.pay_to_evm == ADDRESS
    assert settings.public_base_url == URL
    assert settings.network_mode is NetworkMode.MAINNET
    assert settings.price_convert == "$0.05"
    assert settings.job_memory_limit_mb == 640
    assert settings.enable_quantum is True
    assert settings.signing_secret.get_secret_value() == "hmac-key"


@pytest.mark.parametrize("bad", [ADDRESS + "\nx", ADDRESS[:-1], ADDRESS + "0", "0x" + "zz" * 20])
def test_settings_still_refuse_what_is_not_an_address(bad: str) -> None:
    with pytest.raises(ValueError, match="AIMART_PAY_TO_EVM must be"):
        Settings(_env_file=None, pay_to_evm=bad)  # type: ignore[call-arg]


# ------------------------------------------------------------------ deploy/smoke-flags.sh
@needs_bash
def test_smoke_flags_ignore_whitespace_around_pasted_values() -> None:
    out = subprocess.run(
        ["bash", str(SMOKE_FLAGS)],
        env={
            "PATH": os.environ["PATH"],
            "AIMART_PAY_TO_EVM": ADDRESS + "\r\n",
            "AIMART_PUBLIC_BASE_URL": URL + "/\n",
            "AIMART_NETWORK_MODE": "testnet\r\n",
            "AIMART_ENABLE_QUANTUM": "true\n",
            "SMOKE_SIGNING_KEY_SHA256": "ab" * 32 + " \n",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    # The workflows read one flag per line, exactly (`IFS= read -r`): a blank or a "\r" is a flag.
    flags = out.stdout.split("\n")[:-1]
    assert all(flag and flag == flag.strip() for flag in flags), flags
    assert flags[flags.index("--expect-pay-to") + 1] == ADDRESS
    assert flags[flags.index("--expect-base-url") + 1] == URL
    assert flags[flags.index("--expect-signing-key-sha256") + 1] == "ab" * 32
    assert flags.count("--require-operation") == 2
