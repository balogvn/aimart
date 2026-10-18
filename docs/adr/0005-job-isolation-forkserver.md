# 0005: Heavy jobs in forkserver children; BLAS pinned to one thread

- Status: accepted
- Date: 2026-10-17 (built in commit 40b6380; BLAS amendment in d909ef1; recorded 2026-10-18)

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
