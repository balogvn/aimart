"""A malformed download signature is a 404, never a bare 500.

Incident (final security review): ``LocalArtifactStore.verify`` passed the caller's ``sig``
string to ``hmac.compare_digest``, which raises TypeError for a str with non-ASCII characters, so
``GET /v1/artifacts/<id>/x.parquet?exp=9999999999&sig=%C3%A9`` reached the ASGI server as a 500
on the free, public download route. It now compares bytes.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx
import pytest

import aimart_testkit as kit
from aimart_parquet.artifacts import LocalArtifactStore

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.anyio

ODD_SIGNATURES = ["é", "%C3%A9", "Ω" * 32, "é" * 32, "0" * 31 + "é", "\x00", " "]


async def _artifact(app: kit.AppHarness) -> dict[str, str]:
    r = await app.http.post(
        "/v1/convert",
        json={"content_base64": kit.b64(b"a,b\n1,x\n"), "filename": "t.csv"},
    )
    assert r.status_code == 200, r.text
    return r.json()["artifact"]  # type: ignore[no-any-return]


@pytest.mark.parametrize("sig", ODD_SIGNATURES)
async def test_a_non_ascii_signature_is_a_404(free_app: kit.AppHarness, sig: str) -> None:
    artifact = await _artifact(free_app)
    url = httpx.URL(artifact["download_url"])
    r = await free_app.http.get(url.path, params={"exp": url.params["exp"], "sig": sig})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "artifact_not_found"
    assert r.content[:4] != b"PAR1"
    # The real link still works afterwards.
    ok = await free_app.http.get(url.raw_path.decode())
    assert ok.status_code == 200
    assert ok.content[:4] == b"PAR1"


async def test_the_reviewers_link_is_a_404_with_payments_on(paid_app: kit.AppHarness) -> None:
    r = await paid_app.http.get(
        "/v1/artifacts/AAAAAAAAAAAAAAAAAAAAAAAA/x.parquet?exp=9999999999&sig=%C3%A9"
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "artifact_not_found"


def test_verify_compares_any_text_without_raising(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path, ttl_s=3600, max_total_bytes=10**6, secret=b"k" * 32)
    artifact_id = "A" * 24
    exp = int(time.time()) + 600
    good = store.sign(artifact_id, exp)
    assert store.verify(artifact_id, exp, good)
    for sig in [*ODD_SIGNATURES, "\udcff" * 32, good.upper(), good + "é"]:
        assert store.verify(artifact_id, exp, sig) is False, repr(sig)
    for odd_id in ["é" * 24, "\udcff" * 24, "../" * 8, ""]:
        assert store.verify(odd_id, exp, good) is False, repr(odd_id)
