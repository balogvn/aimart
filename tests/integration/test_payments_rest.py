"""REST paywall, asserted on the wire: what an agent decodes from PAYMENT-REQUIRED and what the
facilitator is asked to verify and settle.

Expected amounts are literal atomic-unit strings, never ``usd_to_atomic(price)``: a test that
computes the expectation with the code under test cannot see that code being wrong.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import anyio
import jsonschema
import pytest
from pydantic import ValidationError

import aimart_testkit as kit
from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.payments import DEFAULT_FACILITATORS, Payments, facilitator_urls
from aimart_parquet.payments.bazaar import EXAMPLES
from aimart_parquet.payments.pricing import BY_KEY, OPERATIONS, active_operations, usd_to_atomic
from aimart_parquet.service import SourceRef

if TYPE_CHECKING:
    from collections.abc import Callable

    from aimart_parquet.config import Settings

pytestmark = pytest.mark.anyio

# Default prices in USDC atomic units (6 decimals), written out by hand.
DEFAULT_ATOMIC = {
    "convert": "20000",
    "convert_upload": "20000",
    "inspect": "2000",
    "preview": "2000",
    "query": "5000",
    "quantum_compress": "500000",
    "quantum_portfolio": "1000000",
}

# 28 daily rows of 4 numeric columns: valid input for every operation, quantum ones included.
PRICES_CSV = (
    "date,a,b,c,d\n"
    + "\n".join(
        f"2025-01-{d + 1:02d},{100 + d * 0.3:.2f},{50 + (d % 5):.2f},"
        f"{80 - d * 0.1:.2f},{20 + (d % 3) * 0.5:.2f}"
        for d in range(28)
    )
    + "\n"
).encode()

UNKNOWN_ARTIFACT = "Zz" * 12  # well-formed, never issued


def request_for(key: str, artifact_id: str) -> dict[str, Any]:
    """httpx keyword arguments for a valid call to each operation."""
    builders: dict[str, Callable[[], dict[str, Any]]] = {
        "convert": lambda: {"json": {"content_base64": kit.b64(PRICES_CSV), "filename": "p.csv"}},
        "convert_upload": lambda: {
            "files": {"file": ("p.csv", PRICES_CSV, "text/csv")},
            "data": {"options": '{"sort_by": ["date"]}'},
        },
        "inspect": lambda: {"json": {"artifact_id": artifact_id}},
        "preview": lambda: {"json": {"artifact_id": artifact_id, "limit": 3, "format": "csv"}},
        "query": lambda: {
            "json": {"artifact_id": artifact_id, "sql": "SELECT count(*) AS n FROM data"}
        },
        "quantum_compress": lambda: {
            "json": {"artifact_id": artifact_id, "options": {"components": 2, "landmarks": 16}}
        },
        "quantum_portfolio": lambda: {
            "json": {"artifact_id": artifact_id, "options": {"budget": 2, "steps": 10}}
        },
    }
    return builders[key]()


async def make_artifact(harness: kit.AppHarness) -> str:
    """Create an artifact through the service directly: free, so it settles nothing."""
    result = await harness.app.state.service.convert(
        SourceRef(content_base64=kit.b64(PRICES_CSV), filename="p.csv"), ConvertOptions()
    )
    return str(result.artifact.id)


ACTIVE = active_operations()


def test_every_operation_has_a_request_builder_and_a_literal_price() -> None:
    # A new operation without a wire test would otherwise be skipped silently.
    for op in OPERATIONS:
        assert op.key in DEFAULT_ATOMIC, op.key
        request_for(op.key, UNKNOWN_ARTIFACT)


# ------------------------------------------------------------------------------ unpaid -> 402
@pytest.mark.parametrize("op", ACTIVE, ids=lambda op: op.key)
async def test_unpaid_call_quotes_the_price_on_the_wire(
    op: Any, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    r = await paid_app.http.request(op.method, op.path, **request_for(op.key, UNKNOWN_ARTIFACT))
    pr = kit.payment_required(r)

    assert pr["x402Version"] == 2
    assert pr["resource"]["url"] == f"http://test{op.path}"
    assert pr["resource"]["description"] == op.summary
    assert pr["resource"]["mimeType"] == "application/json"
    assert pr["resource"]["serviceName"] == "AImart Parquet"
    [accept] = pr["accepts"]
    assert accept["scheme"] == "exact"
    assert accept["amount"] == DEFAULT_ATOMIC[op.key]
    assert accept["amount"] == str(usd_to_atomic(op.price(paid_app.settings)))
    assert accept["payTo"] == kit.PAY_TO_EVM
    assert accept["network"] == kit.BASE_SEPOLIA
    assert accept["asset"] == kit.USDC_BASE_SEPOLIA
    assert accept["maxTimeoutSeconds"] == 300
    assert accept["extra"]["name"] == "USDC"
    assert accept["extra"]["version"] == "2"
    assert "tag" not in accept["extra"]  # no bazaar_tag configured

    # Nothing was verified or settled for a request that carried no payment.
    assert facilitator.verified == []
    assert facilitator.settled == []


@pytest.mark.parametrize("op", ACTIVE, ids=lambda op: op.key)
async def test_bazaar_extension_is_present_and_self_consistent(
    op: Any, paid_app: kit.AppHarness
) -> None:
    # Catalogs validate info against schema strictly and silently drop entries that fail.
    r = await paid_app.http.request(op.method, op.path, **request_for(op.key, UNKNOWN_ARTIFACT))
    bazaar = kit.payment_required(r)["extensions"]["bazaar"]
    jsonschema.Draft202012Validator.check_schema(bazaar["schema"])
    jsonschema.validate(bazaar["info"], bazaar["schema"], cls=jsonschema.Draft202012Validator)

    example = EXAMPLES[op.key]
    info_input = bazaar["info"]["input"]
    assert info_input["type"] == "http"
    assert info_input["method"] == op.method
    assert info_input["bodyType"] == ("form-data" if op.key == "convert_upload" else "json")
    assert info_input["body"] == example["input"]
    jsonschema.validate(info_input["body"], example["input_schema"])
    assert bazaar["info"]["output"]["example"] == example["output"]


async def test_bazaar_tag_is_placed_in_accepts_extra(
    make_settings: Callable[..., Settings],
) -> None:
    # The leaderboard filters on accepts[].extra.tag, not resource.tags: two fields, one name.
    settings = make_settings(bazaar_tag="aimart-test")
    async with kit.running_app(settings) as h:
        r = await h.http.post("/v1/inspect", json={"artifact_id": UNKNOWN_ARTIFACT})
        pr = kit.payment_required(r)
    assert pr["accepts"][0]["extra"]["tag"] == "aimart-test"
    assert pr["accepts"][0]["extra"]["name"] == "USDC"
    assert pr["resource"]["tags"] == ["data", "parquet", "etl", "csv", "mcp"]


async def test_custom_prices_reach_the_wire_exactly(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(
        price_convert="$2", price_inspect="$0.123456", price_query="$0.1", price_preview="$0.002"
    )
    async with kit.running_app(settings) as h:
        amounts = {}
        for key in ("convert", "inspect", "query", "preview"):
            op = BY_KEY[key]
            r = await h.http.post(op.path, **request_for(key, UNKNOWN_ARTIFACT))
            amounts[key] = kit.payment_required(r)["accepts"][0]["amount"]
    assert amounts == {
        "convert": "2000000",
        "inspect": "123456",
        "query": "100000",
        "preview": "2000",
    }


async def test_two_payees_are_both_offered(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings(pay_to_svm=kit.PAY_TO_SVM)
    fake = kit.FakeFacilitator(networks=(kit.BASE_SEPOLIA, kit.SOLANA_DEVNET))
    async with kit.running_app(settings, facilitator=fake) as h:
        r = await h.http.post("/v1/inspect", json={"artifact_id": UNKNOWN_ARTIFACT})
        accepts = {a["network"]: a for a in kit.payment_required(r)["accepts"]}
        pricing = (await h.http.get("/pricing")).json()
    assert set(accepts) == {kit.BASE_SEPOLIA, kit.SOLANA_DEVNET}
    svm = accepts[kit.SOLANA_DEVNET]
    assert svm["payTo"] == kit.PAY_TO_SVM
    assert svm["asset"] == kit.USDC_SOLANA_DEVNET
    assert svm["amount"] == "2000"
    assert svm["extra"]["feePayer"] == kit.FEE_PAYER
    assert accepts[kit.BASE_SEPOLIA]["payTo"] == kit.PAY_TO_EVM
    assert {(n["network"], n["payTo"]) for n in pricing["networks"]} == {
        (kit.BASE_SEPOLIA, kit.PAY_TO_EVM),
        (kit.SOLANA_DEVNET, kit.PAY_TO_SVM),
    }


async def test_mainnet_mode_quotes_base_mainnet_usdc(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(network_mode="mainnet")
    fake = kit.FakeFacilitator(networks=(kit.BASE_MAINNET,))
    async with kit.running_app(settings, facilitator=fake) as h:
        r = await h.http.post("/v1/convert", json={"url": "https://example.com/a.csv"})
        [accept] = kit.payment_required(r)["accepts"]
    assert accept["network"] == kit.BASE_MAINNET
    assert accept["asset"] == kit.USDC_BASE_MAINNET
    assert accept["amount"] == "20000"


# ------------------------------------------------------------------------------ paid -> 200
@pytest.mark.parametrize("op", ACTIVE, ids=lambda op: op.key)
async def test_paid_call_succeeds_and_settles_the_quoted_amount_once(
    op: Any, paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    artifact_id = await make_artifact(paid_app)
    r = await paid_app.paying.request(op.method, op.path, **request_for(op.key, artifact_id))
    assert r.status_code == 200, r.text

    settlement = kit.payment_response(r)
    assert settlement["success"] is True
    assert settlement["transaction"] == kit.TX_HASH
    assert settlement["network"] == kit.BASE_SEPOLIA

    assert len(facilitator.verified) == 1
    assert len(facilitator.settled) == 1
    _, requirements = facilitator.settled[0]
    assert requirements.amount == DEFAULT_ATOMIC[op.key]
    assert requirements.pay_to == kit.PAY_TO_EVM
    assert requirements.network == kit.BASE_SEPOLIA
    assert requirements.asset == kit.USDC_BASE_SEPOLIA
    payload, _ = facilitator.settled[0]
    assert payload.accepted.amount == DEFAULT_ATOMIC[op.key]

    body = r.json()
    if op.key.startswith("convert"):
        assert body["report"]["rows"] == 28
        assert body["artifact"]["provenance"]["algorithm"] == "ML-DSA-65"
    elif op.key == "query":
        assert body["rows"] == [[28]]


@pytest.mark.parametrize(
    ("key", "body", "status", "code"),
    [
        ("inspect", {"artifact_id": UNKNOWN_ARTIFACT}, 404, "artifact_not_found"),
        ("inspect", {}, 400, "invalid_request"),
        ("inspect", {"artifact_id": "bad id!"}, 404, "artifact_not_found"),
        ("query", {"artifact_id": "ARTIFACT", "sql": "DROP TABLE data"}, 400, "query_rejected"),
        # A schema-invalid body ("limit": 0) is now refused before any quote (see
        # test_fix_money_gate.py); this one passes the schema and fails in the service.
        ("preview", {"artifact_id": "ARTIFACT", "columns": ["nope"]}, 400, "invalid_request"),
        ("convert", {"url": "http://127.0.0.1/data.csv"}, 422, "url_not_allowed"),
        ("convert", {"url": "ftp://example.com/data.csv"}, 422, "url_not_allowed"),
        (
            "convert",
            {"content_base64": kit.b64(b"a,b\n1,2\n"), "url": "https://example.com/a.csv"},
            400,
            "invalid_request",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) and len(v) < 30 else None,
)
async def test_paid_call_that_fails_is_verified_but_never_settled(
    key: str,
    body: dict[str, Any],
    status: int,
    code: str,
    paid_app: kit.AppHarness,
    facilitator: kit.FakeFacilitator,
) -> None:
    artifact_id = await make_artifact(paid_app)
    body = {k: (artifact_id if v == "ARTIFACT" else v) for k, v in body.items()}
    r = await paid_app.paying.post(BY_KEY[key].path, json=body)
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code
    assert "payment-response" not in r.headers
    assert len(facilitator.verified) == 1  # the payment was real and checked...
    assert facilitator.settled == []  # ...but a failed call is never charged


async def test_paid_call_that_times_out_is_not_settled(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    import aimart_parquet.service as service_module

    def slow_inspect(*_: Any, **__: Any) -> Any:
        time.sleep(0.5)
        raise AssertionError("must have been abandoned at the deadline")

    monkeypatch.setattr(service_module, "inspect_parquet", slow_inspect)
    settings = make_settings(query_timeout_s=0.05)
    fake = kit.FakeFacilitator()
    async with kit.running_app(settings, facilitator=fake) as h:
        artifact_id = await make_artifact(h)
        r = await h.paying.post("/v1/inspect", json={"artifact_id": artifact_id})
    assert r.status_code == 504, r.text
    assert r.json()["error"]["code"] == "timeout"
    assert len(fake.verified) == 1
    assert fake.settled == []


async def test_failed_upload_conversion_is_not_settled(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    # Valid options the conversion then refuses. (Options that are not ConvertOptions JSON are
    # now refused before the quote: tests/integration/test_fix_money_upload_check.py.)
    r = await paid_app.paying.post(
        "/v1/convert/upload",
        files={"file": ("p.csv", PRICES_CSV, "text/csv")},
        data={"options": '{"sort_by": ["no_such_column"]}'},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert len(facilitator.verified) == 1
    assert facilitator.settled == []


# ------------------------------------------------------------------------------ facilitator says no
async def test_settlement_failure_returns_402_and_withholds_the_result(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    facilitator.settle_ok = False
    r = await paid_app.paying.post("/v1/convert", **request_for("convert", ""))
    assert r.status_code == 402
    assert "artifact" not in r.text
    assert "report" not in r.text
    assert "download_url" not in r.text
    assert len(facilitator.settled) >= 1
    assert all(req.amount == "20000" for _, req in facilitator.settled)


async def test_settlement_exception_returns_402_and_withholds_the_result(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    facilitator.settle_error = RuntimeError("chain congested")
    r = await paid_app.paying.post("/v1/convert", **request_for("convert", ""))
    assert r.status_code == 402
    assert "artifact" not in r.text


async def test_invalid_payment_is_refused_before_any_work(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    facilitator.valid = False
    root = paid_app.settings.artifact_dir
    before = {p.name for p in root.iterdir()}
    r = await paid_app.paying.post("/v1/convert", **request_for("convert", ""))
    assert r.status_code == 402
    assert "artifact" not in r.text
    assert len(facilitator.verified) >= 1
    assert facilitator.settled == []
    assert {p.name for p in root.iterdir()} == before  # no artifact was produced


async def test_garbage_payment_signature_header_is_refused(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    r = await paid_app.http.post(
        "/v1/inspect",
        json={"artifact_id": UNKNOWN_ARTIFACT},
        headers={"PAYMENT-SIGNATURE": "bm90IGpzb24="},
    )
    assert r.status_code in {400, 402}
    assert facilitator.settled == []


async def test_facilitator_outage_is_503_and_charges_nothing(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    facilitator.supported_error = RuntimeError("facilitator down")
    r = await paid_app.paying.post("/v1/convert", **request_for("convert", ""))
    assert r.status_code == 503
    error = r.json()["error"]
    assert error["code"] == "payments_unavailable"
    assert "not charged" in error["hint"]
    assert "payment-required" not in r.headers
    assert facilitator.verified == []
    assert facilitator.settled == []

    # Initialization is lazy and retried: the next request after recovery is quoted normally.
    facilitator.supported_error = None
    r = await paid_app.http.post("/v1/inspect", json={"artifact_id": UNKNOWN_ARTIFACT})
    assert kit.payment_required(r)["accepts"][0]["amount"] == "2000"
    assert facilitator.supported_calls == 2


async def test_facilitator_is_initialized_once_under_concurrency(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    statuses: list[int] = []

    async def hit() -> None:
        r = await paid_app.http.post("/v1/inspect", json={"artifact_id": UNKNOWN_ARTIFACT})
        statuses.append(r.status_code)

    async with anyio.create_task_group() as tg:
        for _ in range(8):
            tg.start_soon(hit)
    assert statuses == [402] * 8
    assert facilitator.supported_calls == 1


# ------------------------------------------------------------------------------ free routes
FREE_HTTP: list[tuple[str, str, dict[str, Any]]] = [
    ("GET", "/", {}),
    ("GET", "/health", {}),
    ("GET", "/pricing", {}),
    ("GET", "/llms.txt", {}),
    ("GET", "/openapi.json", {}),
    ("GET", "/docs", {}),
    ("GET", "/.well-known/x402", {}),
    ("GET", "/.well-known/mcp/server-card.json", {}),
    ("GET", "/.well-known/aimart-signing-key", {}),
    ("POST", "/v1/verify", {"json": {"manifest": {"a": 1}, "signature": "AAAA"}}),
    ("GET", f"/v1/artifacts/{UNKNOWN_ARTIFACT}/x.parquet?exp=1&sig=00", {}),
]


async def test_free_routes_never_ask_for_payment(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    for method, path, kwargs in FREE_HTTP:
        r = await paid_app.http.request(method, path, **kwargs)
        assert r.status_code != 402, (method, path)
        assert "payment-required" not in r.headers, (method, path)
        assert r.status_code < 500, (method, path, r.text)
    async with kit.mcp_over_http(paid_app.app) as client:
        tools = await client.list_tools()
    assert {t.name for t in tools.tools} >= {op.tool for op in ACTIVE}
    # Free routes never even need the facilitator: an outage cannot take discovery down.
    assert facilitator.supported_calls == 0
    assert facilitator.verified == []
    assert facilitator.settled == []


async def test_free_routes_survive_a_facilitator_outage(
    paid_app: kit.AppHarness, facilitator: kit.FakeFacilitator
) -> None:
    facilitator.supported_error = RuntimeError("down")
    for path in ("/pricing", "/llms.txt", "/.well-known/x402", "/openapi.json"):
        assert (await paid_app.http.get(path)).status_code == 200, path


# ------------------------------------------------------------------------------ price floors
async def test_price_below_facilitator_floor_is_logged_as_an_error(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    fake = kit.FakeFacilitator(kind_extra={kit.BASE_SEPOLIA: {"minPaymentAmountAtomic": "5000"}})
    payments = Payments(settings, [fake])
    with caplog.at_level(logging.ERROR, logger="aimart.payments"):
        await payments.ensure_initialized()
    below = {
        r.operation: r for r in caplog.records if r.name == "aimart.payments" and r.levelno >= 40
    }
    # inspect and preview (2000) are under the 5000 floor; query (5000) sits exactly on it.
    assert set(below) == {"inspect", "preview"}
    assert below["inspect"].floor_atomic == 5000
    assert below["inspect"].price_atomic == 2000
    assert below["inspect"].network == kit.BASE_SEPOLIA
    assert payments.is_initialized()


async def test_no_floor_means_no_price_error(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    payments = Payments(settings, [kit.FakeFacilitator()])
    with caplog.at_level(logging.ERROR, logger="aimart.payments"):
        await payments.ensure_initialized()
        await payments.ensure_initialized()  # idempotent
    assert [r for r in caplog.records if r.levelno >= 40] == []


# ------------------------------------------------------------------------------ configuration
def test_payments_without_a_payee_refuse_to_start(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ValueError, match="no payee is configured"):
        Payments(make_settings(pay_to_evm=None), [kit.FakeFacilitator()])


def test_app_factory_refuses_paid_mode_without_a_payee(
    make_settings: Callable[..., Settings],
) -> None:
    from aimart_parquet.api.app import create_app

    with pytest.raises(ValueError, match="AIMART_PAY_TO_EVM"):
        create_app(make_settings(pay_to_evm=None))


def test_facilitator_urls_defaults_and_overrides(make_settings: Callable[..., Settings]) -> None:
    assert facilitator_urls(make_settings(facilitator_urls=None)) == [
        "https://x402.org/facilitator"
    ]
    assert facilitator_urls(make_settings(facilitator_urls=None, network_mode="mainnet")) == [
        "https://facilitator.heurist.xyz",
        "https://x402.dexter.cash",
    ]
    assert facilitator_urls(make_settings(facilitator_urls=" https://a.example , ,https://b ")) == [
        "https://a.example",
        "https://b",
    ]
    assert DEFAULT_FACILITATORS  # both modes have a default


async def test_from_settings_builds_http_facilitators(
    make_settings: Callable[..., Settings],
) -> None:
    from x402.http import HTTPFacilitatorClient

    payments = Payments.from_settings(make_settings(facilitator_urls="https://a.invalid,https://b"))
    assert len(payments._facilitators) == 2
    assert all(isinstance(c, HTTPFacilitatorClient) for c in payments._facilitators)
    assert not payments.is_initialized()
    await payments.aclose()


def test_discovery_document_lists_every_paid_resource(settings: Settings) -> None:
    doc = Payments(settings, [kit.FakeFacilitator()]).discovery_document()
    assert doc["version"] == 1
    assert doc["resources"] == [f"http://test{op.path}" for op in ACTIVE]


@pytest.mark.parametrize(
    ("price", "atomic"),
    [
        ("$0.01", 10000),
        ("$0.002", 2000),
        ("$1", 1000000),
        ("$1.00", 1000000),
        ("$0.000001", 1),
        ("$12.345678", 12345678),
        ("$.5", 500000),
        ("0.25", 250000),
    ],
)
def test_usd_to_atomic_is_exact(price: str, atomic: int) -> None:
    assert usd_to_atomic(price) == atomic


def test_usd_to_atomic_refuses_sub_atomic_precision() -> None:
    with pytest.raises(ValueError, match="precision"):
        usd_to_atomic("$0.0000001")


def test_settings_reject_bad_payees_and_prices(make_settings: Callable[..., Settings]) -> None:
    for overrides in (
        {"pay_to_evm": "0x123"},
        {"pay_to_evm": "ab" * 21},
        {"pay_to_evm": "0x" + "zz" * 20},
        {"pay_to_svm": "0OIl" * 10},
        {"pay_to_svm": "short"},
        {"price_convert": "0.01"},
        {"price_inspect": "$0.0000001"},
        {"price_query": "$-1"},
        {"price_quantum_compress": "$1,00"},
        {"price_quantum_portfolio": "free"},
    ):
        with pytest.raises(ValidationError):
            make_settings(**overrides)


def test_settings_normalize_the_public_base_url(make_settings: Callable[..., Settings]) -> None:
    assert (
        make_settings(public_base_url="https://x.example/").public_base_url == "https://x.example"
    )
    ok = make_settings(pay_to_svm=kit.PAY_TO_SVM, price_convert="$3")
    assert ok.pay_to_svm == kit.PAY_TO_SVM
