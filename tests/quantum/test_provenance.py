from __future__ import annotations

import base64

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.mldsa import MLDSA65PublicKey

from aimart_parquet import provenance as pv


def _manifest() -> dict[str, object]:
    return pv.build_manifest(
        artifact_id="A" * 24,
        sha256="0" * 64,
        size_bytes=10,
        filename="x.parquet",
        operation="convert",
    )


def test_sign_and_verify_round_trip(signing_seed: str) -> None:
    signer = pv.Signer.from_b64(signing_seed)
    m = _manifest()
    assert pv.verify(signer.public_key_b64(), m, signer.sign(m))


@pytest.mark.parametrize(
    "field", ["sha256", "size_bytes", "artifact_id", "operation", "created_at"]
)
def test_any_change_to_the_manifest_breaks_the_signature(signing_seed: str, field: str) -> None:
    signer = pv.Signer.from_b64(signing_seed)
    m = _manifest()
    sig = signer.sign(m)
    tampered = dict(m)
    tampered[field] = "tampered" if isinstance(m[field], str) else 11
    assert not pv.verify(signer.public_key_b64(), tampered, sig)


def test_key_order_does_not_matter_canonical_json(signing_seed: str) -> None:
    signer = pv.Signer.from_b64(signing_seed)
    m = _manifest()
    reordered = dict(reversed(list(m.items())))
    assert pv.verify(signer.public_key_b64(), reordered, signer.sign(m))


def test_same_seed_same_public_key_different_seed_rejected(signing_seed: str) -> None:
    a, b = pv.Signer.from_b64(signing_seed), pv.Signer.from_b64(signing_seed)
    assert a.public_key_b64() == b.public_key_b64()
    other = pv.Signer(None)
    m = _manifest()
    assert not pv.verify(other.public_key_b64(), m, a.sign(m))


def test_context_string_provides_domain_separation(signing_seed: str) -> None:
    """A signature made here must not verify as a plain (context-free) ML-DSA signature."""
    signer = pv.Signer.from_b64(signing_seed)
    m = _manifest()
    sig = base64.b64decode(signer.sign(m))
    key = MLDSA65PublicKey.from_public_bytes(base64.b64decode(signer.public_key_b64()))
    with pytest.raises(InvalidSignature):
        key.verify(sig, pv.canonical(m))  # no context


def test_bad_inputs_never_raise_only_return_false(signing_seed: str) -> None:
    signer = pv.Signer.from_b64(signing_seed)
    assert not pv.verify(signer.public_key_b64(), _manifest(), "not-base64!!")
    assert not pv.verify("bm90IGEga2V5", _manifest(), signer.sign(_manifest()))


def test_invalid_seed_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="32-byte seed"):
        pv.Signer.from_b64(base64.b64encode(b"short").decode())


def test_ephemeral_key_is_flagged() -> None:
    assert pv.Signer(None).ephemeral
    assert pv.Signer.from_b64(None).public_document("http://x")["ephemeral"] is True
