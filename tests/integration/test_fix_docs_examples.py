"""Every amount a document shows must be one the service actually sends at its default prices.

When convert moved from $0.01 to $0.02, the README's decoded 402 kept ``"amount": "10000"``, the
payments page settled 10000 for a 2000 quote, the economics model kept a $0.024 session, and the
agents guide claimed a cap test its own command could not have run. Copy-pasted samples are what
agents and their developers trust most, so each one is compared with the wire here.
"""

from __future__ import annotations

import importlib.util
import json
import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import httpx2
import pytest
from mcp.client.streamable_http import streamable_http_client
from x402.http.clients.httpx import PaymentError

import aimart_testkit as kit
from aimart_parquet.config import Settings

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[2]
CSV = b"region,units\nNorth,10\nSouth,20\n"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def flat(rel: str) -> str:
    return " ".join(read(rel).split())


def json_block_after(rel: str, marker: str) -> dict[str, Any]:
    """The first ```json block after ``marker`` in a document, parsed."""
    text = read(rel)
    start = text.index(marker)
    match = re.compile(r"```json\n(.*?)\n\s*```", re.DOTALL).search(text, start)
    assert match, f"no json block after {marker!r} in {rel}"
    block: dict[str, Any] = json.loads(match.group(1))
    return block


def usd(price: str) -> Decimal:
    return Decimal(price.removeprefix("$"))


def dollars(value: Decimal) -> str:
    return f"${value.quantize(Decimal(1), rounding=ROUND_HALF_UP):,}"


