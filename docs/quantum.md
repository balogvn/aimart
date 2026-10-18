# The premium quantum tier

> **Read this first.** Both premium tools run quantum circuits on a **simulator** (PennyLane
> on an ordinary CPU), not on quantum hardware. At the sizes a web request can simulate, a
> simulator gives **no speed or accuracy advantage** over classical methods, and we do not
> claim one. Every result therefore ships a classical answer computed in the same call, with
> a metric you can compare, and `notes[0]` always says so:
>
> *"Computed on a quantum circuit simulator (PennyLane); at this size a simulator gives no
> speed or accuracy advantage over classical methods, so a classical baseline is included."*

What you are paying for is a correct, ready-to-use classical answer (exact, in the portfolio
case) together with a quantum-method result and a fair comparison of the two, in a single
call. You do not need PennyLane installed, and the result works as a reproducible
experiment. The contract that every change must keep is in
[specs/quantum-premium.md](specs/quantum-premium.md).

Both tools take a Parquet input: an `artifact_id` from `convert_to_parquet`, or a `url` to a
`.parquet` file. Both run in the isolated worker with a 90 s deadline
(`AIMART_QUANTUM_TIMEOUT_S`), and like every paid call they are charged only on success.

## `quantum_compress_features`: quantum-kernel PCA ($0.50)

REST: `POST /v1/quantum/compress`.

**What it does.** It reduces many numeric columns to `components` dimensions (1–8):

1. It standardizes the features. If there are more features than `max_qubits` (2–12, default
   8), it pre-reduces them with classical PCA and reports that in `method.pre_reduction`.
2. It embeds each row as a quantum state with PennyLane's `IQPEmbedding` (a ZZ-entangling
   feature map), simulated on `default.qubit`.
3. It compares rows by state fidelity, `K(x, y) = |⟨ψ(x)|ψ(y)⟩|²`, on a random sample of
   `landmarks` rows (16–1024, default 256). It centres and eigendecomposes the kernel, then
   projects every row (Nyström extension).
4. It picks the **bandwidth** (the angle scale) automatically. Too large a bandwidth makes
   every pair of rows look unrelated, which is kernel concentration. Too small, and the map
   degenerates toward classical PCA. The automatic choice targets a median off-diagonal
   kernel value of about 0.75, reported as `method.median_kernel`. You can set it yourself
   with `bandwidth`.

**Output.** A new Parquet artifact, signed like every hosted artifact, with your
`keep_columns`, then `qpc_1..k` (quantum) and `pc_1..k` (classical PCA on the same
standardized features) side by side. The result also includes:

- `neighbor_preservation_quantum` and `neighbor_preservation_classical`: **the comparable
  metric**. For sampled rows, it is the mean share of each row's 10 nearest neighbours (in
  the standardized input) that are still among its 10 nearest in the reduced space. Higher
  is better.
- `quantum_explained_variance` (share of *kernel-space* variance) and
  `classical_explained_variance` (share of *input-space* variance). These two are **not
  comparable** with each other, and the field descriptions say so.
- `method`: feature map, kernel, device, qubits, landmarks, rows, rows dropped for nulls,
  bandwidth, median kernel.

**Measured behaviour** (synthetic data):

