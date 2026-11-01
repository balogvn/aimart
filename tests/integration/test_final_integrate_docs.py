"""The guide, README, SECURITY.md and AGENTS.md describe the merged fixes as the code behaves.

Every quote here is produced by the code (on the wire where it can be) and must appear in the
docs word for word, with numbers taken from the defaults: the guide is what agents act on, and
a quote that drifted from the code is a refusal an agent cannot recognise.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
import pytest

import aimart_testkit as kit
from aimart_parquet import artifacts
from aimart_parquet.artifacts import LocalArtifactStore
from aimart_parquet.config import Settings
from aimart_parquet.engine import convert, isolate
from aimart_parquet.errors import AimartError, InputTooLarge

if TYPE_CHECKING:
    import httpx

ROOT = Path(__file__).resolve().parents[2]
_MB = 1024 * 1024


def text(rel: str, heading: str | None = None) -> str:
    """A doc (or one ``## heading`` section of it) with line wrapping collapsed."""
    body = (ROOT / rel).read_text(encoding="utf-8")
    if heading is not None:
        start = body.index(heading)
        end = body.find("\n## ", start + len(heading))
        body = body[start : end if end != -1 else None]
    return " ".join(body.split())


GUIDE = text("docs/agents-guide.md", "## 6. Handle errors")
README = text("README.md", "## Limits and errors")


def _error(r: httpx.Response, status: int) -> dict[str, Any]:
    assert r.status_code == status, r.text
    error: dict[str, Any] = r.json()["error"]
    return error


@pytest.mark.anyio
async def test_the_guide_quotes_the_empty_upload_refusal(paid_app: kit.AppHarness) -> None:
    r = await paid_app.http.post("/v1/convert/upload", files={"file": ("e.csv", b"")})
    assert f'"{_error(r, 400)["message"]}"' in GUIDE


@pytest.mark.anyio
async def test_the_guide_quotes_the_undeclared_argument_refusal(paid_app: kit.AppHarness) -> None:
    async with kit.mcp_over_http(paid_app.app) as client:
        result = await client.call_tool(
            "inspect_parquet", {"artifact_id": "Zz" * 12, "content_base64": "YQ=="}
        )
    error = (result.structured_content or {})["error"]
    assert f'"{error["message"]}"' in GUIDE
    assert '"Its parameters are: ..."' in GUIDE
    assert error["hint"].startswith("Its parameters are: ")


@pytest.mark.anyio
async def test_the_guide_quotes_the_convert_out_of_memory_refusal(
    paid_app: kit.AppHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def runs_out(*_: Any, **__: Any) -> None:
        raise duckdb.OutOfMemoryException("Out of Memory Error")

    monkeypatch.setattr(convert, "copy_to_parquet", runs_out)
    body = {"content_base64": kit.b64(b"a\n1\n"), "filename": "x.csv"}
    error = _error(await paid_app.paying.post("/v1/convert", json=body), 413)
    assert f'"{error["message"]}"' in GUIDE
    assert "`options.infer_types=false`" in GUIDE
    assert "options.infer_types=false" in error["hint"]


def test_the_guide_quotes_the_memory_budget_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit = Settings(_env_file=None).job_memory_limit_mb
    (tmp_path / "f").write_bytes(b"x" * (3 * _MB))
    monkeypatch.setattr(isolate, "_read_rss", lambda pid: limit * _MB)
    with pytest.raises(InputTooLarge) as info:
        isolate._over_budget(0, limit, (tmp_path,))
    template = re.sub(
        r"\b\d+ MB in memory plus \d+ MB", "N MB in memory plus M MB", info.value.message
    )
    assert template.startswith(f"The job exceeded its memory budget ({limit} MB)")
    assert f'"{template}"' in GUIDE
    assert set(info.value.details) == {"limit_bytes", "memory_bytes", "temp_file_bytes"}
    assert "`details.memory_bytes`" in GUIDE
    assert "`details.temp_file_bytes`" in GUIDE


def test_the_guide_quotes_the_output_storage_refusal(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path, ttl_s=3600, max_total_bytes=10, secret=b"s" * 32)
    data = tmp_path / "x.parquet"
    data.write_bytes(b"x" * 8)
    store.put(data, "x.parquet")
    refusal = store._full(8)
    assert f'"{refusal.message}"' in GUIDE
    assert refusal.hint is not None
    assert refusal.hint.startswith("Retry in about 60 minutes, when enough stored outputs have")
    assert '"Retry in about N minutes, when enough stored outputs have expired, ..."' in GUIDE
    assert "`details.retry_after_s`" in GUIDE
    assert "retry_after_s" in refusal.details


def test_the_docs_no_longer_promise_ten_minutes_of_residency() -> None:
    assert not hasattr(artifacts, "MIN_RESIDENCY_S")
    for rel in ("SECURITY.md", "README.md", "docs/agents-guide.md"):
        assert "10 minutes ago" not in text(rel), rel
    assert "a local one that has not expired" in text("SECURITY.md")
    agents = text("AGENTS.md")
    assert "`MIN_RESIDENCY_S` is gone" in agents
    assert "(`MIN_RESIDENCY_S`)" not in agents


def test_readme_and_security_describe_the_cap_as_memory_plus_files() -> None:
    limit = Settings(_env_file=None).job_memory_limit_mb
    assert (
        f"| memory per job | {limit} MB, counting its resident memory plus its temporary files |"
        in README
    )
    assert "roughly over 120 MB uncompressed" in README
    assert "roughly over 120 MB uncompressed" in GUIDE
    security = text("SECURITY.md")
    assert "resident memory plus its temporary files" in security
    assert "logged by its type and traceback frames, never its text" in security


def test_agents_md_names_each_mcp_tools_inputs() -> None:
    agents = text("AGENTS.md")
    assert (
        "Remote: `convert_to_parquet` takes `url`/`content_base64`/`artifact_id`, every other tool `url`/`artifact_id`."
        in agents
    )
    assert "`SourceRef.takes`" in agents
    assert "`errors.without_paths`" in agents


def test_internal_error_hint_in_the_guide_is_the_codes() -> None:
    from aimart_parquet.errors import InternalError

    assert isinstance(InternalError.of("KeyError"), AimartError)
    assert '"Internal error (KeyError)."' in GUIDE
    assert InternalError.HINT.startswith("Retry; if it persists, report it.")
