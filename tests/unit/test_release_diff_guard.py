"""The release guard (scripts/check_release_diff.py) and where release.yml runs it.

On 2026-11-17 the 0.1.2 release commit carried server.json as it was before the PR merged 16
seconds earlier, so merging the release PR reverted that PR and the MCP Registry published the
old x402 networks. The guard reads what the release merge changed and fails unless it is only
version numbers (and CHANGELOG additions). Each case below is a small git repository shaped
like this one: a clean release must pass, as a merge commit and as a squash; every way the
release can carry stale content must fail.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "check_release_diff.py"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
CONFIG = ROOT / "release-please-config.json"

OLD, NEW = "0.1.1", "0.1.2"
TESTNET = ["eip155:84532", "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"]


def server_json(version: str, networks: dict[str, list[str]], indent: int = 2) -> str:
    doc = {
        "name": "io.github.balogvn/aimart-parquet",
        "version": version,
        "packages": [{"registryType": "pypi", "identifier": "aimart-parquet", "version": version}],
        "remotes": [{"type": "streamable-http", "url": "https://example.test/mcp"}],
        "_meta": {"x402": {"networks": networks, "notes": "Signed with ML-DSA-65."}},
    }
    return json.dumps(doc, indent=indent) + "\n"


def uv_lock(version: str, httpx: str = "0.28.1", other: str = "0.1.1") -> str:
    return f"""version = 1
requires-python = ">=3.12"

[[package]]
name = "aimart-parquet"
version = "{version}"
source = {{ editable = "." }}
dependencies = [
    {{ name = "httpx" }},
]

[package.metadata]
requires-dist = [{{ name = "httpx", specifier = ">=0.28" }}]

[[package]]
name = "httpx"
version = "{httpx}"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "tiny-dep"
version = "{other}"
source = {{ registry = "https://pypi.org/simple" }}
"""


def files_at(
    version: str,
    *,
    networks: dict[str, list[str]] | None = None,
    changelog: str = "# Changelog\n",
    httpx: str = "0.28.1",
) -> dict[str, str]:
    return {
        ".release-please-manifest.json": json.dumps({".": version}, indent=2) + "\n",
        "CHANGELOG.md": changelog,
        "pyproject.toml": f'[project]\nname = "aimart-parquet"\nversion = "{version}"\n'
        'dependencies = ["httpx>=0.28"]\n',
        "uv.lock": uv_lock(version, httpx=httpx),
        "server.json": server_json(version, networks or {"mainnet": ["eip155:8453"]}),
        "src/aimart_parquet/__init__.py": f'__version__ = "{version}"\n',
        "README.md": "# AImart Parquet\n",
    }


NOTES = f"# Changelog\n\n## [{NEW}](https://example.test) (2026-11-17)\n\n* a fix\n"


def clean_env(**extra: str) -> dict[str, str]:
    """No GIT_* (a hook's GIT_INDEX_FILE would point git elsewhere) and no GITHUB_ACTIONS."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GITHUB_ACTIONS"))}
    return {**env, **extra}


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        env = {
            **clean_env(),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.test",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.test",
        }
        proc = subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        return proc.stdout.strip()

    def commit(self, files: dict[str, str], message: str) -> str:
        for name, text in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def merge_release(self, files: dict[str, str]) -> None:
        """release-please's flow: a release branch off main, merged with a merge commit."""
        self.git("checkout", "-q", "-b", "release-please")
        self.commit(files, f"chore: release {NEW}")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", "Merge the release PR", "release-please")


def guard(repo: Repo, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), "--repo", str(repo.path), *args],
        capture_output=True,
        text=True,
        env=clean_env(),
        check=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    r = Repo(tmp_path / "repo")
    r.commit(files_at(OLD), "feat: initial")
    return r


# ------------------------------------------------------------------ releases that must pass


