# 0007: Return artifact references, not file bytes

- Status: accepted
- Date: 2026-10-17 (built in commit 40b6380; recorded 2026-10-18; residency amendment from
  the final review, recorded 2026-10-26)

## Context and problem statement

A conversion produces a binary Parquet file. MCP tool results and JSON responses are read by
an LLM. Base64 Parquet in a tool result would cost the agent thousands of tokens to carry
around, which defeats the point of Parquet. How does the caller get the file?

## Decision outcome

**By reference.** `convert` stores the file and returns an `artifact` with an unguessable `id`
(`secrets.token_urlsafe(18)`), its `sha256`, `size_bytes`, `expires_at`, a signed
`download_url`, and, on the hosted service, `provenance`. Later calls (`inspect`, `preview`,
`query`, the quantum tools) take `artifact_id`, so the agent never re-sends the file. Over MCP
the download link is also attached as a `resource_link`.

Two stores sit behind one interface (`artifacts.py`):

- `LocalArtifactStore`: disk with a TTL (`AIMART_ARTIFACT_TTL_S`, 1 hour) and a size budget
  that evicts the oldest artifacts first (amended below). Links are HMAC-SHA256 signed and
  expiring (`exp`, `sig`).
- `GcsArtifactStore`: for Cloud Run, where local disk vanishes on scale-to-zero (ADR 0004).
  Downloads are V4 signed URLs served by GCS, with a lifecycle rule as the cleanup backstop.

**Amendment (review of 6f99145).** The TTL only made artifacts unreachable, while the
published retention promised deletion. Expired artifacts and their GCS blobs are now deleted
by a request-path sweep (at most once a minute); a bucket-listing sweep (at most every 6 h)
catches blobs whose local record a restart lost, and the 1-day lifecycle rule is the last
resort, which bounds the worst case at about a day ([privacy.md](../privacy.md)).

**Amendment: the size budget (review of 6f99145).** "Evicts the oldest first" charged callers
for links that died: an output larger than the whole budget was stored, evicted at once and
paid for, and of two concurrent converts that fit alone but not together, the second evicted
the first a moment after its caller paid. Now every stored output counts toward
`AIMART_ARTIFACT_MAX_TOTAL_BYTES`, outputs whose payment is still settling included, and room
is made before an output is stored. Three kinds are never evicted: outputs of a paid call that
has not settled (committed when it settles, deleted when it does not), local outputs delivered
less than 10 minutes ago (`MIN_RESIDENCY_S`, capped at the TTL; on GCS eviction drops only the
cached copy), and artifacts a running call reads (`hold()`; on GCS a join's second table had
evicted its first). An output larger than the whole budget is refused with 413
`input_too_large`, and one that cannot fit without evicting a protected artifact with 503
`busy`. Both happen before the output is stored, so neither is paid for.

**Amendment: a local output lives until its `expires_at` (final review).** The 10-minute
residency was too short for the promise every response makes: `expires_at` is the time the
artifact becomes unreachable. On `LocalArtifactStore` (any deployment without a bucket) a paid,
committed output was evicted for a newer one once it was 600 s old, so its `download_url` and
`artifact_id` 404'd about 49 minutes early, and the 404 blamed the TTL. There the folder is the
artifact, so capacity now evicts only expired outputs (exactly what the sweep would delete), by
their record's `expires_at`; `MIN_RESIDENCY_S` is gone. A store full of live outputs answers 503
`busy` before the new output is stored, unpaid, and the hint says when enough outputs expire
("Retry in about 12 minutes", with `retry_after_s` in the details; "Retry in a minute" when only
outputs still settling hold the space). On GCS nothing changes: eviction drops only the cached
copy, and the blob and its link stay valid until the TTL.

Consequence: on a bucketless deployment a busy hour can fill the budget with live outputs, and
every convert in that time runs to completion and is then refused (503, unpaid) until the oldest
expire. Size `AIMART_ARTIFACT_MAX_TOTAL_BYTES` for an hour of outputs, or use a bucket.

Local stdio mode writes the file next to the input, or to `output_path`, and returns its
`path`.

### Consequences

- Good: compact results, cheap chaining of calls, and free downloads.
- Good: one `_publish` path for every output, which is also where provenance is signed.
- Bad: artifacts expire. An agent that comes back after the TTL gets `artifact_not_found` and
  must convert again. The error's hint says so.
