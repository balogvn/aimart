# 0001: Python 3.12+ managed with uv

- Status: accepted
- Date: 2026-10-17 (built in commit 40b6380; recorded 2026-10-18)

## Context and problem statement

The product's edge is reading the formats other converters skip: SPSS (`.sav`, `.zsav`,
`.por`), Stata (`.dta`) and SAS (`.sas7bdat`, `.xpt`), with their variable and value labels.
It also needs a fast columnar engine, Excel readers, a Parquet writer, an x402 SDK and an MCP
SDK. Which language and toolchain give us all of that with the least glue?

## Considered options

1. **Python**: pyreadstat (ReadStat bindings) for SPSS, Stata and SAS; DuckDB; pyarrow;
   python-calamine; the x402 Python SDK; the MCP Python SDK.
2. **TypeScript/Node**: the most mature x402 packages (including `@x402/mcp`), but no
   maintained equivalent of ReadStat with label support.
3. **Rust or Go**: fast and small, but statistical-format support and both SDKs would be
   ours to build.

For the toolchain: **uv** (lockfile with hashes, fast, Python-version management), Poetry, or
pip-tools.

## Decision outcome

We chose **Python 3.12+** (the repo pins 3.13) and **uv**. `uv.lock` pins every dependency
with hashes. Every command runs through `uv run --frozen`, so a lockfile change is always a
deliberate edit and never a side effect.

### Consequences

- Good: every format and both SDKs are native. Later, PennyLane and `cryptography`'s ML-DSA
  were also only an import away (ADR 0008, ADR 0009).
- Good: one command (`make setup`) reproduces the environment exactly, in CI and in the
  Docker image.
- Bad: the Python x402 SDK lags the npm one. Its MCP helpers target mcp 1.x (ADR 0002), and
  it has no Algorand (AVM) mechanism, so only Base (EVM) and Solana (SVM) are offered.
- Bad: native parsers inside one Python process can crash the process, so heavy work runs in
  child processes (ADR 0005).
