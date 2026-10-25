# 0007: Return artifact references, not file bytes

- Status: accepted
- Date: 2026-10-17 (built in commit 40b6380; recorded 2026-10-18)

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
  that evicts the oldest artifacts first. Links are HMAC-SHA256 signed and expiring (`exp`,
  `sig`).
- `GcsArtifactStore`: for Cloud Run, where local disk vanishes on scale-to-zero (ADR 0004).
  Downloads are V4 signed URLs served by GCS, with a lifecycle rule as the cleanup backstop.

**Amendment (review of 6f99145).** The TTL only made artifacts unreachable, while the
published retention promised deletion. Expired artifacts and their GCS blobs are now deleted
by a request-path sweep (at most once a minute); a bucket-listing sweep (at most every 6 h)
catches blobs whose local record a restart lost, and the 1-day lifecycle rule is the last
resort, which bounds the worst case at about a day ([privacy.md](../privacy.md)).

Local stdio mode writes the file next to the input, or to `output_path`, and returns its
`path`.

### Consequences

- Good: compact results, cheap chaining of calls, and free downloads.
- Good: one `_publish` path for every output, which is also where provenance is signed.
- Bad: artifacts expire. An agent that comes back after the TTL gets `artifact_not_found` and
  must convert again. The error's hint says so.
