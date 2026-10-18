# Canonical commands for humans, agents and CI. CI runs the same underlying commands, so a green
# `make check` locally means a green CI run (modulo network and secrets).
#
# Everything goes through `uv run --frozen`: it runs inside the project venv without activating it,
# and --frozen means it never rewrites uv.lock (a lockfile change is a decision, not a side effect).
# Override when needed:  make test UV=/path/to/uv   |   make test RUN=   (use the active venv)

UV ?= uv
RUN ?= $(UV) run --frozen
# Default test selection: no big inputs, no internet, no testnet spending (see pytest markers).
MARKERS ?= not slow and not network and not testnet
IMAGE ?= aimart-parquet:dev
URL ?=
# Extra smoke.py flags, e.g. SMOKE_ARGS="--expect-pay-to 0x... --expect-network eip155:8453"
SMOKE_ARGS ?=

.DEFAULT_GOAL := help
.PHONY: help setup fmt lint type test check serve mcp docker smoke mutate

help: ## List the targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-z][a-z-]*:.*## / {printf "  %-8s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Install the locked dependencies, every extra and the dev group
	$(UV) sync --all-extras --frozen

fmt: ## Format and autofix (rewrites files)
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

lint: ## Check formatting and lint without changing anything
	$(RUN) ruff format --check .
	$(RUN) ruff check .

type: ## mypy --strict over src/
	$(RUN) mypy src

test: ## Tests with branch coverage (fails under 90%); excludes slow/network/testnet
	$(RUN) pytest -q -m "$(MARKERS)" --cov --cov-report=term-missing

check: lint type test ## lint + type + test: the definition of green

serve: ## REST + remote MCP over HTTP on env PORT or 8000 (x402-paid; testnet by default)
	$(RUN) aimart-parquet serve

mcp: ## The free local MCP server over stdio (local file paths, no payments)
	$(RUN) aimart-parquet mcp

docker: ## Build the container image
	docker build -t $(IMAGE) .

smoke: ## Decode live 402s and check prices/payTo/network: make smoke URL=https://<host>
	@test -n "$(URL)" || { echo "usage: make smoke URL=https://<host> [SMOKE_ARGS=...]"; exit 2; }
	$(RUN) python scripts/smoke.py $(URL) $(SMOKE_ARGS)

mutate: ## Mutation-test the money and security paths (slow; see [tool.mutmut])
	$(RUN) mutmut run
