# 0008: Quantum tier: simulator disclosed, classical baseline always

- Status: accepted
- Date: 2026-10-17 (built in commit d909ef1; recorded 2026-10-18)

## Context and problem statement

The 13 October 2026 product discussion proposed a PennyLane-powered premium tier at
$0.50–$2.00 per call, plus "quantum-resistant tamper-proof metadata via quantum kernels". At
the sizes a request handler can simulate (up to about 16 qubits), simulated quantum circuits
offer **no computational advantage** over classical methods. Selling them as faster or better
would be false, and agents and their operators would rightly stop trusting the listing.

## Considered options

1. Do not ship a quantum tier.
2. Ship it with quantum-advantage marketing. Rejected as dishonest.
3. **Ship it honestly**, as a convenience and an experiment with a fair comparison built in.

## Decision outcome

Option 3, under a contract that every change must keep
([specs/quantum-premium.md](../specs/quantum-premium.md)):

1. Every result says it ran on a **simulator** and names the device. The simulator note is
   always `notes[0]`.
2. Every result ships an **exact or standard classical baseline**, computed in the same call,
   with a **comparable metric**: neighbour preservation for kernel PCA; the exact optimum and
   approximation ratio for QAOA.
3. Nothing claims speed-ups, "quantum advantage", or security properties it does not have.

"Tamper-proofing via quantum kernels" is **not a real technique**. The real technology for
that goal is **post-quantum cryptography**, so artifacts are signed with ML-DSA-65 instead
(ADR 0009).

The tools exist only when the `quantum` extra is installed (`pricing.active_operations()`), so
an installation never advertises a tool it cannot serve.

### Consequences

- Good: the listing is truthful. Measurements are published even when the classical method
  wins, which it does on linear data ([quantum.md](../quantum.md)).
- Good: the agent always gets a verified answer, whichever method wins.
- Bad: the premium tier's value is convenience and the comparison, not superior results, so
  adoption is uncertain ([economics.md](../economics.md)).
- Changes to the quantum tools are reviewed against the `quantum-honesty-reviewer`
  checklist.
