"""The default facilitators, and what the mainnet 402 for Base says with the chosen one.

2026-11-12 to 15: the first mainnet default (facilitator.heurist.xyz) answered GET /supported
with a Cloudflare 502 at every probe (11-12, 11-14, 11-15) while its site still called it live, and the second
(Dexter) lists x402 v2 'exact' on Base with ``assetTransferMethod: permit2``, not the EIP-3009
transfer this service quotes. The defaults are now two keyless facilitators that settle USDC on
Base with EIP-3009 (xpay, then Primer), then Dexter, which settles Solana
(docs/adr/0003, the amendment). No network here: each fake repeats the kinds its facilitator's
GET /supported listed on 2026-11-15.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import pytest

import aimart_testkit as kit
from aimart_parquet.config import NetworkMode
from aimart_parquet.payments import DEFAULT_FACILITATORS, facilitator_urls

if TYPE_CHECKING:
    from collections.abc import Callable

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

# The owner's public receive address on Base: an address, not a key.
PAYEE = "0xa206bb756b0F3F08e8d31BB46e7F7C5145A883Aa"
SOLANA_MAINNET = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
A = "Zz" * 12
XPAY, PRIMER, DEXTER = (
    "https://facilitator.xpay.sh",
    "https://x402.primer.systems",
    "https://x402.dexter.cash",
)
# Dexter's v2 'exact' kind on Base, as it listed it: Permit2, not the EIP-3009 default.
DEXTER_BASE = {
    "name": "USD Coin",
    "version": "2",
    "features": {"gasSponsored": True, "smartWalletSupported": True},
    "assetTransferMethod": "permit2",
    "paymentFloorAvailable": False,
    "minPaymentAmountAtomic": None,
    "minPaymentAmountUsd": None,
}
# What a payer signs for USDC on Base: the token's own EIP-712 domain (chain 8453, the USDC
# contract) and nothing else, so one EIP-3009 authorization and no approval.
EIP3009_DOMAIN = {"name": "USD Coin", "version": "2"}


def chosen() -> list[kit.FakeFacilitator]:
    """The mainnet defaults, in order, as fakes."""
    return [
        # xpay and Primer list the kind with no ``extra`` at all.
        kit.FakeFacilitator(url=XPAY, networks=(kit.BASE_MAINNET,)),
        kit.FakeFacilitator(url=PRIMER, networks=(kit.BASE_MAINNET,)),
        kit.FakeFacilitator(
            url=DEXTER,
            networks=(kit.BASE_MAINNET, SOLANA_MAINNET),
            kind_extra={kit.BASE_MAINNET: DEXTER_BASE},
        ),
    ]


@pytest.fixture
def mainnet(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings(network_mode="mainnet", pay_to_evm=PAYEE, facilitator_urls=None)


# ------------------------------------------------------------------------------ DEF 2
def test_the_mainnet_defaults_are_the_chosen_three_in_order(mainnet: Settings) -> None:
    assert facilitator_urls(mainnet) == [XPAY, PRIMER, DEXTER]
    assert [f.url for f in chosen()] == facilitator_urls(mainnet)


def test_testnet_is_unchanged() -> None:
    assert DEFAULT_FACILITATORS[NetworkMode.TESTNET] == ("https://x402.org/facilitator",)


@pytest.mark.parametrize("mode", list(NetworkMode), ids=lambda m: m.value)
def test_default_lists_are_https_urls_without_duplicates(mode: NetworkMode) -> None:
    urls = DEFAULT_FACILITATORS[mode]
    assert set(DEFAULT_FACILITATORS) == set(NetworkMode)
    assert isinstance(urls, tuple)
    assert urls
    for url in urls:
        parts = urlsplit(url)
        assert parts.scheme == "https"
        assert parts.hostname
        assert "." in parts.hostname
        # The SDK appends '/supported', '/verify' and '/settle' to it as it is.
        assert not url.endswith("/")
        assert (parts.query, parts.fragment, parts.username) == ("", "", None)
        assert url == url.strip() == url.lower()
    hosts = [urlsplit(url).hostname for url in urls]
    assert len(set(hosts)) == len(hosts)


def test_mainnet_has_an_independent_fallback_and_no_dead_entry() -> None:
    urls = DEFAULT_FACILITATORS[NetworkMode.MAINNET]
    assert len(urls) >= 2
    # 502 from its /supported at every probe, 2026-11-12 to 15; testnet only; v1 only.
    for host in ("heurist", "x402.org", "mogami"):
        assert not any(host in url for url in urls)
    # Two different operators before Dexter: by registrable domain, not by subdomain.
    domains = [".".join(urlsplit(url).hostname.split(".")[-2:]) for url in urls]  # type: ignore[union-attr]
    assert len(set(domains)) == len(domains)


# ------------------------------------------------------------------------------ DEF 3
def base_requirement(accepts: list[dict[str, Any]]) -> dict[str, Any]:
    (requirement,) = accepts  # one payee, one network: nothing else is quoted
    return requirement


async def test_the_mainnet_402_for_base_names_usdc_the_payee_and_the_eip3009_domain(
    mainnet: Settings,
) -> None:
    fakes = chosen()
    async with kit.running_app(mainnet, facilitators=fakes) as h:
        rest = await h.http.post("/v1/convert", json={})
        async with kit.mcp_over_http(h.app) as client:
            tool = await client.call_tool("inspect_parquet", {"artifact_id": A})
    assert rest.status_code == 402
    quoted = kit.payment_required(rest)
    assert quoted["x402Version"] == 2
    requirement = base_requirement(quoted["accepts"])
    assert requirement["scheme"] == "exact"
    assert requirement["network"] == "eip155:8453"
    assert requirement["asset"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    assert requirement["payTo"] == PAYEE
    assert requirement["amount"] == "20000"  # $0.02 in USDC's 6 decimals
    assert requirement["maxTimeoutSeconds"] == 300
    # The facilitator's kind has no `extra`, so the domain is the token's: EIP-3009. A
    # `assetTransferMethod` here would send every payer down the Permit2 path instead.
    assert {k: requirement["extra"][k] for k in EIP3009_DOMAIN} == EIP3009_DOMAIN
    assert "assetTransferMethod" not in requirement["extra"]

    assert tool.is_error
    assert tool.structured_content is not None
    over_mcp = base_requirement(tool.structured_content["accepts"])
    assert (over_mcp["network"], over_mcp["asset"], over_mcp["payTo"], over_mcp["amount"]) == (
        "eip155:8453",
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        PAYEE,
        "2000",
    )
    assert {k: over_mcp["extra"][k] for k in EIP3009_DOMAIN} == EIP3009_DOMAIN
    assert "assetTransferMethod" not in over_mcp["extra"]
    # Asked once each, and the quote cost nobody a verify.
    assert [f.supported_calls for f in fakes] == [1, 1, 1]
    assert all(f.verified == [] for f in fakes)


async def test_the_first_default_serves_base_and_dexter_serves_solana(
    make_settings: Callable[..., Settings],
) -> None:
    both = make_settings(
        network_mode="mainnet", pay_to_evm=PAYEE, pay_to_svm=kit.PAY_TO_SVM, facilitator_urls=None
    )
    xpay, primer, dexter = fakes = chosen()
    async with kit.running_app(both, facilitators=fakes) as h:
        assert h.payments is not None
        rest = await h.http.post("/v1/convert", json={})
        served = {
            network: h.payments.server._facilitator_clients_map[network]["exact"]
            for network in ("eip155:8453", SOLANA_MAINNET)
        }
    quoted = {a["network"]: a for a in kit.payment_required(rest)["accepts"]}
    assert set(quoted) == {"eip155:8453", SOLANA_MAINNET}
    assert quoted[SOLANA_MAINNET]["payTo"] == kit.PAY_TO_SVM
    assert quoted[SOLANA_MAINNET]["extra"]["feePayer"] == kit.FEE_PAYER
    # Neither EIP-3009 facilitator lists Solana, so they cannot take it from Dexter.
    assert served["eip155:8453"].client is xpay
    assert served[SOLANA_MAINNET].client is dexter
    assert primer.verified == []


async def test_with_xpay_down_primer_serves_base_with_the_same_quote(mainnet: Settings) -> None:
    xpay, primer, dexter = fakes = chosen()
    xpay.supported_error = RuntimeError("down")
    async with kit.running_app(mainnet, facilitators=fakes) as h:
        assert h.payments is not None
        rest = await h.http.post("/v1/convert", json={})
        serving = h.payments.server._facilitator_clients_map["eip155:8453"]["exact"]
    requirement = base_requirement(kit.payment_required(rest)["accepts"])
    assert serving.client is primer
    assert (requirement["asset"], requirement["payTo"]) == (kit.USDC_BASE_MAINNET, PAYEE)
    assert "assetTransferMethod" not in requirement["extra"]
    assert dexter.verified == []


async def test_dexter_alone_on_base_is_still_quoted_as_eip3009(mainnet: Settings) -> None:
    """A fact to know, not a behaviour to keep: the SDK builds a Base quote from its own table
    of assets, never from the facilitator's kind. With both EIP-3009 facilitators down at
    initialization Dexter serves Base, whose kind says Permit2, and the 402 still asks for an
    EIP-3009 authorization. Whether Dexter verifies one was not tested (that needs /verify
    with a signed payment); if it does not, the paid retry is a 402, unpaid.
    """
    xpay, primer, dexter = fakes = chosen()
    xpay.supported_error = primer.supported_error = RuntimeError("down")
    async with kit.running_app(mainnet, facilitators=fakes) as h:
        rest = await h.http.post("/v1/convert", json={})
    requirement = base_requirement(kit.payment_required(rest)["accepts"])
    assert {k: requirement["extra"][k] for k in EIP3009_DOMAIN} == EIP3009_DOMAIN
    assert "assetTransferMethod" not in requirement["extra"]
    assert dexter.supported_calls == 1
