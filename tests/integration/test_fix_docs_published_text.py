"""What the discovery documents and the docs promise must be what the code does.

An adversarial review of commit 6f99145 found promises that every other test let through: a
retention statement that said "deleted" where the code only made files unreachable, a join limit
off by one, and machine-read summaries that never mentioned PDFs or joins. Agents decide from
this text, so a wrong sentence is a wrong purchase. Each test reads the text a real consumer
reads (the served document, or the file in the repository) and compares it with the code.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from aimart_parquet.api.discovery import DESCRIPTION, llms_txt, retention_policy
from aimart_parquet.config import Settings
from aimart_parquet.engine.detect import SUPPORTED_EXTENSIONS
from aimart_parquet.payments.bazaar import EXAMPLES
from aimart_parquet.payments.pricing import BY_KEY, OPERATIONS
from aimart_parquet.service import MAX_QUERY_TABLES

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[2]
EXTRA_TABLES = MAX_QUERY_TABLES - 1  # `data` is one of the tables
PDF = "digital (not scanned) PDF"


def doc(rel: str) -> str:
    """A repository document with line wrapping collapsed, so phrases can span lines."""
    return " ".join((ROOT / rel).read_text(encoding="utf-8").split())


def section(rel: str, heading: str) -> str:
    """The text of one ``## heading`` section (up to the next ``## ``)."""
    text = (ROOT / rel).read_text(encoding="utf-8")
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return " ".join(text[start : end if end != -1 else None].split())


# ------------------------------------------------------------------ D1: data retention
@pytest.mark.parametrize("bucket", [None, "aimart-test-artifacts"])
def test_retention_promises_unreachable_at_the_ttl_not_deletion(
    make_settings: Callable[..., Settings], bucket: str | None
) -> None:
    # The TTL makes an artifact unreachable; deletion runs later (a sweep at most once a
    # minute, and on GCS a listing sweep plus the bucket's lifecycle rule). Promising
    # "deleted 30 minutes after creation" was false for every artifact.
    outputs = retention_policy(make_settings(gcs_bucket=bucket, artifact_ttl_s=1800))["outputs"]
    assert "unreachable 30 minutes after creation" in outputs
    assert "deleted 30 minutes" not in outputs
    assert "within minutes" in outputs


def test_retention_cites_the_one_day_backstop_only_where_it_exists(
    make_settings: Callable[..., Settings],
) -> None:
    local = retention_policy(make_settings())["outputs"]
    gcs = retention_policy(make_settings(gcs_bucket="aimart-test-artifacts"))["outputs"]
    assert "day" not in local  # no bucket, no lifecycle rule to cite
    assert "1-day" in gcs
    assert "about a day" in gcs  # the worst case, stated as one


@pytest.mark.parametrize("bucket", [None, "aimart-test-artifacts"])
def test_llms_txt_repeats_the_published_retention_word_for_word(
    make_settings: Callable[..., Settings], bucket: str | None
) -> None:
    settings = make_settings(gcs_bucket=bucket)
    text = llms_txt(settings)
    assert retention_policy(settings)["outputs"] in text  # one statement, not a paraphrase
    assert "deleted when the call ends" in text


@pytest.mark.parametrize("rel", ["README.md", "docs/privacy.md", "deploy/README.md", "SECURITY.md"])
def test_docs_state_the_same_retention_contract(rel: str) -> None:
    text = doc(rel)
    assert "unreachable" in text, rel
    assert "about a day" in text, rel
    for overpromise in ("converted files after 60 minutes", "then deleted after the TTL"):
        assert overpromise not in text, (rel, overpromise)


