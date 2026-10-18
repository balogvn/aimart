#!/usr/bin/env bash
# Self-test for the Claude Code hooks: pipes the JSON Claude Code sends into each hook and
# checks the exit code (0 = allow, 2 = block) and, for the formatter, the file on disk.
#
# Why a script and not pytest: the hooks run under the system python3 (3.9+), outside the
# project venv, so they are tested the way Claude Code runs them.
#
#   bash .claude/hooks/test_hooks.sh            # after installing into .claude/
#   bash proposed-agent-config/claude/hooks/test_hooks.sh   # before installing
set -u
HOOKS="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HOOKS/../.." && pwd)"
# Before installation the hooks live two levels deeper than the repo root.
[ -f "$REPO/pyproject.toml" ] || REPO="$(cd "$HOOKS/../../.." && pwd)"
PY="${PYTHON:-python3}"
pass=0
fail=0

json_bash() {  # $1 = command, $2 = cwd
  "$PY" -c 'import json,sys; print(json.dumps({"hook_event_name":"PreToolUse","tool_name":"Bash","cwd":sys.argv[2],"tool_input":{"command":sys.argv[1]}}))' "$1" "$2"
}

expect_guard() {  # $1 = expected exit code, $2 = command
  local out code
  out="$(json_bash "$2" "$REPO" | CLAUDE_PROJECT_DIR="$REPO" "$PY" "$HOOKS/guard_bash.py" 2>&1)"
  code=$?
  if [ "$code" = "$1" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'FAIL guard: expected %s got %s for: %s\n  %s\n' "$1" "$code" "$2" "$out"
  fi
}

# --- allowed: everyday work must not be blocked
expect_guard 0 'make check'
expect_guard 0 '.venv/bin/pytest -q -m "not slow" tests/unit'
expect_guard 0 'git status --short && git diff --stat'
expect_guard 0 'cp .env.example .env.example.bak'
expect_guard 0 'cat .env.example'
expect_guard 0 'echo "${#AIMART_SIGNING_SECRET}"'
expect_guard 0 'env AIMART_ISOLATE_JOBS=false .venv/bin/pytest -q'
expect_guard 0 'AIMART_NETWORK_MODE=testnet .venv/bin/aimart-parquet serve'
expect_guard 0 'printenv PATH'
expect_guard 0 'python3 -c "import os; print(os.environ[\"HOME\"])"'
expect_guard 0 'rm -rf /tmp/aimart-artifacts'
expect_guard 0 'rm -rf htmlcov .pytest_cache mutants'
expect_guard 0 'rm build.log'
expect_guard 0 'git push origin main'
expect_guard 0 'grep -rn "env" docs/'
expect_guard 0 "cat > notes.txt <<'EOF'
env
rm -rf /
EOF"
expect_guard 0 'set -euo pipefail'

# --- blocked: printing secrets
expect_guard 2 'env'
expect_guard 2 'env | grep AIMART'
expect_guard 2 'printenv'
expect_guard 2 'export -p'
expect_guard 2 'set'
expect_guard 2 'cat .env'
expect_guard 2 'cat ./config/.env.production'
expect_guard 2 '/bin/cat .env'
expect_guard 2 'ls .wallets/'
expect_guard 2 'head -c 100 deploy/key.pem'
expect_guard 2 'echo $AIMART_SIGNING_SECRET'
expect_guard 2 'echo "${AIMART_SIGNING_KEY_MLDSA}"'
expect_guard 2 'printenv GITHUB_TOKEN'
expect_guard 2 'python3 -c "import os; print(os.environ)"'
expect_guard 2 'python3 -c "import os; print(os.getenv(\"EVM_PRIVATE_KEY\"))"'
expect_guard 2 'cat /proc/self/environ'
expect_guard 2 'gh auth token'
expect_guard 2 'gcloud secrets versions access latest --secret=aimart-signing'
expect_guard 2 'bash -c "env | sort"'
expect_guard 2 'source .env && make serve'
# --- blocked: spending real money
expect_guard 2 'AIMART_MAINNET_CONFIRM=I_UNDERSTAND_THIS_SPENDS_REAL_USDC python scripts/x.py'
expect_guard 2 'cast send 0xabc "transfer(address,uint256)" 0xdef 1'
expect_guard 2 'solana transfer Abc 1'
# --- blocked: destroying work
expect_guard 2 'git push --force origin main'
expect_guard 2 'git push -f'
expect_guard 2 'git push origin +main'
expect_guard 2 'git clean -fdx'
expect_guard 2 'rm -rf src'
expect_guard 2 'rm -rf .'
expect_guard 2 'rm -rf /'
expect_guard 2 'rm -rf ~/Documents'
expect_guard 2 'rm -fr "$HOME"'
expect_guard 2 'cd /tmp && rm -rf /Users'
# --- robustness: unbalanced quotes must not crash the guard into a false allow
expect_guard 2 'cat .env "unterminated'

# --- formatter hook
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/proj" "$WORK/elsewhere"
printf 'import os,sys\nx = {  "a":1 }\n' > "$WORK/proj/messy.py"
printf 'x = {  "a":1 }\n' > "$WORK/elsewhere/outside.py"
printf 'x = {  "a":1 }\n' > "$WORK/proj/notes.txt"
cp "$WORK/elsewhere/outside.py" "$WORK/outside.orig"
cp "$WORK/proj/notes.txt" "$WORK/notes.orig"

run_fmt() {  # $1 = file_path ; prints the hook's stdout, returns its exit code
  "$PY" -c 'import json,sys; print(json.dumps({"hook_event_name":"PostToolUse","tool_name":"Edit","tool_input":{"file_path":sys.argv[1]}}))' "$1" \
    | CLAUDE_PROJECT_DIR="$WORK/proj" PATH="$REPO/.venv/bin:$PATH" "$PY" "$HOOKS/format_python.py"
}

check() {  # $1 = description, then a test expression
  local name="$1"; shift
  if "$@"; then pass=$((pass + 1)); else fail=$((fail + 1)); echo "FAIL fmt: $name"; fi
}

out="$(run_fmt "$WORK/proj/messy.py")"; code=$?
check "exit 0 on a project .py file" test "$code" = 0
check "formats the file" grep -q '^x = {"a": 1}$' "$WORK/proj/messy.py"
check "removes the unused import (ruff check --fix)" test "$(grep -c 'import' "$WORK/proj/messy.py")" = 0
check "prints nothing when clean" test -z "$out"
run_fmt "$WORK/elsewhere/outside.py" >/dev/null; code=$?
check "exit 0 outside the project" test "$code" = 0
check "never touches files outside the project" cmp -s "$WORK/elsewhere/outside.py" "$WORK/outside.orig"
run_fmt "$WORK/proj/notes.txt" >/dev/null; code=$?
check "ignores non-Python files" cmp -s "$WORK/proj/notes.txt" "$WORK/notes.orig"
printf 'def f(:\n' > "$WORK/proj/broken.py"
out="$(run_fmt "$WORK/proj/broken.py")"; code=$?
check "exit 0 on a syntax error" test "$code" = 0
check "reports unfixable problems to Claude" grep -q additionalContext <<<"$out"
printf 'not json' | "$PY" "$HOOKS/format_python.py"; code=$?
check "exit 0 on garbage input" test "$code" = 0

echo "hooks self-test: $pass passed, $fail failed"
[ "$fail" = 0 ]
