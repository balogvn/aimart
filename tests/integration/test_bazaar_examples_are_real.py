"""The examples the 402s advertise are real calls, and the service answers what they show.

Facilitator catalogs and x402 indexes show each paid route's bazaar example to agents; some
replay it (x402gle pays for every route and grades the answer), and agents copy it. The
examples used to name https://example.com/sales.csv and an artifact_id that never existed, so
every replay was refused and an auditioned route could only fail. Each input is now a sample
file in this repository, fetched from GitHub, and each output is what the service answers for
it: every field an example shows must be the one the service sends (ids, links and byte counts
differ per call and are checked for shape only).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest

import aimart_testkit as kit
from aimart_parquet import service as service_module
from aimart_parquet.payments.bazaar import EXAMPLES, SAMPLES, example_output
from aimart_parquet.sources import Fetched

if TYPE_CHECKING:
    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[2]
REPLAYED = ("convert", "inspect", "preview", "query")  # the upload example is a form, no file


def sample_files() -> list[Path]:
    names = {
        EXAMPLES[key]["input"]["url"].removeprefix(f"{SAMPLES}/")
        for key in REPLAYED
        if "url" in EXAMPLES[key]["input"]
    }
    return [ROOT / "docs" / "examples" / name for name in sorted(names)]


def test_every_replayed_example_fetches_a_sample_file_of_this_repository() -> None:
    for key in REPLAYED:
        url = EXAMPLES[key]["input"].get("url", "")
        assert url.startswith(f"{SAMPLES}/"), key  # no artifact_id that exists nowhere
    files = sample_files()
    assert [f.name for f in files] == ["sales.csv", "sales.parquet"]
    for path in files:
        assert path.is_file(), path


def test_the_sample_files_are_not_ignored_by_git() -> None:
    """*.parquet is ignored repository-wide: an ignored sample would never reach GitHub, and
    every example that names it would fetch a 404."""
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    for path in sample_files():
        ignored = subprocess.run([git, "check-ignore", "-q", str(path)], cwd=ROOT, check=False)
        assert ignored.returncode == 1, f"{path} is ignored by .gitignore"


def assert_shows(example: Any, real: Any, where: str) -> None:
    """Every field the example shows is the service's own; the service may send more."""
    if isinstance(example, dict):
        assert isinstance(real, dict), where
        for key, value in example.items():
            assert key in real, f"{where}.{key} is not in the answer"
            assert_shows(value, real[key], f"{where}.{key}")
    elif isinstance(example, list):
        assert isinstance(real, list), where
        assert len(real) == len(example), where
        for i, (shown, sent) in enumerate(zip(example, real, strict=True)):
            assert_shows(shown, sent, f"{where}[{i}]")
    else:
        assert example == real, (where, example, real)


@pytest.fixture
def samples_from_this_checkout(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve the sample URLs from docs/examples, as GitHub serves them; refuse anything else."""
    fetched: list[str] = []

    async def fetch(url: str, dest: Path, **_: Any) -> Fetched:
        assert url.startswith(f"{SAMPLES}/"), url
        data = (ROOT / "docs" / "examples" / url.removeprefix(f"{SAMPLES}/")).read_bytes()
        await anyio.Path(dest).write_bytes(data)
        fetched.append(url)
        return Fetched(path=dest, filename=url.rsplit("/", 1)[1], content_type=None, size=len(data))

    monkeypatch.setattr(service_module, "fetch_url", fetch)
    return fetched


@pytest.mark.parametrize("key", REPLAYED)
async def test_a_paid_replay_of_the_example_answers_what_the_example_shows(
    key: str, settings: Settings, samples_from_this_checkout: list[str]
) -> None:
    facilitator = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitator=facilitator) as h:
        op_path = {"convert": "/v1/convert"}.get(key, f"/v1/{key}")
        quote = kit.payment_required(await h.http.post(op_path, json=EXAMPLES[key]["input"]))
        shown = quote["extensions"]["bazaar"]["info"]["output"]["example"]
        paid = await h.paying.post(op_path, json=EXAMPLES[key]["input"])
    assert paid.status_code == 200, paid.text  # before: 422 fetch_failed or 404, unpaid
    assert len(facilitator.settled) == 1
    assert samples_from_this_checkout == [EXAMPLES[key]["input"]["url"]]
    answer = paid.json()
    assert shown == example_output(key, settings.public_base_url)
    if key == "convert":
        artifact, shown_artifact = answer["artifact"], shown["artifact"]
        assert artifact["filename"] == shown_artifact["filename"]
        prefix = f"{settings.public_base_url}/v1/artifacts/"
        assert artifact["download_url"].startswith(f"{prefix}{artifact['id']}/sales.parquet?")
        assert shown_artifact["download_url"].startswith(prefix)
        assert isinstance(artifact["size_bytes"], int)
        assert isinstance(shown_artifact["size_bytes"], int)
        assert_shows(shown["report"], answer["report"], "report")
    else:
        assert_shows(shown, answer, key)
