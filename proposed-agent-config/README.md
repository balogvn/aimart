# Proposed Claude Code configuration (not active yet)

This folder holds the Claude Code configuration for this repo: `CLAUDE.md`, `.mcp.json`,
`.claude/settings.json` (permissions and hooks), the hook scripts, four skills and three
reviewer subagents. **It is deliberately inert.** The folder and file names (`claude/` instead
of `.claude/`, `*.staged`) keep Claude Code from loading any of it.

Why it is staged instead of installed: it was written by an agent in an automated workflow.
Permission rules, hooks and agent instructions change what every Claude Code session in this
repo may do, and Claude Code applies edits to `.claude/settings.json` to running sessions
immediately (and loads `.mcp.json` servers without a prompt in SDK and `-p` runs). That kind
of change needs the owner's own decision, not an agent's, so it waits here for review.

## Review, then install

```bash
cd /path/to/AImart
bash proposed-agent-config/claude/hooks/test_hooks.sh    # expect: 61 passed, 0 failed
mkdir -p .claude
cp -R proposed-agent-config/claude/. .claude/
cp proposed-agent-config/CLAUDE.md.staged CLAUDE.md
cp proposed-agent-config/mcp.json.staged .mcp.json
chmod +x .claude/hooks/*.py .claude/hooks/test_hooks.sh
bash .claude/hooks/test_hooks.sh                          # same result from the real location
rm -r proposed-agent-config
```

Then delete the "Pending install" note in `AGENTS.md` (section "Agent tooling").

## What is in it

| path after install | what it does |
|---|---|
| `CLAUDE.md` | `@AGENTS.md` import plus Claude-Code-only notes |
| `.mcp.json` | dogfoods the free local stdio server: `uv run --frozen aimart-parquet mcp` |
| `.claude/settings.json` | allow: `make lint/type/test/check/fmt/help`, venv and `uv run --frozen` pytest/ruff/mypy, `git status/diff/log/show`. ask: `git push`, `git commit`, `gcloud run deploy`, testnet pytest, `uv publish`. deny: force-push, `git clean -fdx`, `rm -rf /` and `~`, bare `env`/`printenv`, reading or editing `.env*` (except `.env.example`), `.wallets/`, `.local/`, `*.pem`, `*.key`, `~/.ssh` |
| `.claude/hooks/guard_bash.py` | PreToolUse on Bash: blocks commands that print secrets, spend real money or destroy work (details in its docstring); exits 2 to block, also when it crashes |
| `.claude/hooks/format_python.py` | PostToolUse on Edit/Write/MultiEdit: `ruff check --fix` + `ruff format` on the edited `.py` file inside the repo; always exits 0; reports leftover violations to Claude |
| `.claude/hooks/test_hooks.sh` | pipes sample hook JSON into both hooks: 51 guard cases, 10 formatter checks |
| `.claude/skills/*/SKILL.md` | `add-input-format`, `add-paid-operation`, `release-and-deploy`, `verify-payments` |
| `.claude/agents/*.md` | `payments-reviewer`, `security-reviewer`, `quantum-honesty-reviewer` |

Notes:

- The hooks run under the system `python3` (3.9+ compatible, no third-party imports). If
  `python3` is missing the guard cannot run and Claude Code treats that as a non-blocking
  error, so the guard fails open; the `deny` rules still apply. Claude Code's `onFailure:
  "block"` field would close that gap but is not in the published settings schema yet.
- The hook commands use `$CLAUDE_PROJECT_DIR`, so they work from any working directory.
- Formats checked against code.claude.com/docs (hooks, settings, permissions, MCP, subagents,
  skills) on 2026-10-17; `settings.json` validates against
  https://json.schemastore.org/claude-code-settings.json.
- `MultiEdit` in the matcher is a legacy tool name; it is harmless if the tool no longer exists.
