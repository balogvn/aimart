#!/usr/bin/env python3
"""Release guard: a release merge may change version strings, and nothing else.

    python3 scripts/check_release_diff.py [--repo PATH] [BASE..HEAD]

The default range is ``HEAD^1..HEAD``: run on main right after the release PR was merged
(a merge commit or a squash), it is exactly what the release added to main.

Why: on 2026-11-17 two PRs were merged 16 seconds apart and release-please built the 0.1.2
release commit from file contents read before the second merge. Its server.json was the old
file plus the version bump, so merging the release PR silently reverted that PR's server.json
change, and the MCP Registry got the old x402 networks. The same race can revert a dependency
or security bump in uv.lock or pyproject.toml. Nobody reviews a release PR line by line, so
this script does, before anything is published.

Rules, with OLD and NEW read from .release-please-manifest.json at both ends of the range:

* Only files release-please writes may change (``MANAGED`` below).
* CHANGELOG.md may only gain lines.
* uv.lock: only the ``version = "OLD"`` line of the ``[[package]]`` named aimart-parquet,
  becoming ``version = "NEW"``.
* Every other managed file: each removed line, paired in order with the added line that
  replaced it, must differ only by OLD becoming NEW.
* Formatting is never a failure: a JSON or TOML file whose lines fail those checks still passes
  when its parsed data is the same apart from those version changes (the 0.1.0 release
  re-indented server.json, for example).

Stdlib only, Python 3.12+, so it runs on a bare runner before anything is installed.
Exit status: 0 the release is version bumps only; 1 it changed something else; 2 the range or
repository could not be read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from typing import Any

PACKAGE = "aimart-parquet"
TAG_PREFIX = "v"  # release-please-config.json: include-v-in-tag
MANIFEST = ".release-please-manifest.json"
CHANGELOG = "CHANGELOG.md"
UV_LOCK = "uv.lock"
# What release-please writes for this repo: its manifest, the changelog, the python strategy's
# pyproject.toml and src/<package>/__init__.py, and the extra-files in release-please-config.json
# (tests/unit/test_release_diff_guard.py keeps this in step with that file).
MANAGED = frozenset(
    {
        MANIFEST,
        CHANGELOG,
        "pyproject.toml",
        UV_LOCK,
        "server.json",
        "src/aimart_parquet/__init__.py",
    }
)
PARSERS = {
    MANIFEST: "json",
    "server.json": "json",
    "pyproject.toml": "toml",
    UV_LOCK: "toml",
}
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
UV_VERSION_LINE = re.compile(r'^version = "[^"]*"\s*$')
UV_NAME_LINE = re.compile(r'^name = "([^"]*)"\s*$')


class GitError(Exception):
    """A git command failed: the range or the repository is not what we were told."""


@dataclass
class Hunk:
    old_start: int
    new_start: int
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)


@dataclass
class Finding:
    path: str
    line: int | None  # in the file as the release left it
    message: str


class Repo:
    def __init__(self, path: str) -> None:
        self.path = path

    def git(self, *args: str) -> str:
        # Fixed options so a developer's git config cannot change what the guard sees.
        cmd = [
            "git",
            "-C",
            self.path,
            "-c",
            "core.quotePath=false",
            "-c",
            "diff.noprefix=false",
            *args,
        ]
        env = {**os.environ, "LC_ALL": "C", "GIT_PAGER": "cat"}
        proc = subprocess.run(cmd, capture_output=True, check=False, env=env)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise GitError(f"git {' '.join(args)}: {err}")
        return proc.stdout.decode("utf-8", "surrogateescape")

    def commit(self, rev: str) -> str:
        return self.git("rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}").strip()

    def show(self, rev: str, path: str) -> str | None:
        try:
            self.git("cat-file", "-e", f"{rev}:{path}")
        except GitError:
            return None
        return self.git("show", f"{rev}:{path}")

    def changed(self, base: str, head: str) -> list[str]:
        out = self.git("diff", "--no-renames", "--name-only", "-z", base, head, "--")
        return sorted(p for p in out.split("\0") if p)

    def hunks(self, base: str, head: str, path: str) -> list[Hunk]:
        out = self.git(
            "diff",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--diff-algorithm=myers",
            "-U0",
            base,
            head,
            "--",
            path,
        )
        hunks: list[Hunk] = []
        for line in out.split("\n"):
            m = HUNK.match(line)
            if m:
                hunks.append(Hunk(old_start=int(m.group(1)), new_start=int(m.group(3))))
            elif not hunks or line.startswith("\\"):  # file header, or "\ No newline at end"
                continue
            elif line.startswith("-"):
                hunks[-1].removed.append(line[1:])
            elif line.startswith("+"):
                hunks[-1].added.append(line[1:])
        return hunks


def bump(text: str, old: str, new: str) -> str:
    """``text`` with every whole occurrence of version ``old`` replaced by ``new``."""
    return re.sub(rf"(?<![\d.]){re.escape(old)}(?!\.?\d)", new, text)


def is_bump(removed: str, added: str, old: str, new: str) -> bool:
    if removed == added:  # only the newline at the end of the file changed
        return True
    return bump(removed, old, new) == added


def in_own_package(lines: list[str], index: int) -> bool:
    """Is ``lines[index]`` a top-level line of uv.lock's ``[[package]]`` for this package?"""
    j = index - 1
    while j >= 0 and not lines[j].startswith("["):
        j -= 1
    if j < 0 or lines[j].strip() != "[[package]]":
        return False
    for k in range(j + 1, len(lines)):
        if lines[k].startswith("["):
            break
        m = UV_NAME_LINE.match(lines[k])
        if m:
            return m.group(1) == PACKAGE
    return False


