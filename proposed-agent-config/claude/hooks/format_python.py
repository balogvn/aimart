#!/usr/bin/env python3
"""PostToolUse hook: ruff-fix and ruff-format the Python file Claude just edited or wrote.

Why a hook rather than an instruction: formatting is mechanical, and "remember to run the
formatter" is forgotten exactly on the last edit before a commit. Running it per edit keeps
`make lint` green and keeps diffs free of whitespace noise.

Contract:

* Never fail the session. Every path exits 0: the edit already happened, and a formatter
  hiccup must not turn into an error banner on every keystroke.
* Touch only `.py`/`.pyi` files inside this project, and let ruff's own `exclude` apply
  (`--force-exclude`), so files outside the repo or in `.venv` are never rewritten.
* Report what ruff could not fix back to Claude (as `additionalContext`), with the total count
  stated, so a remaining violation is fixed now instead of discovered in CI.

Compatible with Python 3.9+ because it runs under the system `python3`, not the project venv.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MAX_REPORTED = 20  # lines of remaining violations shown to Claude; the total is always stated


def _project_dir() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env).resolve() if env else Path(__file__).resolve().parents[2]


def _ruff(project: Path) -> str | None:
    venv_ruff = project / ".venv" / "bin" / "ruff"
    if venv_ruff.is_file():
        return str(venv_ruff)
    return shutil.which("ruff")


def _run(argv: list[str], project: Path) -> subprocess.CompletedProcess[str]:
    # Fixed argv, no shell: the file path is only ever an argument.
    return subprocess.run(  # noqa: S603
        argv, cwd=project, capture_output=True, text=True, timeout=60, check=False
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        raw = (payload.get("tool_input") or {}).get("file_path")
        if not isinstance(raw, str) or not raw:
            return 0
        project = _project_dir()
        path = Path(raw)
        if not path.is_absolute():
            path = Path(str(payload.get("cwd") or project)) / path
        path = path.resolve()
        if path.suffix not in {".py", ".pyi"} or not path.is_file():
            return 0
        if project not in path.parents:
            return 0
        ruff = _ruff(project)
        if ruff is None:
            return 0
        target = str(path)
        _run([ruff, "check", "--fix", "--force-exclude", "--quiet", target], project)
        _run([ruff, "format", "--force-exclude", "--quiet", target], project)
        # --quiet leaves only the diagnostics themselves: one line per problem, no summary.
        left = _run(
            [ruff, "check", "--force-exclude", "--quiet", "--output-format", "concise", target],
            project,
        )
        problems = [line for line in left.stdout.splitlines() if line.strip()]
        if problems:
            shown = problems[:MAX_REPORTED]
            context = (
                f"ruff still reports {len(problems)} problem(s) in {path.name} after autofix "
                f"(showing {len(shown)} of {len(problems)}):\n" + "\n".join(shown)
            )
            out = {
                "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}
            }
            print(json.dumps(out))
    except Exception:  # noqa: BLE001 - a formatter problem must never break the session
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
