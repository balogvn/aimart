"""The example client says why a paid call was refused, in one line, whatever the 402 carries.

Review of 2026-11-16 (S1, S5). ``docs/examples/agent_pays.py`` read the reason of every 402
after a paid attempt from ``r.headers["PAYMENT-REQUIRED"]``. Only a refusal before the work
(verify) carries that header; the 402 after a settlement that fails, raises or cannot be read
carries only PAYMENT-RESPONSE, so the example died with ``KeyError: 'PAYMENT-REQUIRED'`` on
exactly the path where the payer may already have been charged. A refused second payment
(inspect) was an ``httpx.HTTPStatusError`` traceback with no artifact id printed for the
conversion already paid, and a refused MCP payment a ``BaseExceptionGroup``: ``sys.exit``
inside ``mcp.Client``'s task group.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import httpx2
import pytest
from mcp.client.streamable_http import streamable_http_client
from x402 import SettleResponse, VerifyResponse

import aimart_testkit as kit

if TYPE_CHECKING:
    from types import ModuleType

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[2]
CSV = b"region,units\nNorth,10\nSouth,20\n"
BODY = {"content_base64": kit.b64(CSV), "filename": "sales.csv"}
TX = kit.TX_HASH


def load_example() -> ModuleType:
    path = ROOT / "docs" / "examples" / "agent_pays.py"
    spec = importlib.util.spec_from_file_location("agent_pays_example", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class OnlyTheFirst(kit.FakeFacilitator):
    """Takes the first payment (the conversion) and refuses every later one."""

    at: str = "settle"  # where the later ones are refused: "verify" or "settle"

    async def verify(self, payload: Any, requirements: Any) -> VerifyResponse:
        if self.at == "verify" and self.settled:
            self.verified.append((payload, requirements))
            return VerifyResponse(is_valid=False, invalid_reason="insufficient_funds")
        return await super().verify(payload, requirements)

    async def settle(self, payload: Any, requirements: Any) -> SettleResponse:
        if self.settled:
            return SettleResponse(
                success=False,
                error_reason="insufficient_funds",
                transaction="",
                network=requirements.network,
            )
        return await super().settle(payload, requirements)


async def rest_exit(settings: Settings, facilitator: Any) -> str:
    """What the example exits with when its REST flow runs against ``facilitator``."""
    example = load_example()
    async with kit.running_app(settings, facilitators=[facilitator]) as h:
        with pytest.raises(SystemExit) as stopped:  # before: KeyError, or HTTPStatusError
            await example.rest_flow(
                kit.BASE_URL,
                kit.paying_client(),
                CSV,
                "sales.csv",
                transport=httpx.ASGITransport(app=h.app),
            )
    line = stopped.value.code
    assert isinstance(line, str), line  # a message, not an exit status
    assert "\n" not in line
    return line


async def mcp_exit(settings: Settings, facilitator: Any, *, break_it: Any) -> str:
    """The same for the MCP flow: one conversion is paid first, then ``break_it`` runs."""
    example = load_example()
    async with kit.running_app(settings, facilitators=[facilitator]) as h:
        paid = await h.paying.post("/v1/convert", json=BODY)
        assert paid.status_code == 200, paid.text
        break_it(facilitator)
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=h.app), base_url=kit.BASE_URL
        ) as http_client:
            # Before: BaseExceptionGroup("unhandled errors in a TaskGroup").
            with pytest.raises(SystemExit) as stopped:
                await example.mcp_flow(
                    f"{kit.BASE_URL}/mcp",
                    kit.paying_client(),
                    paid.json()["artifact"]["id"],
                    transport=streamable_http_client(
                        f"{kit.BASE_URL}/mcp", http_client=http_client
                    ),
                )
    line = stopped.value.code
    assert isinstance(line, str), line
    assert "\n" not in line
    return line


# ------------------------------------------------------------------------------ S1: REST
async def test_a_refused_settlement_is_one_line_with_its_reason(settings: Settings) -> None:
    line = await rest_exit(settings, kit.FakeFacilitator(settle_ok=False))
    assert "not settled" in line
    assert "insufficient_funds" in line  # PAYMENT-RESPONSE's errorReason
    assert "result was withheld" in line


async def test_a_settlement_that_raised_is_one_line_with_its_reason(settings: Settings) -> None:
    """The known gap: the facilitator timed out and the transfer may still complete."""
    line = await rest_exit(settings, kit.FakeFacilitator(settle_error=TimeoutError("took 20 s")))
    assert "not settled" in line
    assert "took 20 s" in line
    assert "before paying again" in line  # the payer may have been charged


async def test_a_refusal_before_the_work_is_one_line_with_its_reason(settings: Settings) -> None:
    line = await rest_exit(settings, kit.FakeFacilitator(valid=False))
    assert "payment rejected" in line
    assert "insufficient_funds" in line  # PAYMENT-REQUIRED's error
    assert "not settled" not in line  # nothing was attempted: no warning to look on chain
    assert "on chain" not in line


async def test_a_transaction_named_by_a_failed_settlement_is_passed_to_the_payer(
    settings: Settings,
) -> None:
    """The one thing the payer can check: the 402's PAYMENT-RESPONSE names a transaction."""
    facilitator = kit.wire_facilitator({"transaction": TX, "error": "reverted"})
    line = await rest_exit(settings, facilitator)
    assert "reverted" in line
    assert TX in line
    assert "look it up on chain before paying again" in line


