---
name: add-input-format
description: End-to-end checklist for teaching the converter a new input file format (detection, reader, READERS map, fixtures, tests, docs). Use when someone asks to support a new file type or extension, fix detection of a mislabelled format, or add a reader for a format family such as a new statistical package.
---

# Add an input format

A format is not "supported" until detection, reading, the report, the failure path and every
place we advertise formats agree. Work through the list in order; each step names the file.

## 1. Name it

- `src/aimart_parquet/engine/models.py`: add an `InputFormat` member. This file is part of the
  public contract (it becomes OpenAPI and MCP JSON Schema), so the value is a short lowercase
  token such as `"feather"`. Owner-managed file: coordinate before editing.

## 2. Detect it (bytes first, name second)

- `src/aimart_parquet/engine/detect.py`
  - `_EXTENSIONS`: map every extension to the member. `SUPPORTED_EXTENSIONS` is derived from it
    and feeds `/llms.txt` and the service index, so nothing else needs a list.
  - `format_from_magic`: add the signature if the format has one. Magic beats the extension
    because agents mislabel files constantly (a TSV called `.csv`, an xlsx saved as `.xls`).
  - Binary format whose older versions lack a signature? Add it to the "trust the name" set in
    `detect_format`, next to DTA/POR/XPT/SAS7BDAT.
  - Update the `UnsupportedFormat` hint text, which lists what is supported.
- Container formats (zip-based like xlsx/ods): detection may open the archive; it must go
  through the size/ratio checks in `engine/safety.py` first.

## 3. Read it

- Reader lives in `src/aimart_parquet/engine/readers/<family>.py` and implements the `Reader`
  protocol from `engine/readers/__init__.py`:
  `(con, path, workdir, options, fmt, limits) -> RawTable`.
- Load into the DuckDB table `RAW_TABLE` on the connection you are given. That connection is
  sandboxed (`engine/duck.py`): it can only see `workdir`, has no network, and its config is
  locked. Do not open another connection or relax the sandbox to make a reader work.
- Describe each column with `RawColumn`: `infer=True` for text that should climb the type ladder
  in `engine/infer.py`; carry `label` / `value_labels` if the format has them (they are written
  to Parquet metadata and reported by inspect).
- Respect `limits`: stream with `safety.copy_capped` for anything that expands; never read an
  unbounded file into Python memory.
- Raise `InvalidInput` (422) for bytes that do not parse. Every error must be an `AimartError`
  with a concrete `hint`, because a >= 400 status is what guarantees the caller is not charged.

## 4. Register it

- `src/aimart_parquet/engine/convert.py`: add the member to `READERS`. A format missing here
  detects fine and then fails at conversion time, which is the worst order.
- Native parser that can crash or ignore interrupts? Conversions already run in the forkserver
  child (`engine/isolate.py`); add the reader module to `set_forkserver_preload` if its import
  is slow. Owner-managed file: coordinate before editing.
- Heavy dependency? It belongs in an optional extra in `pyproject.toml` (owner decides) and in
  the mypy `ignore_missing_imports` override if untyped.

## 5. Fixtures and tests

- `tests/fixtures/<format>/`: small files (tens of KB). Commit the script that generated them
  next to them, so a fixture can be regenerated rather than mysteriously binary.
- Golden test on the *report* built from the written file: column names, Arrow types, null
  counts, `rejected_count`, labels, warnings. Assert values, not "it returned 200".
- Mislabelled-extension test (right bytes, wrong name) and wrong-bytes test (right name,
  garbage inside) -> `invalid_input`, and over REST that means HTTP 422 with no settlement.
- Compressed variants (`.gz`, `.zip`) and a bomb variant if the format is itself a container.
- Use `Settings(isolate_jobs=False)` in unit tests (fast, visible to coverage). Keep a single
  test that exercises the isolated path for real; any script that triggers isolated jobs needs
  an `if __name__ == "__main__":` guard because forkserver re-imports `__main__`.

## 6. Advertise it (every place, or the docs lie)

- `README.md` supported-formats table.
- `src/aimart_parquet/mcp/server.py`: `INSTRUCTIONS` and the `convert_to_parquet` description
  list formats in prose. Owner-managed file.
- `src/aimart_parquet/api/discovery.py`: `DESCRIPTION` (also used by OpenAPI and the server card).
- `docs/architecture.md` if the reader adds a new technique worth explaining.

## 7. Done when

- `make check` passes (lint, type, tests with coverage >= 90%).
- `.venv/bin/aimart-parquet convert tests/fixtures/<format>/<file>` prints a report you have
  read line by line, and `.venv/bin/aimart-parquet inspect` on the output agrees with it.
