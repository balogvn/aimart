# AImart Parquet: messy files to clean Parquet, plus SQL

**AImart Parquet** turns messy data files into **clean, typed Parquet** and, if you ask, answers a
**read-only SQL query** over each file. Give it links to CSV, Excel, SPSS, Stata, SAS or JSON
files, or tables in digital PDFs; get back one Parquet file per input, a report of every column's
type, nulls and rejected values, and your query's rows.

It is built for **AI agents and data pipelines**: a 100,000-row CSV of 4.2 MB became 0.6 MB of
typed Parquet, and its schema digest is about 59 tokens where reading the CSV is about a
million. It is the same engine as the open-source
[`aimart-parquet`](https://github.com/balogvn/aimart) package and the hosted x402 service.

## What can it convert?

| input | details |
|---|---|
| CSV / TSV | delimiter, header, encoding (UTF-8, UTF-16, cp1252, Latin-1...) and number style (`1,234.5` or `1.234,5`) detected |
| JSON / NDJSON | arrays of records, a records path inside a document (`data.items`), nested objects kept or flattened |
| Excel | xlsx, xls, ods; pick a sheet by name or number |
| SPSS, Stata, SAS | sav, zsav, por, dta, sas7bdat, xpt; variable and value labels kept |
| PDF | tables in digital (not scanned) PDFs, across pages |
| Parquet | column names normalized, text columns re-typed, schema validated |

Inputs may be gzip, bz2 or zip compressed. Text columns become integers, decimals, booleans,
dates and timestamps when every value fits; the report says what was inferred and shows up to
three values that did not fit.

## How to use it

1. Put one or more **public links** in **File links** (a pre-signed S3 or Google Cloud Storage
   link works), or paste small files as base64 in **Inline files**.
2. Optionally set **Conversion options** (e.g. `{"sheet": "2025", "sort_by": ["date"]}`) and a
   **SQL query** over the table `data`.
3. Run it. Each input gets one row in the **Output** tab: its Parquet download link, row count,
   column types and the query result, or the error and what to do about it.

### Input example

```json
{
    "urls": ["https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.csv"],
    "sql": "SELECT region, sum(units * unit_price) AS revenue FROM data GROUP BY region ORDER BY revenue DESC"
}
```

### Output example

One dataset item per input (the full `report` is shortened here):

```json
{
    "index": 1,
    "input": "https://raw.githubusercontent.com/balogvn/aimart/main/docs/examples/sales.csv",
    "status": "converted",
    "fileName": "sales.parquet",
    "parquetKey": "parquet-001-sales.parquet",
    "parquetUrl": "https://api.apify.com/v2/key-value-stores/<storeId>/records/parquet-001-sales.parquet?signature=<signature>",
    "sizeBytes": 1022,
    "sourceFormat": "csv",
    "rows": 6,
    "columns": [
        {"name": "order_id", "type": "int64"},
        {"name": "order_date", "type": "date32[day]"},
        {"name": "region", "type": "string"},
        {"name": "units", "type": "int64"},
        {"name": "unit_price", "type": "double"}
    ],
    "warnings": [],
    "report": {"source_format": "csv", "encoding": "utf-8", "delimiter": ",", "rows": 6, "columns": ["..."]},
    "sql": "SELECT region, sum(units * unit_price) AS revenue FROM data GROUP BY region ORDER BY revenue DESC",
    "query": {
        "columns": ["region", "revenue"],
        "types": ["string", "double"],
        "rows": [["North", 551.88], ["South", 282.41999999999996], ["West", 245.0], ["East", 139.92999999999998]],
        "row_count": 4,
        "truncated": false
    },
    "charged": ["file-converted", "query-run"]
}
```

A failed input looks like this, and costs nothing:

```json
{
    "index": 2,
    "input": "file:broken.xlsx",
    "status": "failed",
    "error": {"code": "invalid_input", "message": "...", "hint": "..."},
    "charged": []
}
```

The Parquet files are also listed under **Storage > Key-value store** (keys starting with
`parquet-`). The `parquetUrl` link downloads the file without an Apify account.

## How much does it cost?

Pay per event, nothing else:

| event | price | when |
|---|---|---|
| `file-converted` | **$0.02** per file | a file was converted and its Parquet stored |
| `query-run` | **$0.005** per query | your SQL ran successfully over one converted file (even if it matched no rows) |

So converting 100 files costs $2.00, and converting and querying them $2.50. **Failed inputs are
never charged**: a link that cannot be fetched, a file that cannot be read, an invalid option or
a query that fails each produce an item with the error, and no charge. A query that fails does
not undo the conversion's charge, since the Parquet file was delivered.

You control the most a run may cost (**Max cost per run** in the run options, or
`maxTotalChargeUsd` through the API and MCP). The Actor checks the remaining budget before every
file: when it cannot pay for the next one, that file and the rest are marked `skipped`
(`charge_limit_reached`), uncharged, and the run ends normally.

If Apify moves your run to another server mid-way, or you resurrect a finished run, the Actor
picks up where it stopped: inputs already reported are not converted, charged or reported again.

## For AI agents

- **Apify MCP server** (`https://mcp.apify.com`): call this Actor with the `call-actor` tool
  (put `maxTotalChargeUsd` in `callOptions`), then read the result with `get-dataset-items`
  using the run's `defaultDatasetId`. The input fields above are the tool's arguments; every
  field's description says what it accepts.
- **No Apify account?** Agents can buy a prepaid, spend-capped Apify token with x402, MPP or
  Skyfire at [agi.apify.com](https://agi.apify.com) and run this Actor with it (it uses limited
  permissions, which prepaid tokens require).
- **Paying in USDC directly?** The same converter runs as an x402 service: pay per call in USDC
  on Base, no account at all. REST and remote MCP at
  `https://balogvn--aimart-parquet.us-east.modal.run` (MCP at `/mcp`, pricing at `/pricing`,
  agent notes at `/llms.txt`), at the same prices.

## Data handling

- Each input is downloaded once into the run's container, converted there, and the container is
  discarded when the run ends. Only the Parquet files (in the run's key-value store) and the
  dataset items stay, in **your** Apify account, for as long as your plan keeps run storages
  (unnamed run storages are deleted automatically after your plan's data retention period).
- The Actor runs with **limited permissions**: it can read and write only its own run's storages,
  nothing else in your account.
- Links must be public: private and local network addresses (and cloud metadata addresses) are
  refused, and every redirect is checked again. Files on the machine running the Actor cannot be
  named as inputs.
- Logs record errors by type only, never file contents or query results.
- The run's key-value store also holds one small `progress-NNN` record per input (its status and
  the events charged), so that a restarted run does not charge you twice. While an input is
  being charged, its record briefly holds that input's result.

## Limits

- Up to 20 links and 10 inline files per run; each file up to 50 MB (after download; up to
  256 MB decompressed).
- Each conversion has 120 seconds, each query 15 seconds; a query returns at most 1,000 rows
  (`queryLimit`, default 100).
- If failed inputs use more than 120 seconds of a run in total (usually one file that hits the
  conversion limit), the inputs after them are not started. They are reported as `skipped`
  (`failure_budget_reached`), uncharged; run them again in a new run.
- Scanned PDFs (images of tables) are not read: there is no OCR.
- SQL is DuckDB's dialect: one `SELECT` (CTEs, `DESCRIBE`, `SUMMARIZE` too) over `data`.

## FAQ

**My file is private.** Use a pre-signed link (S3, Google Cloud Storage, Azure SAS) that works
without signing in, or send it inline as base64 if it is small.

**An input failed. What now?** Read its `error.hint`: it says what to change (for example which
option to set, such as `sheet` or `encoding` in **Conversion options**). You were not charged
for it.

**Can I keep the Parquet file?** Download it from `parquetUrl`, or copy it from the run's
key-value store before your plan's retention period ends.

**Is it open source?** Yes, Apache-2.0: [github.com/balogvn/aimart](https://github.com/balogvn/aimart).
For local files, `uvx aimart-parquet mcp` runs the same converter as a free local MCP server.
