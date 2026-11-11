"""Create the Modal secret ``aimart-parquet-keys`` once; leave it alone when it exists.

    uv run --no-project --with modal==1.6.3 python deploy/modal_keys.py

It holds AIMART_SIGNING_KEY_MLDSA (the 32-byte ML-DSA-65 seed, base64) and AIMART_SIGNING_SECRET
(the HMAC key for signed download links), generated here as setup-gcp.sh generates them and sent
straight to Modal: never printed, never in argv, never on disk. Re-running never rotates them,
because rotating the ML-DSA key breaks every published signature (deploy/README.md, "Rotating the
ML-DSA signing key"). Needs a Modal token (MODAL_TOKEN_ID/MODAL_TOKEN_SECRET, or `modal token new`).
"""

from __future__ import annotations

import base64
import secrets
import sys

import modal

NAME = "aimart-parquet-keys"  # deploy/modal_app.py KEYS_SECRET


def main() -> int:
    try:
        existing = modal.Secret.objects.list()
    except modal.exception.AuthError as exc:
        # The first deploy stopped here with a bare traceback ending "Token validation failed".
        print(
            f"modal_keys: Modal rejected the token ({exc}). Create a new API token in Modal's "
            "settings (API Tokens, New Token) and store its two pieces as MODAL_TOKEN_ID (it "
            "starts with ak-) and MODAL_TOKEN_SECRET (it starts with as-): deploy/README.md, "
            "section 0.2.",
            file=sys.stderr,
        )
        return 3
    if any(s.name == NAME for s in existing):
        print(f"secret {NAME} exists: left unchanged")
        return 0
    modal.Secret.objects.create(
        NAME,
        {
            "AIMART_SIGNING_KEY_MLDSA": base64.b64encode(secrets.token_bytes(32)).decode(),
            "AIMART_SIGNING_SECRET": secrets.token_urlsafe(48),
        },
    )
    print(f"created secret {NAME} (values not shown)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
