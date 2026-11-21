"""``python -m aimart_actor``: the Actor's entry point (the Dockerfile's CMD)."""

import asyncio

from aimart_actor.actor import main

# The guard is load-bearing: conversions run in a forkserver child, which re-imports __main__
# (AGENTS.md, "The forkserver re-imports `__main__`"); unguarded, the child would start the
# Actor again.
if __name__ == "__main__":
    asyncio.run(main())
