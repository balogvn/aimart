from aimart_parquet.cli import main

# The guard is load-bearing: job isolation uses a forkserver, which re-imports __main__ in
# the worker. Unguarded, every conversion would start a second copy of the CLI.
if __name__ == "__main__":
    raise SystemExit(main())
