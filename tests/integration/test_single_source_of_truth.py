"""Every surface that names a price, a route or a tool must agree with ``pricing.OPERATIONS``.

"Paid means listed": a route or tool missing from the table is silently free; a table row with
no route is advertised and broken. Prices are custom here so a surface that reads defaults, or
another copy of the table, cannot pass.
"""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import jsonschema
import mcp
import pytest
from fastapi.routing import APIRoute

import aimart_testkit as kit
from aimart_parquet import quantum
from aimart_parquet.api.app import create_app
from aimart_parquet.payments import Payments
from aimart_parquet.payments.bazaar import EXAMPLES
from aimart_parquet.payments.pricing import BY_KEY, BY_TOOL, OPERATIONS, active_operations

if TYPE_CHECKING:
    from collections.abc import Callable

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

FREE_POSTS = {"/v1/verify"}
FREE_TOOLS = {"verify_provenance"}
PRICES = {
    "price_convert": "$0.011",
    "price_inspect": "$0.0021",
    "price_preview": "$0.0022",
    "price_query": "$0.0051",
    "price_quantum_compress": "$0.51",
    "price_quantum_portfolio": "$1.01",
}


def test_table_is_well_formed() -> None:
    assert len({op.key for op in OPERATIONS}) == len(OPERATIONS)
    assert len({op.route for op in OPERATIONS}) == len(OPERATIONS)
    assert set(BY_KEY) == {op.key for op in OPERATIONS}
    assert set(BY_TOOL) == {op.tool for op in OPERATIONS}
    for op in OPERATIONS:
        assert op.path.startswith("/v1/"), op.key
        assert op.method == "POST", op.key
        assert op.price_setting.startswith("price_"), op.key
        assert op.key in EXAMPLES, op.key


@pytest.mark.parametrize("op", OPERATIONS, ids=lambda op: op.key)
def test_bazaar_example_validates_against_its_schema(op: Any) -> None:
    example = EXAMPLES[op.key]
    jsonschema.Draft202012Validator.check_schema(example["input_schema"])
    jsonschema.validate(example["input"], example["input_schema"])
    assert "$ref" not in str(example["input_schema"])  # catalogs do not resolve references
    assert example["output"]


async def test_every_operation_is_wired_everywhere_with_one_price(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(**PRICES)
    async with kit.running_app(settings) as h:
        spec = (await h.http.get("/openapi.json")).json()
        pricing = {o["operation"]: o for o in (await h.http.get("/pricing")).json()["operations"]}
        llms = (await h.http.get("/llms.txt")).text
        well_known = (await h.http.get("/.well-known/x402")).json()["resources"]
        card = {
            t["name"]: t
            for t in (await h.http.get("/.well-known/mcp/server-card.json")).json()["tools"]
        }
        async with kit.mcp_over_http(h.app) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
        quoted = {}
        for op in active_operations():
            r = await h.http.request(op.method, op.path, json={})
            quoted[op.key] = kit.payment_required(r)["accepts"][0]["amount"]
        routes = {
            (method, route.path)
            for route in h.app.routes
            if isinstance(route, APIRoute)
            for method in route.methods
        }
        gate = h.payments.mcp_extension()

    for op in active_operations():
        price = settings_price = getattr(settings, op.price_setting)
        assert price == PRICES[op.price_setting]
        assert (op.method, op.path) in routes, op.key
        assert spec["paths"][op.path]["post"]["x-payment-info"]["price"]["amount"] == price[1:]
        assert pricing[op.key]["price"] == price
        assert pricing[op.key]["http"] == op.route
        assert pricing[op.key]["mcp_tool"] == op.tool
        assert f"`{op.route}` ({price})" in llms
        assert f"http://test{op.path}" in well_known
        assert op.tool in tools, op.key
        assert BY_TOOL[op.tool].price(settings) in tools[op.tool].description
        assert card[op.tool]["price"] == BY_TOOL[op.tool].price(settings)
        assert gate.settings()["prices"][op.tool] == BY_TOOL[op.tool].price(settings)
        assert gate.payments.tool_operation(op.tool) is not None
        whole, _, frac = settings_price[1:].partition(".")
        assert quoted[op.key] == str(int(whole) * 10**6 + int(frac.ljust(6, "0")))

    # Two operations that share a tool must cost the same, or MCP and REST disagree.
    for tool in {op.tool for op in OPERATIONS}:
        assert len({op.price_setting for op in OPERATIONS if op.tool == tool}) == 1, tool

    # The other direction: nothing paid-looking exists outside the table.
    paid_routes = {(op.method, op.path) for op in active_operations()}
    for method, path in routes:
        if path.startswith("/v1/") and method == "POST":
            assert (method, path) in paid_routes or path in FREE_POSTS, path
    assert set(tools) - {op.tool for op in active_operations()} == FREE_TOOLS


async def test_free_route_set_is_explicit(make_settings: Callable[..., Settings]) -> None:
    async with kit.running_app(make_settings()) as h:
        for path in FREE_POSTS:
            r = await h.http.post(path, json={"manifest": {}, "signature": "AAAA"})
            assert r.status_code == 200, path
    assert h.facilitator is not None
    assert h.facilitator.verified == []


async def test_without_the_quantum_extra_no_surface_mentions_quantum(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate an install without pennylane the way it really looks: find_spec returns None.
    real = importlib.util.find_spec

    def find_spec(name: str, *args: Any) -> Any:
        return None if name == "pennylane" else real(name, *args)

    monkeypatch.setattr(
        quantum, "importlib", SimpleNamespace(util=SimpleNamespace(find_spec=find_spec))
    )
    assert not quantum.available()
    settings = make_settings()
    app = create_app(settings, payments=Payments(settings, [kit.FakeFacilitator()]))
    async with app.router.lifespan_context(app):
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            spec = (await http.get("/openapi.json")).json()
            pricing = (await http.get("/pricing")).json()
            llms = (await http.get("/llms.txt")).text
            unpaid = await http.post("/v1/quantum/portfolio", json={})
        async with kit.mcp_over_http(app) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
    assert not [p for p in spec["paths"] if "quantum" in p]
    assert not [o for o in pricing["operations"] if "quantum" in o["operation"]]
    assert "quantum" not in llms
    assert not [t for t in tools if "quantum" in t]
    assert unpaid.status_code != 402  # not advertised, not charged, not served


async def test_mcp_tool_descriptions_name_the_price_in_remote_mode(
    make_settings: Callable[..., Settings],
) -> None:
    from aimart_parquet.artifacts import build_store
    from aimart_parquet.mcp.server import build_mcp
    from aimart_parquet.service import Service

    settings = make_settings(**PRICES)
    server = build_mcp(Service(settings, build_store(settings)), settings, local_mode=False)
    async with mcp.Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    for op in active_operations():
        assert f"Price: {op.price(settings)} USDC" in tools[op.tool].description
