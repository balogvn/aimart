"""Suite-wide fixtures.

Defaults every test gets, and why:

* No ambient configuration: every ``AIMART_*`` variable is removed and ``.env`` is never read,
  so a developer's shell cannot change what a test asserts.
* ``isolate_jobs=False`` unless a test opts in: in-process jobs are fast and visible to
  coverage; a handful of tests exercise the forkserver child for real.
* The app factory's logging setup is neutralised: it replaces the root logger's handlers,
  which would detach pytest's log capture from every later test.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # makes `import aimart_testkit` work

import aimart_testkit as kit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from aimart_parquet.config import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("AIMART_"):
            monkeypatch.delenv(key)
    from aimart_parquet.config import get_settings

    get_settings.cache_clear()
    try:
        import aimart_parquet.api.app as app_module
    except ImportError:  # the app layer is mid-edit: engine tests must still run
        return
    monkeypatch.setattr(app_module, "configure_logging", lambda *a, **k: None, raising=False)


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    """Settings for tests; keyword overrides win. Never reads the environment or .env."""
    from aimart_parquet.config import Settings

    def make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "artifact_dir": tmp_path / "artifacts",
            "isolate_jobs": False,
            # The full surface by default; test_quantum_off_by_default covers the real default.
            "enable_quantum": True,
            "log_json": False,
            "public_base_url": kit.BASE_URL,
            "pay_to_evm": kit.PAY_TO_EVM,
            "signing_secret": "test-signing-secret",
            "rate_limit_per_minute": 100_000,
            "facilitator_urls": "https://facilitator.invalid",
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return make


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings()


@pytest.fixture
def facilitator() -> kit.FakeFacilitator:
    return kit.FakeFacilitator()


@pytest.fixture
async def paid_app(
    settings: Settings, facilitator: kit.FakeFacilitator
) -> AsyncIterator[kit.AppHarness]:
    """The production app with x402 payments, backed by the recording fake facilitator."""
    async with kit.running_app(settings, facilitator=facilitator) as harness:
        yield harness


@pytest.fixture
async def free_app(settings: Settings) -> AsyncIterator[kit.AppHarness]:
    """The production app with payments disabled (payments=False)."""
    async with kit.running_app(settings, paid=False) as harness:
        yield harness