# ------------------------------------------------------------------ D2: join limits
@pytest.mark.parametrize("rel", ["README.md", "docs/agents-guide.md"])
def test_join_limit_counts_data_and_shows_both_table_shapes(rel: str) -> None:
    text = doc(rel)
    assert "up to 8 files" not in text
    assert f"up to {MAX_QUERY_TABLES} tables" in text, rel
    assert f"up to {EXTRA_TABLES} more" in text, rel
    assert '"tables": {"orders": {"artifact_id": "' in text, rel  # REST: an object per table
    assert '"tables": {"orders": "' in text, rel  # MCP: the artifact_id string itself


def test_llms_txt_states_the_join_limit_and_both_shapes(
    make_settings: Callable[..., Settings],
) -> None:
    text = llms_txt(make_settings())
    assert f"up to {EXTRA_TABLES} more" in text
    assert '"tables": {"orders": {"artifact_id": "' in text
    assert '"tables": {"orders": "' in text


# ------------------------------------------------------------------ D3: PDFs and joins in summaries
@pytest.mark.parametrize("key", ["convert", "convert_upload"])
def test_convert_summaries_mention_pdf_tables(key: str) -> None:
    # The summary is what /pricing, llms.txt, the server card, the 402's resource.description
    # and the Bazaar catalog all show; an agent with a PDF statement read "CSV/JSON/Excel".
    assert PDF in BY_KEY[key].summary


def test_service_description_mentions_pdf_tables_and_joins() -> None:
    assert PDF in DESCRIPTION
    assert "join" in DESCRIPTION


def test_query_summary_mentions_joins_with_the_real_limit() -> None:
    summary = BY_KEY["query"].summary
    assert f"join up to {EXTRA_TABLES} more" in summary
    assert "`tables`" in summary


def test_bazaar_query_schema_offers_tables_with_the_real_limit() -> None:
    props = EXAMPLES["query"]["input_schema"]["properties"]
    assert props["tables"]["maxProperties"] == EXTRA_TABLES
    assert "tables" in props["sql"]["description"]


def test_server_json_description_fits_and_prices_match_the_defaults() -> None:
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert len(manifest["description"]) <= 100  # the MCP Registry's limit
    assert "PDF" in manifest["description"]
    x402 = manifest["_meta"]["io.modelcontextprotocol.registry/publisher-provided"]["x402"]
    defaults = Settings(_env_file=None)
    for op in OPERATIONS:
        if op.requires is None:
            assert x402["prices"][op.tool] == op.price(defaults), op.tool


def test_readme_formats_table_lists_every_supported_extension() -> None:
    formats = section("README.md", "## Supported formats")
    for ext in SUPPORTED_EXTENSIONS:
        assert f"`{ext}`" in formats, ext


def test_agents_guide_lists_pdf_options_and_tables() -> None:
    convert = section("docs/agents-guide.md", "## 3. Convert")
    assert "pdf_pages" in convert
    assert "pdf_table" in convert
    query = section("docs/agents-guide.md", "## 4. Look, then ask")
    assert "`tables`" in query


# ------------------------------------------------------------------ D5: premium tier is opt-in
def test_agents_guide_does_not_send_readers_to_premium_tools_that_may_be_off() -> None:
    choose = section("docs/agents-guide.md", "## 0. Decide")
    assert "premium" in choose
    assert "enabled" in choose
    assert "/pricing" in choose


def test_agents_md_says_the_premium_tier_is_off_by_default() -> None:
    what = section("AGENTS.md", "## What this is")
    assert "premium" in what.lower()
    assert "off by default" in what


# ------------------------------------------------------------------ D8: behaviour other fixes changed
def test_docs_describe_positional_query_rows() -> None:
    query = section("docs/agents-guide.md", "## 4. Look, then ask")
    assert "same name" in query
    assert "`columns`" in query


def test_readme_pdf_row_mentions_password_protected_files() -> None:
    formats = section("README.md", "## Supported formats")
    assert re.search(r"[Pp]assword-protected PDFs", formats)


def test_agents_md_code_map_describes_artifact_deletion() -> None:
    code_map = section("AGENTS.md", "## Code map")
    assert "delete()" in code_map
    assert "lifecycle" in code_map
