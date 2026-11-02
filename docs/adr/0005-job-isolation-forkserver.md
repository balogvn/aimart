# 0005: Heavy jobs in forkserver children; BLAS pinned to one thread; a memory cap per job

- Status: accepted
- Date: 2026-10-17 (built in commit 40b6380; BLAS amendment in d909ef1; recorded 2026-10-18;
  memory cap amendment in 9bdf17b and e8d228e, recorded 2026-10-26; the cap counting the job's
  files, and crash logs without exception text, from the final review, recorded 2026-10-26)

## Context and problem statement

Untrusted files reach native parsers (DuckDB, ReadStat via pyreadstat, calamine), and
untrusted SQL reaches DuckDB. We found three problems with running them in the server
process:

- **Native code crashes instead of raising.** DuckDB's `read_json` segfaults on JSON nested
  about 21,600 levels deep, which fits in a 50 KB file. In-process, that kills the server and
  everyone else's in-flight paid requests.
- **Threads cannot be killed.** pyreadstat and calamine ignore DuckDB's interrupt. A thread
  timeout only stops waiting, and the CPU burn continues.
- Memory fragmentation from large jobs stays with the server process.

## Considered options

1. Threads with timeouts: no crash isolation, and no real deadline.
2. `spawn` a fresh interpreter per job: isolated, but each job pays seconds of imports
   (pyarrow, DuckDB, PennyLane).
3. **`forkserver`**: a clean server process imports the engine once, and each job is a fork
   of it, which takes milliseconds.

## Decision outcome

We chose **forkserver** (`engine/isolate.py`; `spawn` where forkserver is unavailable). Each
convert, query and quantum job runs in a child that is killed at its deadline. A crash
becomes `InvalidInput` ("the input crashed the parser"), and a deadline becomes `Timeout`.
Both are typed errors, so the call is never settled. The forkserver preloads the engine and
quantum modules, which costs about 2.5 s once instead of on every premium call.

**Amendment: BLAS threads.** On macOS (Accelerate), quantum kernel PCA segfaulted in a worker
on one input and not another. BLAS only goes multi-threaded above a size threshold, and its
thread pools do not survive `fork()`. `isolate.py` now sets `OPENBLAS_NUM_THREADS`,
`OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS` and `NUMEXPR_NUM_THREADS` to 1
before the forkserver starts. Workers run one job on 1–2 vCPUs, so this costs nothing.

**Amendment: a memory cap per job (review of 6f99145).** A 1.6 KB PDF drawing a million glyphs
took a child to 2 GB in 15 s, and a Parquet dictionary bomb in preview, or a query returning
huge strings, does the same. On the 2 GiB Cloud Run instance, which holds the API, the
forkserver and two jobs, one such job took down every in-flight request. So:

- **Every parse of caller data runs in the child**: convert, inspect, preview, query and the
  quantum tools (inspect and preview used to run in-process).
- **The parent polls the child's resident memory** every 100 ms (`VmRSS` in `/proc` on Linux,
  `proc_pidinfo` on macOS) and kills it above `Settings.job_memory_limit_mb`
  (`AIMART_JOB_MEMORY_LIMIT_MB`, default 700; 0 disables; needs `isolate_jobs`). The call fails
  with 413 `input_too_large` ("The job exceeded its memory budget"), so it is not charged. The
  700 MB is sized for 2 GiB: the API (about 250 MB) and the forkserver (about 250 MB) plus two
  jobs. On Linux the figure includes the ~160 MB of preloaded engine a child shares with the
  forkserver; macOS leaves those pages out.
- **We rejected `RLIMIT_AS` and `RLIMIT_DATA`.** Measured on Linux, a child starts at about
  2 GB of virtual memory (DuckDB, Arrow, PennyLane and glibc arenas reserve address space) with
  160 MB resident, and a 1.5 GB `RLIMIT_AS` already failed an ordinary 200k-row CSV. Address
  space is not memory use; resident memory is what fills the instance.
- **Each job has its own temporary directory** (`aimart-job-*`), deleted by the parent once the
  child is gone. A child killed at its deadline or at the cap skips its own cleanup, and a
  query's spill directory holds hard links to (or copies of) its inputs: RAM on Cloud Run.
