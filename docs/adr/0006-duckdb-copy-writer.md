# 0006: DuckDB `COPY` as the default Parquet writer

- Status: accepted
- Date: 2026-10-17 (built in commit 40b6380; recorded 2026-10-18)

## Context and problem statement

After reading and type inference, the data sits in a DuckDB relation. On a free tier, CPU
seconds are the budget, and file size is what downstream agents pay to move and scan. How do
we write the Parquet file?

## Considered options

1. Stream Arrow record batches into `pyarrow.parquet.ParquetWriter`.
2. **DuckDB `COPY ... TO ... (FORMAT parquet)`** straight from the relation.

## Decision outcome

**DuckDB `COPY` is the default** (`engine/writer.py`). We measured it at about 4× faster than
streaming Arrow batches into pyarrow, with files about 20% smaller. pyarrow is used only when
columns carry SPSS, Stata or SAS labels, because only pyarrow can write field-level metadata.
Either way, labels are also stored at file level (`aimart.columns`), where DuckDB SQL can read
them with `parquet_kv_metadata()`.

Writer settings, and why:

- zstd level 3: close to gzip's ratio at close to snappy's speed, and every modern reader
  supports it;
- statistics and the page index on, so readers can skip row groups and pages from min/max;
- dictionary encoding on, for codes, categories and names;
- an optional `sort_by`, so min/max statistics prune well.

The conversion report is then built from the footer of the file actually written, not from
what the pipeline intended to write.

### Consequences

- Good: faster conversions and smaller files at no extra cost.
- Bad: two writers to keep equivalent. Round-trip property tests (`tests/unit/test_roundtrip_property.py`) cover the default path; the labelled path is covered by the statistical-reader tests.
