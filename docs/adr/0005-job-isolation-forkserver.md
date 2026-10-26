# 0005: Heavy jobs in forkserver children; BLAS pinned to one thread; a memory cap per job

- Status: accepted
- Date: 2026-10-17 (built in commit 40b6380; BLAS amendment in d909ef1; recorded 2026-10-18;
  memory cap amendment in 9bdf17b and e8d228e, recorded 2026-10-26)

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
  type (`Internal error (KeyError).`, HTTP 500, unpaid): the text can carry server paths.

Consequences of the cap:

- Good: one hostile input costs one failed, unpaid call, not the instance.
- Bad: **large legitimate inputs are refused too.** CSV conversion peaks at about 3.5 times the
  decompressed CSV size (measured at e8d228e: a 200 MB CSV peaked at about 706 MB resident, a
  450 MB one at about 1.4 GB). With 700 MB per job on the default 2 GiB instance, CSVs above
  roughly 180-200 MB decompressed are refused with 413 (unpaid), well below
  `max_decompressed_bytes` (512 MB). Files with many short cells per row peak higher: in one
  measurement a 141 MB CSV of 40 short columns peaked near 970 MB in-process. Measure on Linux
  before raising the cap, `max_concurrent_jobs` or the instance size.
- Bad: a job allocating as fast as it can gains a few hundred MB between two polls; the default
  leaves that headroom. DuckDB's own `memory_limit` (1 GB by default) is above the cap, so the
  watchdog can stop a job before DuckDB would spill to disk.
- Tests of the cap must keep touching their allocation: on a loaded Mac, the pages of a child
  that allocated and stopped were paged out before the parent's poll saw them.

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
