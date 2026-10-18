# 0004: Host on Cloud Run's free tier with a hard spend cap

- Status: accepted (supersedes the Hugging Face Spaces plan from the original handoff)
- Date: 2026-10-17 (built in commit 40b6380; recorded 2026-10-18)

## Context and problem statement

The goal is a hosted service with no running cost at low traffic. The original plan was a
Hugging Face Spaces Docker space on the free CPU tier. Where do we host?

## Considered options

- **Hugging Face Spaces:** no longer a free option for this service, so the plan was dropped.
- **Other "free" hosts:** every viable one asked for a payment card at signup, so "no card"
  was not available anywhere. The real choice was which provider's free tier and spend
  controls to trust.
- **Google Cloud Run:** a monthly free tier (about 50 hours of 1 vCPU request time, 360,000
  GiB-seconds, 2 million requests), scale to zero, container images as they are, Secret
  Manager, and budgets that can enforce a cap.

## Decision outcome

We use **Cloud Run**, with:

- scale to zero, a low `max-instances`, and request-based billing;
- a **hard spend cap**: a billing budget wired to stop spending, so a traffic spike or abuse
  cannot run up a bill. The exact mechanism is in [deploy/README.md](../../deploy/README.md);
- secrets (`AIMART_SIGNING_KEY_MLDSA`, `AIMART_SIGNING_SECRET`) in Secret Manager;
- artifacts in **GCS** (`AIMART_GCS_BUCKET`). Cloud Run's disk is RAM and disappears when an
  instance scales to zero, so a download link handed out a minute earlier would 404. GCS
  also serves downloads as V4 signed URLs, with 100 GB a month of free egress compared with
  Cloud Run's 1 GB. A one-day lifecycle rule is the cleanup backstop.

The image stays portable: plain Docker, `$PORT`, no Cloud Run-only APIs in the app.

### Consequences

- Good: zero cost at low volume. Compute is not the binding cost even beyond the free tier
  (see [economics.md](../economics.md)).
- Good: cold starts are survivable because facilitator initialization is lazy (ADR 0002).
- Bad: a card is on file, so the spend cap is load-bearing. Test it.
- Bad: cold starts add latency to the first call after idle.