| data | classical PCA | quantum kernel PCA |
|---|---:|---:|
| linear latent factors (the owner's benchmark) | 0.77 | 0.26 |
| linear latent factors (independent re-run: 600 rows, 2 factors → 8 features, 2 components) | 0.96 | 0.80 |
| concentric circles (the owner's benchmark) | roughly equal: 0.21 and 0.19 | |
| concentric circles (independent re-run: 600 rows, 6 features, 2 components) | 0.21 | 0.21 |

On data with linear structure, classical PCA preserves neighbourhoods far better, as theory
predicts. On non-linear structure such as concentric circles, the two are roughly equal in
these runs. Neither is "the quantum one wins".

**When to use which output.** Use the columns with the higher `neighbor_preservation_*`
score for your data. In practice that is usually `pc_*`. Use `qpc_*` when it scores higher on
your data, or when you are studying quantum kernels and want a reproducible run with a fair
baseline.

**Limits.** Numeric columns only; rows with nulls in the chosen columns are dropped and
counted. At most 12 qubits, 1,024 landmarks, and 200,000 projected rows (`max_rows`). The
quantum part is an approximation (landmark sampling, set by `seed`), so results change with
`seed` and `landmarks`.

## `quantum_optimize_portfolio`: QAOA portfolio selection ($1.00)

REST: `POST /v1/quantum/portfolio`.

**What it does.** It chooses exactly `budget` assets out of up to **14** (default: half,
rounded up) to minimize `risk_aversion · variance − expected return` of the equal-weight
basket. Expected returns and covariance come from your price columns (simple returns are
computed), or from return columns with `data_kind: "returns"`.

- **Quantum:** QAOA with an **XY mixer**, which preserves Hamming weight. The circuit starts
  from the uniform superposition over feasible baskets (exactly `budget` assets), so the
  circuit never leaves the feasible set. The settings are `layers` p (1–4, default 2) and
  `steps` (10–200 Adam iterations, default 60), simulated on `lightning.qubit`. The answer is
  the best feasible basket among the most probable outcomes.
- **Classical:** exhaustive search over all C(n, budget) baskets. This is **exact**: it is
  the true optimum, not a heuristic.

**Output.** `quantum` and `classical_optimum` (each with assets, objective, expected return,
volatility, and for quantum the probability), `approximation_ratio` (1.0 = QAOA found the exact
optimum, 0.0 = the worst valid basket), `optimum_probability` against `uniform_probability`,
and `method` (algorithm, device, qubits, layers, steps, candidates considered, periods).

**Measured behaviour.** The owner's benchmark (6–14 synthetic assets, p = 2, 60 steps)
found an approximation ratio of **0.96–1.00**, with the probability of the exact optimum
**25–95×** higher than uniform random choice. Treat the enrichment range as describing the
larger sizes only. Enrichment cannot exceed C(n, budget): with 6 assets and a budget of 3
there are only 20 baskets, so even a certain answer is at most 20× uniform.

An independent re-run on new synthetic data used 120 periods of random-walk prices, the
default budget (n/2), p = 2 and 60 steps, run in-process on an Apple-silicon Mac:

| assets | baskets C(n, n/2) | approximation ratio | P(optimum) | enrichment over uniform | wall time |
|---:|---:|---:|---:|---:|---:|
| 6 | 20 | 1.000 | 0.29 | 5.8× | 2.4 s |
| 8 | 70 | 1.000 | 0.25 | 17.8× | 2.0 s |
| 10 | 252 | 1.000 | 0.20 | 51.1× | 2.9 s |
| 12 | 924 | 1.000 | 0.037 | 33.9× | 5.4 s |
| 14 | 3,432 | 1.000 | 0.037 | 127.9× | 11.2 s |

QAOA concentrates probability on good baskets, and here it always ended on the optimum. But
exhaustive search finds the exact optimum anyway, and at this size it is fast. Your data will
differ: read `approximation_ratio` on every result, never these figures.

**When to use which output.** If you need the best basket, use `classical_optimum`. It is
exact, and the tool description says so. Use `quantum`, `approximation_ratio` and
`optimum_probability` to study how well QAOA does on your data.

**Limits.** At most 14 assets: exhaustive search and state-vector simulation both grow
exponentially, and 14 keeps a call within the deadline. Equal weights only (selection, not
weight optimization). Returns are estimated from history, which is not a forecast. This is
not investment advice.

## What we do not claim

- No quantum advantage, no speed-up, and no "better accuracy" from simulation.
- No quantum hardware.
- Nothing to do with security. The post-quantum signature on artifacts (ML-DSA-65, see the
  [README](../README.md#post-quantum-provenance)) is classical cryptography designed to resist
  future quantum computers. It proves origin and integrity of a file, and it is unrelated to
  these tools. The product discussion's "quantum-kernel tamper-proofing" is not a real
  technique ([ADR 0008](adr/0008-quantum-tier-honesty.md)).
