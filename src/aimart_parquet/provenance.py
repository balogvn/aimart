"""Post-quantum signed provenance for every artifact we produce.

An agent that receives a Parquet file from a paid service has one question we can answer
cryptographically: "is this exactly what that service produced?" Each artifact gets a manifest
(hash, size, operation, time, service version) signed with ML-DSA-65 — NIST FIPS 204, a lattice
signature designed to stay secure against quantum computers. Verification needs only our
public key, published at ``/.well-known/aimart-signing-key``.

What this does NOT do, stated plainly because the product discussion suggested otherwise: it
does not make the data "true", detect prompt injection inside cell values, or involve quantum
hardware. It proves origin and integrity — no more.

The manifest is canonical JSON (sorted keys, no whitespace), so any language can re-serialize
and verify it. Signatures use a context string for domain separation, so a signature made here
can never be replayed as a signature over some other protocol's message.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import logging
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.mldsa import MLDSA65PrivateKey, MLDSA65PublicKey

from aimart_parquet import __version__

log = logging.getLogger("aimart.provenance")

ALGORITHM = "ML-DSA-65"
CONTEXT = b"aimart-parquet/provenance/v1"
SEED_BYTES = 32


def canonical(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


class Signer:
    def __init__(self, seed: bytes | None = None) -> None:
        if seed is None:
            # A per-boot key still proves integrity within this process's lifetime, but every
            # signature becomes unverifiable after a restart. Production must set the seed.
            self._key = MLDSA65PrivateKey.generate()
            self.ephemeral = True
            log.warning(
                "AIMART_SIGNING_KEY_MLDSA is not set: using an ephemeral signing key; "
                "provenance signatures will not verify after a restart"
            )
        else:
            if len(seed) != SEED_BYTES:
                msg = f"ML-DSA seed must be {SEED_BYTES} bytes, got {len(seed)}"
                raise ValueError(msg)
            self._key = MLDSA65PrivateKey.from_seed_bytes(seed)
            self.ephemeral = False
        self.public_key: MLDSA65PublicKey = self._key.public_key()

    @classmethod
    def from_b64(cls, value: str | None) -> Signer:
        if not value:
            return cls(None)
        try:
            return cls(unb64(value.strip()))
        except (binascii.Error, ValueError) as exc:
            msg = "AIMART_SIGNING_KEY_MLDSA must be base64 of a 32-byte seed"
            raise ValueError(msg) from exc

    def public_key_b64(self) -> str:
        return b64(self.public_key.public_bytes_raw())

    def sign(self, manifest: dict[str, Any]) -> str:
        return b64(self._key.sign(canonical(manifest), CONTEXT))

    def public_document(self, base_url: str) -> dict[str, Any]:
        return {
            "algorithm": ALGORITHM,
            "standard": "NIST FIPS 204",
            "public_key": self.public_key_b64(),
            "encoding": "base64 of the raw 1952-byte ML-DSA-65 public key",
            "context": CONTEXT.decode(),
            "message": "canonical JSON of the manifest: sorted keys, separators (',', ':'), UTF-8",
            "ephemeral": self.ephemeral,
            "verify_endpoint": f"{base_url}/v1/verify",
        }


def build_manifest(
    *, artifact_id: str, sha256: str, size_bytes: int, filename: str, operation: str
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "filename": filename,
        "operation": operation,
        "created_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "service": "aimart-parquet",
        "version": __version__,
    }


def verify(public_key_b64: str, manifest: dict[str, Any], signature_b64: str) -> bool:
    try:
        key = MLDSA65PublicKey.from_public_bytes(unb64(public_key_b64))
        key.verify(unb64(signature_b64), canonical(manifest), CONTEXT)
    except (InvalidSignature, ValueError, binascii.Error):
        return False
    return True
