# Architecture decision records

Each record follows [MADR](https://adr.github.io/madr/): the context, the options considered,
the decision, and its consequences. A record is never edited to change its decision. To change
a decision, write a new record that supersedes the old one, and set the old one's status to
`superseded by NNNN`.

| ADR | decision | status |
|---|---|---|
| [0001](0001-python-and-uv.md) | Python 3.12+ managed with uv | accepted |
| [0002](0002-x402-v2-rest-middleware-and-mcp-extension.md) | x402 v2: SDK middleware for REST, our own MCP extension, lazy facilitator init | accepted |
| [0003](0003-facilitators-and-price-floors.md) | Free keyless facilitators by default; $0.002 price floor | accepted |
| [0004](0004-hosting-cloud-run.md) | Host on Cloud Run's free tier with a hard spend cap | accepted |
| [0005](0005-job-isolation-forkserver.md) | Heavy jobs in forkserver children; BLAS pinned to one thread; a memory cap per job | accepted |
| [0006](0006-duckdb-copy-writer.md) | DuckDB `COPY` as the default Parquet writer | accepted |
| [0007](0007-artifact-references-not-bytes.md) | Return artifact references, not file bytes | accepted |
| [0008](0008-quantum-tier-honesty.md) | Quantum tier: simulator disclosed, classical baseline always | accepted |
| [0009](0009-post-quantum-provenance.md) | ML-DSA-65 provenance signatures on every hosted artifact | accepted |

New record: copy the structure of an existing one, take the next number, and add it to this
table.
