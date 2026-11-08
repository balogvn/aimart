"""The Modal deployment (deploy/modal_app.py, deploy/modal-deploy.sh, deploy-modal.yml).

modal is not a dependency of this package, so the app file is read with ``ast``, not imported.
What must hold:

* Modal answers a web request still running after 150 s with a 303 redirect; a paying client
  must never see one. The runtime settings keep the longest paid call (queue wait, one fetch
  allowance, the job, verify and settle) inside that limit, and inside the payment window.
* Outputs live on the Volume and each call's temporary files on the container's local disk.
* Every deploy-time variable the app reads is one the workflow passes.
* The modal client version is pinned in one place.
"""

from __future__ import annotations

import ast
import base64
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from aimart_parquet.artifacts import build_store
from aimart_parquet.config import PAYMENT_WINDOW_S, SETTLE_MARGIN_S, Settings
from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.service import Service, SourceRef

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "deploy" / "modal_app.py"
SCRIPT = ROOT / "deploy" / "modal-deploy.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-modal.yml"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
# Verify before the job and settle after it are two facilitator round trips, plus the response.
VERIFY_SETTLE_S = 30

pytestmark = pytest.mark.anyio


def _constants() -> dict[str, Any]:
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    values: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id.isupper():
                try:
                    values[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    continue  # not a literal (a Path, a modal object)
    return values


def _modal_settings(**overrides: Any) -> Settings:
    env = _constants()["RUNTIME_ENV"]
    fields = {key.removeprefix("AIMART_").lower(): value for key, value in env.items()}
    return Settings(  # type: ignore[call-arg]
        _env_file=None, enable_quantum=True, payments_enabled=False, **fields, **overrides
    )


def test_the_longest_paid_call_fits_modals_request_limit() -> None:
    limit = _constants()["HTTP_LIMIT_S"]
    settings = _modal_settings()
    jobs = (
        settings.convert_timeout_s,  # convert, convert_upload
        settings.query_timeout_s,  # inspect, preview
        settings.query_timeout_s + 5,  # query: Service.query's outer deadline
        settings.quantum_timeout_s,  # the quantum tools, when enabled
    )
    for job in jobs:
        call = settings.queue_wait_for(job) + settings.fetch_timeout_s + job
        assert call + VERIFY_SETTLE_S <= limit, (job, call)
        assert call + SETTLE_MARGIN_S <= PAYMENT_WINDOW_S  # Payments._check_payment_window


def test_the_function_timeout_is_the_request_limit() -> None:
    source = APP.read_text(encoding="utf-8")
    assert "timeout=HTTP_LIMIT_S," in source
    assert _constants()["HTTP_LIMIT_S"] == 150  # Modal's documented web limit


def test_outputs_go_to_the_volume_and_work_files_stay_local() -> None:
    constants = _constants()
    env = constants["RUNTIME_ENV"]
    assert env["AIMART_ARTIFACT_DIR"] == constants["ARTIFACT_DIR"]
    assert "volumes={ARTIFACT_DIR: volume}" in APP.read_text(encoding="utf-8")
    work = Path(env["AIMART_WORK_DIR"])
    assert not work.is_relative_to(constants["ARTIFACT_DIR"])
    assert env["AIMART_TRUST_FORWARDED_FOR"] == "false"  # Modal's request.client is the caller


def test_every_config_key_is_a_variable_the_workflow_passes() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    [deploy] = [s for s in workflow["jobs"]["deploy"]["steps"] if s.get("id") == "deploy"]
    assert deploy["run"] == "deploy/modal-deploy.sh"
    env = deploy["env"]
    for key in _constants()["CONFIG_KEYS"]:
        assert env[key] == "${{ vars." + key + " }}"
    assert env["MODAL_TOKEN_ID"] == "${{ secrets.MODAL_TOKEN_ID }}"
    assert workflow["jobs"]["deploy"]["environment"]["name"] == "production"


def test_the_modal_version_is_pinned_once() -> None:
    [version] = re.findall(r'^MODAL_VERSION="([\d.]+)"', SCRIPT.read_text(), flags=re.MULTILINE)
    keys = (ROOT / "deploy" / "modal_keys.py").read_text(encoding="utf-8")
    assert f"modal=={version}" in keys
    assert "modal==" not in WORKFLOW.read_text(encoding="utf-8")  # the script installs it


def test_a_release_deploys_to_modal_unless_cloud_run_is_chosen() -> None:
    jobs = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))["jobs"]
    assert jobs["deploy"]["uses"] == "./.github/workflows/deploy-modal.yml"
    assert "vars.DEPLOY_HOST != 'cloudrun'" in jobs["deploy"]["if"]
    assert jobs["deploy-cloudrun"]["uses"] == "./.github/workflows/deploy.yml"
    assert jobs["deploy-cloudrun"]["if"] == "vars.DEPLOY_HOST == 'cloudrun'"


async def test_work_files_go_to_work_dir_and_outputs_to_the_store(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    """The layout Modal runs: work_dir apart from artifact_dir (a different filesystem there)."""
    work = tmp_path / "local-disk"
    settings = make_settings(artifact_dir=tmp_path / "volume", work_dir=work)
    service = Service(settings, build_store(settings), local_mode=False)
    csv = base64.b64encode(b"id,city\n1,Lagos\n2,Abuja\n").decode()
    result = await service.convert(
        SourceRef(content_base64=csv, filename="cities.csv"), ConvertOptions()
    )
    _, stored = service.store.get(result.artifact.id)
    assert stored.is_relative_to(tmp_path / "volume")
    assert not (tmp_path / "volume" / ".work").exists()
    assert work.is_dir()
    assert list(work.iterdir()) == []  # each call's work directory is removed with the call
    inspected = await service.inspect(SourceRef(artifact_id=result.artifact.id))
    assert inspected.rows == 2
