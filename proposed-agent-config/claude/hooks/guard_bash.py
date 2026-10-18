#!/usr/bin/env python3
"""PreToolUse guard for Bash: refuse commands that print secrets, spend real money, or destroy work.

Why a hook as well as permission rules: rules match command *text* and are easy to sidestep by
accident (`/bin/cat .env` is not `cat .env`). This script tokenizes the whole command, splits
pipelines and `&&` chains, looks inside `sh -c` / `eval` bodies, and refuses:

* printing secrets: env dumps, secret files (.env*, .wallets/, *.pem, *.key, ~/.ssh), secret-named
  variables such as `$AIMART_SIGNING_SECRET`, and CLIs whose job is to print a token. The length
  form `${#NAME}` is allowed on purpose: "verify the length, not the listing" is how the
  owner's last project found an empty production key.
* spending real money: the mainnet confirmation sentence exists so that a *human* types it.
  An agent typing it defeats the only check there is. Wallet CLIs that send funds are refused.
* destroying work: force-pushes, `git clean -x` (it deletes the gitignored .env and .wallets/),
  and recursive deletes outside the temp directories and the repo's gitignored build output.

It is a seatbelt, not a sandbox; another spelling always exists. It exists to stop the accidental
leak: the `env` an agent runs while debugging config, which puts every secret into a transcript.

Hook contract (code.claude.com/docs/en/hooks): exit 0 allows, exit 2 blocks and shows stderr to
Claude. A crash here also exits 2, so a broken guard is loud rather than silently open.
Compatible with Python 3.9+ because it runs under the system `python3`, not the project venv.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

# The human-only confirmation for spending real USDC, carried over from the owner's x402 project.
MAINNET_CONFIRMATION = "I_UNDERSTAND_THIS_SPENDS_REAL_USDC"

_CONTROL = {"|", "||", "&&", ";", "&", "|&", "(", ")", ";;"}
_WRAPPERS = {"sudo", "command", "builtin", "exec", "nohup", "nice", "time", "noglob", "xargs"}
# A variable is secret-looking when any `_`-separated word of its name is one of these, so
# AIMART_SIGNING_KEY_MLDSA matches on KEY while MONKEY_COUNT does not.
_SECRET_WORDS = frozenset(
    {
        "SECRET", "SECRETS", "TOKEN", "TOKENS", "PASSWORD", "PASSWD", "PRIVATE", "MNEMONIC",
        "SEED", "KEY", "KEYS", "APIKEY", "CREDENTIAL", "CREDENTIALS",
    }
)  # fmt: skip
_VAR_REF = re.compile(
    r"""\$\{?([A-Za-z_][A-Za-z0-9_]*)"""  # $NAME and ${NAME}; ${#NAME} (length) never matches
    r"""|(?:printenv\s+|getenv\(|environ\[|environ\.get\()\s*\\?['"]?([A-Za-z_][A-Za-z0-9_]*)"""
)
_ENV_OBJECT_DUMP = re.compile(
    r"os\.environ(?!\s*(?:\[|\.get\b|\.setdefault\b|\.pop\b))|process\.env(?!\s*[.\[])"
    r"|/proc/[^/\s]+/environ"
)
_TOKEN_PRINTERS = re.compile(
    r"\bgh\s+auth\s+token\b"
    r"|\bgcloud\s+(?:auth\s+(?:application-default\s+)?print-(?:access|identity)-token"
    r"|secrets\s+versions\s+access)\b"
    r"|\baws\s+secretsmanager\s+get-secret-value\b"
    r"|\bsecurity\s+find-(?:generic|internet)-password\b.*\s-w\b"
    r"|\bop\s+(?:read|item\s+get)\b"
)
_WALLET_SENDS = re.compile(
    r"\bcast\s+(?:send|wallet\s+(?:private-key|decrypt-keystore))\b"
    r"|\bsolana\s+transfer\b|\bspl-token\s+transfer\b"
)
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _is_secret_name(name: str) -> bool:
    return any(part in _SECRET_WORDS for part in name.upper().split("_"))


def _is_secret_path(token: str) -> bool:
    path = token.rstrip("/")
    name = path.rsplit("/", 1)[-1]
    parts = set(path.split("/"))
    if name == ".env" or (name.startswith(".env.") and not name.startswith(".env.example")):
        return True
    if ".wallets" in parts or ".ssh" in parts:
        return True
    if name.startswith(("id_rsa", "id_ed25519", "id_ecdsa")):
        return True
    return name.endswith((".pem", ".key")) and len(name) > 4


def _strip_heredocs(command: str) -> str:
    """Drop heredoc bodies: they are file contents being written, not commands being run."""
    lines = command.split("\n")
    kept: list[str] = []
    terminator: str | None = None
    for line in lines:
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        kept.append(line)
        match = _HEREDOC.search(line)
        if match:
            terminator = match.group(2)
    return "\n".join(kept)


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:  # unbalanced quotes: fall back to a rough split rather than crash
        return command.replace("\n", " ; ").split()


def _segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _CONTROL:
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def _command_words(words: list[str]) -> list[str]:
    """Strip leading NAME=value assignments and wrappers such as `sudo` or `timeout 30`."""
    i = 0
    while i < len(words):
        word = words[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word) or word in _WRAPPERS:
            i += 1
        elif word == "timeout" and i + 1 < len(words):
            i += 2
        else:
            break
    return words[i:]


class Blocked(Exception):
    pass


# Disposable places where a recursive delete cannot lose work.
_TEMP_DIRS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")  # noqa: S108


def _temp_roots(payload: dict[str, object]) -> list[Path]:
    extras = (os.environ.get("TMPDIR"), payload.get("scratchpad_dir"))
    roots = [Path(d) for d in _TEMP_DIRS]
    roots.extend(Path(e) for e in extras if isinstance(e, str) and e)
    return [r.resolve() for r in roots if r.exists()]


def _git_ignored(path: Path, project: Path) -> bool:
    # A pattern such as `htmlcov/` only matches directories, and git cannot tell that a path
    # which does not exist yet is one, so ask about both spellings.
    for spelling in (str(path), str(path) + "/"):
        try:
            # Fixed argv and no shell: the path is only ever an argument. git comes from PATH,
            # as in the developer's own shell.
            done = subprocess.run(  # noqa: S603
                ["git", "-C", str(project), "check-ignore", "-q", "--", spelling],  # noqa: S607
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if done.returncode == 0:
            return True
    return False


def _check_rm(args: list[str], cwd: Path, project: Path, payload: dict[str, object]) -> None:
    flags = [a for a in args if a.startswith("-")]
    recursive = any(
        a in {"--recursive", "-r", "-R"} or (not a.startswith("--") and ("r" in a or "R" in a))
        for a in flags
    )
    if not recursive:
        return
    temp = _temp_roots(payload)
    for target in (a for a in args if not a.startswith("-")):
        if "$" in target or "`" in target or target.startswith("~"):
            msg = f"recursive rm of an unexpanded path ({target}); expand it first"
            raise Blocked(msg)
        literal = re.split(r"[*?\[]", target, maxsplit=1)[0] or "."
        resolved = (cwd / literal).resolve()
        if any(resolved == r or r in resolved.parents for r in temp):
            continue
        inside = project in resolved.parents
        if inside and ".git" not in resolved.parts and _git_ignored(resolved, project):
            continue
        msg = (
            f"recursive rm of {resolved} - only temp directories and gitignored build output "
            "inside the repo may be deleted this way"
        )
        raise Blocked(msg)


def _check_segment(words: list[str], cwd: Path, project: Path, payload: dict[str, object]) -> None:
    words = _command_words(words)
    if not words:
        return
    cmd, args = Path(words[0]).name, words[1:]
    if cmd in {"sh", "bash", "zsh", "dash"} and "-c" in args:
        body_index = args.index("-c") + 1
        if body_index < len(args):
            check_command(args[body_index], cwd, project, payload)
        return
    if cmd == "eval":
        check_command(" ".join(args), cwd, project, payload)
        return
    if cmd == "env":
        rest = [a for a in args if not a.startswith("-")]
        inner = _command_words(rest)
        if not inner:
            msg = "`env` without a command prints every environment variable, secrets included"
            raise Blocked(msg)
        _check_segment(inner, cwd, project, payload)
        return
    if cmd == "printenv" and not [a for a in args if not a.startswith("-")]:
        msg = "`printenv` without a name prints every environment variable, secrets included"
        raise Blocked(msg)
    dump_flags = {"-p", "-x", "-px", "-xp"}
    if (cmd in {"set", "export", "declare", "typeset"} and not args) or (
        cmd in {"export", "declare", "typeset"} and set(args) <= dump_flags and args
    ):
        msg = f"`{' '.join(words)}` dumps shell variables, secrets included"
        raise Blocked(msg)
    if cmd == "git" and args[:1] == ["push"]:
        push_args = args[1:]
        forced = any(
            a in {"-f", "--force", "--force-with-lease", "--force-if-includes"}
            or a.startswith(("--force-with-lease=", "+"))
            for a in push_args
        )
        if forced:
            msg = "force-push rewrites shared history; the owner does this by hand if ever"
            raise Blocked(msg)
    if (
        cmd == "git"
        and args[:1] == ["clean"]
        and any(
            a.startswith("-") and not a.startswith("--") and ("x" in a or "X" in a) for a in args
        )
    ):
        msg = "`git clean -x` deletes gitignored files - that is where .env and .wallets/ live"
        raise Blocked(msg)
    if cmd == "rm":
        _check_rm(args, cwd, project, payload)


def check_command(command: str, cwd: Path, project: Path, payload: dict[str, object]) -> None:
    if MAINNET_CONFIRMATION in command:
        msg = (
            f"{MAINNET_CONFIRMATION} is typed by a human, never by an agent: it is the only "
            "check standing between a script and real USDC. Ask the user to run the command."
        )
        raise Blocked(msg)
    text = _strip_heredocs(command)
    if _WALLET_SENDS.search(text):
        msg = "wallet CLIs that move funds are off-limits to agents; ask the user"
        raise Blocked(msg)
    if _TOKEN_PRINTERS.search(text):
        msg = "this command prints a credential into the transcript"
        raise Blocked(msg)
    if _ENV_OBJECT_DUMP.search(text):
        msg = "dumping the whole environment prints every secret; read one non-secret name instead"
        raise Blocked(msg)
    for match in _VAR_REF.finditer(text):
        name = match.group(1) or match.group(2)
        if name and _is_secret_name(name):
            msg = (
                f"this would expand or print {name}, which looks secret; "
                f"check its length with ${{#{name}}} instead of its value"
            )
            raise Blocked(msg)
    tokens = _tokens(text)
    for token in tokens:
        if _is_secret_path(token):
            msg = (
                f"{token} holds secrets and is off-limits to agents "
                "(.env.example is fine); ask the user if a value is needed"
            )
            raise Blocked(msg)
    for segment in _segments(tokens):
        _check_segment(segment, cwd, project, payload)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if payload.get("tool_name") != "Bash":
            return 0
        command = str((payload.get("tool_input") or {}).get("command") or "")
        cwd = Path(str(payload.get("cwd") or Path.cwd())).resolve()
        project_env = os.environ.get("CLAUDE_PROJECT_DIR")
        project = (
            Path(project_env).resolve() if project_env else Path(__file__).resolve().parents[2]
        )
        check_command(command, cwd, project, payload)
    except Blocked as exc:
        print(f"Blocked by .claude/hooks/guard_bash.py: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - fail closed on any bug, and say so
        print(
            f"guard_bash.py failed ({type(exc).__name__}: {exc}); blocking to be safe. "
            "Fix the hook or ask the user to run the command.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
