# 0004: Host on Cloud Run's free tier with a hard spend cap

- Status: superseded as the default host by Modal on 2026-11-08 (see the amendment at the end);
  Cloud Run stays supported with `DEPLOY_HOST=cloudrun`
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

## Amendment (2026-11-08): Modal is the default host

The owner cannot use Google Cloud or AWS. Of the hosts with a free allowance large enough for a
2 GiB job (most free container tiers give 512 MB and a tenth of a CPU), Modal's Starter plan
fits without a card: $30 of compute credits a month, scale to zero, a workspace budget that
stops it instead of billing. `deploy/modal_app.py` and `deploy-modal.yml` deploy it; a release
goes there unless `DEPLOY_HOST=cloudrun`. Differences that matter:

- Modal answers a web request still running after 150 s with a 303 redirect, so the paid
  timeouts are tighter there (60 s per conversion, 30 s of downloads per call, 15 s of queue),
  checked by `tests/unit/test_modal_deploy.py`. Cloud Run's 300 s window is unchanged.
- Outputs live on a Modal Volume, so download links outlive a scale-down; each call's temporary
  files stay on local disk (`AIMART_WORK_DIR`). On Cloud Run without a bucket they lived in RAM.
- Modal bills a container while it runs (about $0.06 an hour at 1 core and 2 GiB), not per
  request: idle time after a call (2 minutes) costs credits too.
- The Starter plan has no rollbacks: a failed deploy is fixed forward or redeployed from the
  previous release's tag.