def same_data(a: Any, b: Any, old: str, new: str) -> bool:
    """Equal parsed data, except strings in which version ``old`` became ``new``."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same_data(a[k], b[k], old, new) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(
            same_data(x, y, old, new) for x, y in zip(a, b, strict=True)
        )
    if isinstance(a, str):
        return a == b or bump(a, old, new) == str(b)
    return bool(a == b)


def parse(kind: str, text: str) -> Any:
    return json.loads(text) if kind == "json" else tomllib.loads(text)


def same_except_versions(path: str, before: str, after: str, old: str, new: str) -> bool:
    """The formatting escape hatch: parsed data equal apart from the allowed version change."""
    kind = PARSERS.get(path)
    if kind is None:
        return False
    try:
        a, b = parse(kind, before), parse(kind, after)
    except (ValueError, tomllib.TOMLDecodeError):
        return False
    if path != UV_LOCK:
        return same_data(a, b, old, new)
    if not isinstance(a, dict) or not isinstance(a.get("package"), list):
        return False
    for pkg in a["package"]:
        if isinstance(pkg, dict) and pkg.get("name") == PACKAGE and pkg.get("version") == old:
            pkg["version"] = new
    return bool(a == b)


def shown(line: str) -> str:
    return repr(line if len(line) <= 140 else line[:137] + "...")


def line_findings(
    path: str, hunks: list[Hunk], before: list[str], after: list[str], old: str, new: str
) -> list[Finding]:
    found: list[Finding] = []
    for h in hunks:
        if path == CHANGELOG:
            found.extend(
                Finding(path, max(h.new_start, 1), f"line removed from the changelog: {shown(r)}")
                for r in h.removed
            )
            continue
        if len(h.removed) != len(h.added):
            found.extend(
                Finding(path, max(h.new_start, 1), f"line removed: {shown(r)}") for r in h.removed
            )
            found.extend(
                Finding(path, h.new_start + i, f"line added: {shown(a)}")
                for i, a in enumerate(h.added)
            )
            continue
        for i, (r, a) in enumerate(zip(h.removed, h.added, strict=True)):
            ok = is_bump(r, a, old, new)
            if ok and path == UV_LOCK and r != a:
                ok = (
                    UV_VERSION_LINE.match(r) is not None
                    and in_own_package(before, h.old_start - 1 + i)
                    and in_own_package(after, h.new_start - 1 + i)
                )
            if not ok:
                found.append(Finding(path, h.new_start + i, f"{shown(r)} became {shown(a)}"))
    return found


def check_file(
    repo: Repo, base: str, head: str, path: str, old: str, new: str
) -> tuple[list[Finding], str | None]:
    """Findings for one changed path, and a note when it passed only as reformatting."""
    if path not in MANAGED:
        return [
            Finding(path, None, "changed by the release, but release-please never writes this file")
        ], None
    before, after = repo.show(base, path), repo.show(head, path)
    if after is None:
        return [Finding(path, None, "deleted by the release")], None
    if before is None and path != CHANGELOG:
        return [Finding(path, None, "created by the release")], None
    hunks = repo.hunks(base, head, path)
    if not hunks:
        return [
            Finding(
                path, None, "changed in a way a line diff cannot show (binary content or file mode)"
            )
        ], None
    found = line_findings(path, hunks, (before or "").split("\n"), after.split("\n"), old, new)
    if found and before is not None and same_except_versions(path, before, after, old, new):
        return [], f"{path} reformatted, same data"
    return found, None


def manifest_version(repo: Repo, rev: str) -> str:
    text = repo.show(rev, MANIFEST)
    if text is None:
        raise GitError(f"{MANIFEST} is missing at {rev}")
    try:
        version = json.loads(text)["."]
    except (ValueError, KeyError, TypeError) as exc:
        raise GitError(f"{MANIFEST} at {rev} has no '.' version: {exc}") from exc
    if not isinstance(version, str) or not version:
        raise GitError(f"{MANIFEST} at {rev} has no '.' version")
    return version


def annotate(finding: Finding, advice: str) -> str:
    where = finding.path if finding.line is None else f"{finding.path}:{finding.line}"
    text = f"{where}: {finding.message}; {advice}"
    if os.environ.get("GITHUB_ACTIONS") == "true":
        props = f"file={finding.path}" + ("" if finding.line is None else f",line={finding.line}")
        body = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        return f"::error {props}::release guard: {body}"
    return f"release guard: {text}"


def run(repo: Repo, rng: str) -> int:
    if rng.count("..") != 1 or "..." in rng:
        raise GitError(f"expected a range BASE..HEAD, got {rng!r}")
    base_rev, head_rev = rng.split("..")
    base, head = repo.commit(base_rev or "HEAD"), repo.commit(head_rev or "HEAD")
    old, new = manifest_version(repo, base), manifest_version(repo, head)
    short = f"{base[:7]}..{head[:7]}"
    if old == new:
        print(
            f"release guard: {short} does not change the version in {MANIFEST} ({old}), so it is "
            "not a release merge; run this on the merge (or squash) of the release PR",
            file=sys.stderr,
        )
        return 1
    paths = repo.changed(base, head)
    findings: list[Finding] = []
    notes: list[str] = []
    for path in paths:
        found, note = check_file(repo, base, head, path, old, new)
        findings.extend(found)
        if note:
            notes.append(note)
    tag = f"{TAG_PREFIX}{new}"
    if findings:
        advice = (
            f"not a {old} -> {new} version change; if main had this before the release, the "
            f"release reverted it: restore main's content from {base[:7]}"
        )
        for finding in findings:
            print(annotate(finding, advice))
        on_actions = os.environ.get("GITHUB_ACTIONS") == "true"
        print(
            ("::error title=release guard::" if on_actions else "")
            + f"release guard: FAILED: the {old} -> {new} release ({short}) changed "
            f"{len(findings)} thing(s) besides version numbers, so nothing is published from "
            f"this run. The tag {tag} and its GitHub release already exist, so re-running "
            "cannot fix it: commit the fix to main as a `fix:` commit (compare with "
            f"`git diff {base[:7]} {head[:7]}`) and merge the next release PR, which "
            "publishes a new version."
        )
        return 1
    extra = f"; {', '.join(notes)}" if notes else ""
    print(
        f"release guard: OK: {short} releases {old} -> {new} ({tag}); {len(paths)} managed "
        f"file(s) changed, version numbers and changelog additions only{extra}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument(
        "range", nargs="?", default="HEAD^1..HEAD", help="BASE..HEAD (default HEAD^1..HEAD)"
    )
    parser.add_argument(
        "--repo", default=".", help="the repository (default: the current directory)"
    )
    args = parser.parse_args(argv)
    try:
        return run(Repo(args.repo), args.range)
    except GitError as exc:
        print(f"release guard: cannot check the release: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