@pytest.mark.parametrize(
    "headers",
    [
        {},  # another service's bare 402
        {"PAYMENT-REQUIRED": "not base64 !!"},
        {"PAYMENT-RESPONSE": "bnVsbA=="},  # null
        {"PAYMENT-RESPONSE": "W10="},  # []
        {"PAYMENT-REQUIRED": "e30=", "PAYMENT-RESPONSE": "e30="},  # {} and {}
    ],
)
def test_a_402_with_no_readable_reason_is_still_one_line(headers: dict[str, str]) -> None:
    line = load_example().refusal(httpx.Response(402, headers=headers))
    assert "no reason given" in line
    assert "\n" not in line


# ------------------------------------------------------------------------------ S5
async def test_the_artifact_is_named_as_soon_as_the_conversion_is_paid(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """The second payment (inspect) fails after the first settled: the payer must be left
    with the id and the link of what it bought, and one line about the inspect."""
    line = await rest_exit(settings, OnlyTheFirst())
    printed = capsys.readouterr().out
    assert f"paid 20000 in tx {TX}" in printed
    artifact = [row for row in printed.splitlines() if row.startswith("artifact: ")]
    assert len(artifact) == 1
    assert f"{kit.BASE_URL}/v1/artifacts/" in artifact[0]  # the download link
    assert "inspect" in line
    assert "insufficient_funds" in line


async def test_an_inspect_refused_before_the_work_is_one_line_too(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    line = await rest_exit(settings, OnlyTheFirst(at="verify"))
    assert "artifact: " in capsys.readouterr().out
    assert "inspect" in line
    assert "payment rejected" in line


def refuse_settle(facilitator: kit.FakeFacilitator) -> None:
    facilitator.settle_ok = False


def refuse_verify(facilitator: kit.FakeFacilitator) -> None:
    facilitator.valid = False


def raise_in_settle(facilitator: kit.FakeFacilitator) -> None:
    facilitator.settle_error = TimeoutError("took 20 s")


@pytest.mark.parametrize(
    ("break_it", "says"),
    [
        (refuse_settle, ["not settled", "insufficient_funds"]),
        (raise_in_settle, ["not settled", "took 20 s", "before paying again"]),
        (refuse_verify, ["payment rejected", "insufficient_funds"]),
    ],
)
async def test_a_refused_mcp_payment_is_one_line_not_an_exception_group(
    break_it: Any, says: list[str], settings: Settings
) -> None:
    line = await mcp_exit(settings, kit.FakeFacilitator(), break_it=break_it)
    assert line.startswith("mcp query_parquet: ")
    for text in says:
        assert text in line