def load_example() -> ModuleType:
    path = ROOT / "docs" / "examples" / "agent_pays.py"
    spec = importlib.util.spec_from_file_location("agent_pays_example", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def live_quote(h: kit.AppHarness, path: str) -> dict[str, Any]:
    accepts: dict[str, Any] = kit.payment_required(await h.http.post(path, json={}))["accepts"][0]
    return accepts


# ------------------------------------------------------------------ README and payments.md samples
async def test_readme_decoded_402_is_what_convert_sends(paid_app: kit.AppHarness) -> None:
    sample = json_block_after("README.md", '"$URL/v1/convert"')["accepts"][0]
    live = await live_quote(paid_app, "/v1/convert")
    for field in ("scheme", "network", "asset", "amount", "maxTimeoutSeconds", "extra"):
        assert sample[field] == live[field], field


async def test_payments_page_samples_quote_and_settle_the_same_amount(
    paid_app: kit.AppHarness,
) -> None:
    sample = json_block_after("docs/payments.md", "## REST flow")
    path = sample["resource"]["url"].removeprefix("https://YOUR-SERVICE.run.app")
    live = await live_quote(paid_app, path)
    assert sample["accepts"][0]["amount"] == live["amount"]
    assert (
        sample["resource"]["description"]
        == kit.payment_required(await paid_app.http.post(path, json={}))["resource"]["description"]
    )
    settled = re.search(r'`(\{"success": true, [^`]*\})`', read("docs/payments.md"))
    assert settled, "no PAYMENT-RESPONSE sample"
    documented = json.loads(settled.group(1))
    # Pay for the documented operation for real (fake facilitator) and read the header back.
    converted = await paid_app.paying.post(
        "/v1/convert", json={"content_base64": kit.b64(CSV), "filename": "sales.csv"}
    )
    paid = await paid_app.paying.post(
        path, json={"artifact_id": converted.json()["artifact"]["id"]}
    )
    assert paid.status_code == 200, paid.text
    receipt = kit.payment_response(paid)
    assert set(documented) == set(receipt)
    assert documented["amount"] == receipt["amount"] == live["amount"]  # settles what was quoted
    assert documented["network"] == receipt["network"]


# ------------------------------------------------------------------ economics.md arithmetic
def session_revenue(s: Settings) -> Decimal:
    """The model's classical session: convert + inspect + preview + 2 queries."""
    prices = [s.price_convert, s.price_inspect, s.price_preview, s.price_query, s.price_query]
    return sum((usd(p) for p in prices), Decimal(0))


def test_economics_session_revenue_uses_the_default_prices() -> None:
    defaults = Settings(_env_file=None)
    session = session_revenue(defaults)
    premium = (usd(defaults.price_quantum_compress) + usd(defaults.price_quantum_portfolio)) / 2
    text = flat("docs/economics.md")
    assert f"| ${session} |" in text  # the compute table's revenue column
    assert f"sessions × (${session} + premium share × ${premium})" in text


def test_economics_revenue_table_follows_its_own_formula() -> None:
    defaults = Settings(_env_file=None)
    session = session_revenue(defaults)
    premium = (usd(defaults.price_quantum_compress) + usd(defaults.price_quantum_portfolio)) / 2
    shares = [Decimal(0), Decimal("0.01"), Decimal("0.05"), Decimal("0.12")]
    rows = re.findall(r"^\| ([\d,]+) (?:\([a-z]+\) )?\|(.*)\|$", read("docs/economics.md"), re.M)
    assert len(rows) == 4
    for sessions_text, cells in rows:
        sessions = Decimal(sessions_text.replace(",", ""))
        for share, cell in zip(shares, cells.split("|"), strict=True):
            monthly = sessions * (session + share * premium)
            assert cell.strip() == f"{dollars(monthly)}/mo · {dollars(monthly * 12)}/yr", (
                sessions_text,
                share,
            )


def test_economics_reading_notes_match_the_optimistic_row() -> None:
    session = session_revenue(Settings(_env_file=None))
    text = flat("docs/economics.md")
    found = re.search(r"matches about \*\*([\d,]+) sessions a month\*\*", text)
    assert found
    sessions = Decimal(found.group(1).replace(",", ""))
    assert abs(sessions * session * 12 - 96_000) < 500  # the $96k a year it claims to match
    assert f"| {found.group(1)} (optimistic) |" in text


# ------------------------------------------------------------------ the runnable example's claims
async def test_agents_guide_cap_claim_is_what_the_example_does(
    make_settings: Callable[..., Settings],
) -> None:
    claim = re.search(
        r"a `--max-usd ([0-9.]+)` cap refused the \$([0-9.]+) convert quote",
        flat("docs/agents-guide.md"),
    )
    assert claim, "the guide must name the cap it tested"
    cap, quoted = Decimal(claim.group(1)), claim.group(2)
    settings = make_settings()
    assert f"${quoted}" == settings.price_convert
    example = load_example()
    async with kit.running_app(settings) as h:
        payer = example.make_payer(kit.PAYER_KEY, network=kit.BASE_SEPOLIA, max_usd=cap)
        with pytest.raises(PaymentError, match="filtered out by policies"):
            await example.rest_flow(
                kit.BASE_URL, payer, CSV, "sales.csv", transport=httpx.ASGITransport(app=h.app)
            )
    assert h.facilitator is not None
    assert h.facilitator.verified == []  # refused before anything was signed or sent


async def test_documented_example_command_settles_what_the_guide_says(
    make_settings: Callable[..., Settings],
) -> None:
    guide = flat("docs/agents-guide.md")
    command = re.search(r"uv run docs/examples/agent_pays\.py \S+ --max-usd ([0-9.]+)", guide)
    settled_claim = re.search(r"it settled (\$[0-9.]+(?: \+ \$[0-9.]+)*)", guide)
    assert command
    assert settled_claim
    example = load_example()
    async with kit.running_app(make_settings()) as h:
        payer = example.make_payer(
            kit.PAYER_KEY, network=kit.BASE_SEPOLIA, max_usd=Decimal(command.group(1))
        )
        converted = await example.rest_flow(
            kit.BASE_URL, payer, CSV, "sales.csv", transport=httpx.ASGITransport(app=h.app)
        )
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=h.app), base_url=kit.BASE_URL
        ) as http_client:
            await example.mcp_flow(
                f"{kit.BASE_URL}/mcp",
                payer,
                converted["artifact"]["id"],
                transport=streamable_http_client(f"{kit.BASE_URL}/mcp", http_client=http_client),
            )
    assert h.facilitator is not None
    paid = [Decimal(req.amount) / 10**6 for _, req in h.facilitator.settled]
    claimed = [usd(p) for p in settled_claim.group(1).split(" + ")]
    assert paid == claimed
