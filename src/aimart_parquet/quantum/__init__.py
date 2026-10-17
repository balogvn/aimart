"""Premium quantum tier (PennyLane). Optional: install the ``quantum`` extra.

Read docs/specs/quantum-premium.md first — especially the honesty contract: these tools run on
simulators, always ship a classical baseline, and never claim an advantage they do not have.
"""

from __future__ import annotations

import importlib.util


def available() -> bool:
    return importlib.util.find_spec("pennylane") is not None
