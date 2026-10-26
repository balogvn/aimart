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
- Bad: **large legitimate inputs are refused too.** CSV conversion peaks at about 3.5 times the
  decompressed CSV size in resident memory alone (measured at e8d228e: a 200 MB CSV peaked at
  about 706 MB resident, a 450 MB one at about 1.4 GB), and since the final review the
  decompressed file counts too (next amendment): CSVs above roughly 120-150 MB decompressed are
  refused with 413 (unpaid) on the default 2 GiB instance. Files with many short cells per row
  peak higher: in one measurement a 141 MB CSV of 40 short columns peaked near 970 MB
  in-process. Measure on Linux before raising the cap, `max_concurrent_jobs` or the instance
  size.
- Bad: a job allocating as fast as it can gains a few hundred MB between two polls; the default
  leaves that headroom. DuckDB's own `memory_limit` (1 GB by default) is above the cap, so the
  watchdog can stop a job before DuckDB would spill to disk.
- Tests of the cap must keep touching their allocation: on a loaded Mac, the pages of a child
  that allocated and stopped were paged out before the parent's poll saw them.

**Amendment: the cap counts the job's files (final review).** On Cloud Run `/tmp` is RAM inside
the same 2 GiB, and resident memory never shows it. A 1.5 MB gzip CSV sent as `content_base64`
decompressed to 501 MiB in the call's work directory (`artifact_dir/.work/<call>/`) while the
child's RSS climbed to the 700 MB cap on its own; two such calls held about 2.5 GB, enough to
OOM-kill the instance and every other caller's request, and both ended in 413, unpaid. So:

- **The watchdog adds the bytes of the job's files to its RSS** and compares the sum with
  `job_memory_limit_mb`. The job's files are those under its own temp directory and under the
  work directories `Service` passes to `run_isolated` (`job_dirs`): the call's work directory for
  every operation, and an upload's for `convert_upload`. The 413 says both figures ("... 554 MB
  in memory plus 150 MB of temporary files, which count toward the budget.") and its details
  carry `memory_bytes` and `temp_file_bytes`.
- **Measured cheaply on every poll**: an `os.scandir` walk that never follows symlinks and stops
  after 10,000 entries, counting allocated blocks. A file hard-linked from outside those
  directories does not count (a query's input linked from the artifact store takes no new space
  and is in the artifact budget already); a file all of whose links are the job's counts once.
- **`max_decompressed_bytes` dropped from 512 MB to 256 MB.** Measured on macOS through
  `Service.convert` with the 700 MB default (Linux counts about 160 MB more per child): the
  largest inputs that converted were 120 MB of decompressed CSV (8 short text columns; 100 MB
  for a mixed and a one-integer-column shape) and an xlsx of 144 MB of XML; every CSV of 150 MB,
  of all three shapes, and the xlsx of 254 MB of XML failed at the cap. 256 MB therefore refuses
  nothing that could convert, while an input that can only fail now stops at the existing
  "Decompressed input exceeds the 256 MB limit." with half the RAM written, and a zip or xlsx
  over it is refused from its central directory before anything is inflated. Raise the two
  settings together.
- **A crash's log carries no exception text.** The published retention policy says logs never
  hold file contents, and since e8d228e the child's unexpected exception was logged in full,
  which can quote a column name or a cell. The log now has its type and traceback frames only.

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