def test_a_clean_release_merge_passes(repo: Repo) -> None:
    repo.merge_release(files_at(NEW, changelog=NOTES))
    result = guard(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("release guard: OK: ")
    assert f"{OLD} -> {NEW}" in result.stdout
    assert len(result.stdout.strip().splitlines()) == 1


def test_a_clean_squashed_release_passes(repo: Repo) -> None:
    repo.commit(files_at(NEW, changelog=NOTES), f"chore: release {NEW} (#24)")
    result = guard(repo, "HEAD^1..HEAD")
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_release_that_only_reformats_json_passes(repo: Repo) -> None:
    """The 0.1.0 release re-indented server.json (release-please re-serialises JSON files)."""
    files = files_at(NEW, changelog=NOTES)
    files["server.json"] = server_json(NEW, {"mainnet": ["eip155:8453"]}, indent=4)
    repo.merge_release(files)
    result = guard(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "server.json reformatted, same data" in result.stdout


def test_a_pr_merged_before_the_release_is_kept_by_a_correct_release(repo: Repo) -> None:
    repo.commit(
        {"server.json": server_json(OLD, {"mainnet": ["eip155:8453"], "testnet": TESTNET})}, "x"
    )
    repo.commit({"uv.lock": uv_lock(OLD, httpx="0.28.2")}, "deps: bump httpx")
    files = files_at(NEW, changelog=NOTES, httpx="0.28.2")
    files["server.json"] = server_json(NEW, {"mainnet": ["eip155:8453"], "testnet": TESTNET})
    repo.merge_release(files)
    assert guard(repo).returncode == 0


# ------------------------------------------------------------------ releases that must fail


def test_the_incident_a_release_built_from_stale_server_json_fails(repo: Repo) -> None:
    """2026-11-17: #32 narrowed the networks; the release commit carried the file from before."""
    repo.commit(files_at(OLD, networks={"mainnet": ["eip155:8453"], "testnet": TESTNET}), "base")
    repo.commit({"server.json": server_json(OLD, {"mainnet": ["eip155:8453"]})}, "docs: #32")
    stale = files_at(
        NEW, changelog=NOTES, networks={"mainnet": ["eip155:8453"], "testnet": TESTNET}
    )
    repo.merge_release(stale)
    result = guard(repo)
    assert result.returncode == 1
    lines = result.stdout.strip().splitlines()
    offending = [ln for ln in lines if ln.startswith("release guard: server.json:")]
    assert offending, result.stdout
    assert any("testnet" in ln for ln in offending)
    assert all("restore main's content from" in ln for ln in offending)
    assert not any("uv.lock" in ln or "pyproject" in ln for ln in lines)
    assert "already exist" in lines[-1]
    assert f"v{NEW}" in lines[-1]
    assert "commit the fix to main" in lines[-1]


def test_a_release_that_reverts_a_dependency_bump_in_uv_lock_fails(repo: Repo) -> None:
    repo.commit({"uv.lock": uv_lock(OLD, httpx="0.28.2")}, "deps: security bump")
    repo.merge_release(files_at(NEW, changelog=NOTES, httpx="0.28.1"))  # stale lock
    result = guard(repo)
    assert result.returncode == 1
    assert "release guard: uv.lock:" in result.stdout
    assert "0.28.2" in result.stdout


def test_uv_lock_may_bump_only_this_packages_version(repo: Repo) -> None:
    """A dependency whose version happens to equal ours must not move with it."""
    files = files_at(NEW, changelog=NOTES)
    files["uv.lock"] = uv_lock(NEW).replace(
        'name = "tiny-dep"\nversion = "0.1.1"', 'name = "tiny-dep"\nversion = "0.1.2"'
    )
    repo.merge_release(files)
    result = guard(repo)
    assert result.returncode == 1
    found = [ln for ln in result.stdout.splitlines() if ln.startswith("release guard: uv.lock:")]
    assert len(found) == 1, result.stdout
    assert found[0].startswith("release guard: uv.lock:22: ")  # tiny-dep's line, not ours (6)
    assert "'version = \"0.1.1\"' became 'version = \"0.1.2\"'" in found[0]


def test_a_reverted_line_in_pyproject_fails(repo: Repo) -> None:
    repo.commit(
        {"pyproject.toml": files_at(OLD)["pyproject.toml"].replace(">=0.28", ">=0.28.2")}, "deps"
    )
    repo.merge_release(files_at(NEW, changelog=NOTES))
    result = guard(repo)
    assert result.returncode == 1
    assert "release guard: pyproject.toml:" in result.stdout


def test_a_file_release_please_never_writes_fails(repo: Repo) -> None:
    files = files_at(NEW, changelog=NOTES)
    files["README.md"] = "# AImart Parquet (stale)\n"
    repo.merge_release(files)
    result = guard(repo)
    assert result.returncode == 1
    assert (
        "release guard: README.md: changed by the release, but release-please never writes"
        in result.stdout
    )


def test_a_changelog_that_loses_lines_fails(repo: Repo) -> None:
    repo.commit({"CHANGELOG.md": "# Changelog\n\nRelease notes are generated.\n"}, "docs")
    repo.merge_release(files_at(NEW, changelog=NOTES))
    result = guard(repo)
    assert result.returncode == 1
    assert "CHANGELOG.md" in result.stdout
    assert "Release notes are generated." in result.stdout


def test_a_range_without_a_version_change_is_not_a_release(repo: Repo) -> None:
    repo.commit({"README.md": "changed\n"}, "docs")
    result = guard(repo)
    assert result.returncode == 1
    assert "not a release merge" in result.stderr


def test_an_unreadable_range_is_exit_2(repo: Repo) -> None:
    assert guard(repo, "nope..HEAD").returncode == 2
    assert guard(repo, "HEAD").returncode == 2


def test_on_actions_findings_are_error_annotations(repo: Repo) -> None:
    files = files_at(NEW, changelog=NOTES)
    files["README.md"] = "100% stale\n"
    repo.merge_release(files)
    env = clean_env(GITHUB_ACTIONS="true")
    result = subprocess.run(
        [sys.executable, str(GUARD), "--repo", str(repo.path)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout.startswith("::error file=README.md::release guard: README.md:")


def _guard_module() -> Any:
    spec = importlib.util.spec_from_file_location("check_release_diff", GUARD)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while it executes
    spec.loader.exec_module(module)
    return module


def test_the_managed_set_is_what_release_please_config_names() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    package = config["packages"]["."]
    names = {f["path"] for f in package["extra-files"]}
    names |= {package["changelog-path"], ".release-please-manifest.json"}
    # The python strategy's own files: the project version and the package's __init__.py.
    names |= {"pyproject.toml", "src/aimart_parquet/__init__.py"}
    module = _guard_module()
    assert names == module.MANAGED
    assert package["package-name"] == module.PACKAGE
    assert (module.TAG_PREFIX == "v") is config["include-v-in-tag"]


@pytest.mark.parametrize(
    ("merge", "status"),
    [("e813ae0", 0), ("6b5677e", 0), ("c157568", 1)],  # 0.1.0, 0.1.1, and the 0.1.2 incident
)
def test_the_real_release_merges(merge: str, status: int) -> None:
    """Runs only where this repository's history is present (not in a shallow CI checkout)."""
    present = subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "-e", f"{merge}^1^{{commit}}"],
        capture_output=True,
        env=clean_env(),
        check=False,
    )
    if present.returncode != 0:
        pytest.skip("release history not in this checkout")
    result = subprocess.run(
        [sys.executable, str(GUARD), "--repo", str(ROOT), f"{merge}^1..{merge}"],
        capture_output=True,
        text=True,
        env=clean_env(),
        check=False,
    )
    assert result.returncode == status, result.stdout + result.stderr


# ------------------------------------------------------------------ release.yml


def _release() -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    return workflow


def _needs(job: dict[str, Any]) -> set[str]:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


def test_the_guard_runs_after_release_please_and_only_when_a_release_was_created() -> None:
    steps = _release()["jobs"]["release-please"]["steps"]
    rp = next(i for i, s in enumerate(steps) if s.get("id") == "rp")
    guard_at = next(
        i for i, s in enumerate(steps) if "scripts/check_release_diff.py" in s.get("run", "")
    )
    assert guard_at > rp
    checkout = steps[guard_at - 1]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["ref"] == "${{ steps.rp.outputs.sha }}"
    assert int(checkout["with"]["fetch-depth"]) >= 2
    assert checkout["with"]["persist-credentials"] is False
    for step in (checkout, steps[guard_at]):
        assert step["if"] == "steps.rp.outputs.release_created == 'true'"
        assert "continue-on-error" not in step
    assert "HEAD^1..HEAD" in steps[guard_at]["run"]
    assert guard_at == len(steps) - 1  # nothing after it can publish


def test_every_publishing_and_deploying_job_depends_on_the_guard() -> None:
    jobs = _release()["jobs"]
    assert "continue-on-error" not in jobs["release-please"]

    def upstream(name: str) -> set[str]:
        seen: set[str] = set()
        todo = list(_needs(jobs[name]))
        while todo:
            n = todo.pop()
            if n not in seen:
                seen.add(n)
                todo.extend(_needs(jobs[n]))
        return seen

    others = [n for n in jobs if n != "release-please"]
    assert {"build", "pypi", "image", "mcp-registry", "deploy", "deploy-cloudrun"} <= set(others)
    for name in others:
        assert "release-please" in upstream(name), name
        # always()/failure()/cancelled() in `if` would run the job after the guard failed.
        condition = str(jobs[name].get("if", ""))
        assert not any(f in condition for f in ("always()", "failure()", "cancelled()")), name