- **An unexpected exception in the child is logged, not returned.** The caller sees only its
  type (`Internal error (KeyError).`, HTTP 500, unpaid): the text can carry server paths. The
  log gets its type and traceback frames (file, line, function) and never its text either (see
  the last amendment).

Consequences of the cap:

- Good: one hostile input costs one failed, unpaid call, not the instance.
- Bad: **large legitimate inputs are refused too.** At e8d228e CSV conversion peaked at about
  3.5 times the decompressed CSV size in resident memory alone (a 200 MB CSV at about 706 MB
  resident, a 450 MB one at about 1.4 GB; a 141 MB CSV of 40 short columns near 970 MB
  in-process), and once the decompressed file counted too (next amendment) CSVs above roughly
  120-150 MB decompressed were refused with 413 (unpaid) on the default 2 GiB instance. CSV
  memory is now flat and the input caps bind first (the last amendment has the numbers).
  Measure on Linux before raising the cap, `max_concurrent_jobs` or the instance size.
- Bad: a job allocating as fast as it can gains a few hundred MB between two polls; the default
  leaves that headroom. DuckDB's own `memory_limit` (1 GB by default) was above the cap, so the
  watchdog could stop a job before DuckDB would spill to disk; it is now held under the cap
  (last amendment).
- Tests of the cap must keep touching their allocation: on a loaded Mac, the pages of a child
  that allocated and stopped were paged out before the parent's poll saw them.

**Amendment: the cap counts the job's files (final review).** On Cloud Run `/tmp` is RAM inside
the same 2 GiB, and resident memory never shows it. A 1.5 MB gzip CSV sent as `content_base64`
decompressed to 501 MiB in the call's work directory (`artifact_dir/.work/<call>/`) while the
child's RSS climbed to the 700 MB cap on its own; two such calls held about 2.5 GB, enough to
OOM-kill the instance and every other caller's request, and both ended in 413, unpaid. So:

- **The watchdog adds the bytes of the job's files to its RSS** and compares the sum with
  `job_memory_limit_mb`. The job's files are those under its own temp directory and under the
  work directories `Service` passes to `run_isolated` (`job_dirs`): the call's work directory,
  for every operation. The 413 says both figures ("... 554 MB in memory plus 150 MB of
  temporary files, which count toward the budget.") and its details carry `memory_bytes` and
  `temp_file_bytes`.
- **Measured cheaply on every poll**: an `os.scandir` walk that never follows symlinks and stops
  after 10,000 entries, counting allocated blocks. A file hard-linked from outside those
  directories does not count (a query's input linked from the artifact store takes no new space
  and is in the artifact budget already); a file all of whose links are the job's counts once.
- **A convert's input is on the job's disk once.** The engine copies an input that is outside
  its work directory into it (DuckDB reads nothing else), so `convert_upload` writes the upload
  into the work directory the convert then uses (it had a directory of its own, and a 50 MB
  upload counted 100 MB), and a stored artifact given as a convert's input is hard-linked in
  rather than copied, falling back to the copy where a link fails.
- **Streams and workbooks have separate caps.** This amendment first lowered
  `max_decompressed_bytes` to 256 MB, sized to the job cap from single-sheet measurements (with
  the 700 MB default the largest inputs that converted were 120 MB of decompressed CSV and an
  xlsx of 144 MB of XML). That was wrong for workbooks: the excel reader checked the sum of
  every part against the same setting, though calamine parses only the sheet asked for and
  writes none of them to disk, and a 27.5 MB workbook of a 200-row `summary` sheet beside two
  raw sheets of 129.6 MB of XML each, whose `summary` converted at 07fa450, became 413 from the
  zip's directory for every sheet, before any was parsed. Round 2 put the shared setting back
  at 512 MB, which let a stream between about 250 and 500 MB spend a job's whole budget in the
  RAM-backed work directory before its 413. The integration of the final fixes split it:
  `max_decompressed_bytes` (256 MB) caps a gzip/bz2/zip stream, and is checked as it inflates,
  so a 300 MB gzip CSV is refused at 256 MB (0.3 s once the forkserver is warm);
  `max_workbook_xml_bytes` (512 MB) caps the XML parts of an xlsx/ods, from the directory, with
  the hint "Save the sheet you need as its own workbook or as CSV, then send that.". The
  workbook above converts its `summary` sheet with the defaults. The job cap above still bounds
  a stream under 256 MB: its decompressed file is in the work directory and counts. Measured
  on macOS with the defaults, two of the incident's calls at once: each 500 MiB stream and the
  child's 60-80 MiB resident stayed under the cap together (under 600 MiB), and the watchdog
  stopped each job when parsing began, at about 720-900 MiB in all, the overshoot between two
  polls that any job allocating fast gets (see above). At e8d228e the same pair held about
  2.5 GB. An unpaid input of a few KB, such as a PDF drawing glyphs, reaches the cap anyway, so
  the instance has to hold two jobs at the cap whatever these limits are.
- **A crash's log carries no exception text.** The published retention policy says logs never
  hold file contents, and since e8d228e the child's unexpected exception was logged in full,
  which can quote a column name or a cell. The log now has its type and traceback frames only.

**Amendment: CSV memory is flat, and DuckDB is held under the cap (perf/csv-memory, merged at
integrate/final).** Profiling showed that all of a CSV's memory was DuckDB's: the text loaded
into an uncompressed VARCHAR table, 256 MB of read buffers, batches a two-thread ordered COPY
held back, and a hash join for each `IN` list of null tokens. Delimited text is now staged once
as zstd Parquet, with `raw` a view over it, read through one line cap of buffers, on one DuckDB
thread, with `list_contains` for constant lists. `Limits.from_settings` holds DuckDB's
`memory_limit` at the cap minus 300 MB (400 MiB under 700) and its spill cap at the rest of the
cap (300 MiB): spill files are in the directories the watchdog counts, so DuckDB's memory and
its spill together never pass the cap (`within_job_cap`). Past either, DuckDB stops itself,
which is a 413 whose hint names `sort_by` when it was passed, or the watchdog stops the job,
also a 413: never a 500. Measured at 0348e94 in the isolated child with the defaults (macOS on
Apple silicon; peaks of the watchdog's own readings, process and files, in MiB):

| CSV (decompressed) | result | process | files | in all |
|---|---|---|---|---|
| 255 MiB, sent as 39 MB of gzip (the input caps as they are) | converted | 179 | 301 | 481 |
| 48 MiB, 6 columns, 1.0 M rows | converted | 155 | 66 | 222 |
| 191 MiB, 6 columns, 3.9 M rows | converted | 160 | 269 | 430 |
| 135 MiB, 40 random columns, 1.3 M rows | converted | 324 | 211 | 535 |
| 134 MiB, 40 short columns, 1.3 M rows | converted | 320 | 136 | 457 |
| 429 MiB, 6 columns, 8.7 M rows | 413 at the cap | 153 | 547 | 700 |
| 95 MiB with `sort_by` | converted | 329 | 132 | 461 |
| 191 MiB with `sort_by` | 413 at the cap | 539 | 257 | 797 |

All but the first ran with `max_input_bytes` and `max_decompressed_bytes` raised so the file
reached the engine; with the defaults a CSV over 50 MB on the wire, or over 256 MB
decompressed, is refused before. Without the cap the 429 MiB CSV peaked at 161 + 622 MiB and
the sorted 191 MiB one at 546 + 430 MiB. A job's files, not its process, now decide what
converts: about 1.0-1.6 times the CSV (the input, its staged copy and the output). Also at
0348e94: 24 wide CSVs of 100-200 rows and 30-101 columns (dates, timestamps, offsets, zone
names, EU decimals, 12-hour times; the review's repro) each converted with a 200 through REST,
and a 226,303-row CSV, two row groups, read back with its ids 1..N in order.

### Consequences

- Good: a malicious file costs one failed, unpaid call, and the server keeps serving.
- Good: real deadlines, and memory is returned to the OS when each child exits.
- Bad: **any script or test module that triggers isolated jobs while running as `__main__`
  needs an `if __name__ == "__main__":` guard**, because the forkserver re-imports `__main__`.
  Under pytest this works without a guard.
- Bad: arguments and results are pickled across the pipe, so jobs pass paths, not big
  objects.
- Unit tests use `Settings(isolate_jobs=False)` for speed and coverage, and a few tests keep
  the child path honest.
