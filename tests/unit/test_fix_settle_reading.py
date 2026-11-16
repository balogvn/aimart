"""The settle answer's reading rules, one by one, and the SDK internals they stand on.

``payments/settle.py`` replaces the SDK's ``HTTPFacilitatorClient._settle_http`` (status 200
or an error, then a strict parse) because a facilitator that settled and answered outside the
schema left the payer charged for a 402. The end-to-end proof is
``integration/test_fix_settle_answers.py``; here are the rules themselves, the request the
replacement sends, and the tripwire for an SDK upgrade.
"""

from __future__ import annotations

import base64
import inspect
import json
import logging
from typing import Any

import httpx
import pytest
from x402 import SettleResponse
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.http import facilitator_client as sdk_client
from x402.schemas import PaymentPayload, PaymentRequirements, ResourceInfo

import aimart_testkit as kit
from aimart_parquet.payments import settle as settle_module
from aimart_parquet.payments.settle import (
    UNREADABLE,
    LenientSettleClient,
    read_settle_answer,
)

pytestmark = pytest.mark.anyio

N = kit.BASE_SEPOLIA
TX = kit.TX_HASH

REQUIREMENTS = PaymentRequirements(
    scheme="exact",
    network=N,
    asset=kit.USDC_BASE_SEPOLIA,
    amount="20000",
    pay_to=kit.PAY_TO_EVM,
    max_timeout_seconds=300,
    extra={"name": "USDC", "version": "2"},
)
PAYLOAD = PaymentPayload(
    x402_version=2,
    payload={"signature": "0x00", "authorization": {"from": kit.PAYER, "value": "20000"}},
    accepted=REQUIREMENTS,
    resource=ResourceInfo(url="http://test/v1/convert"),
)


def client_over(
    cls: type[HTTPFacilitatorClient], answer: httpx.Response, seen: list[httpx.Request]
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer

    return cls(
        FacilitatorConfig(
            url="https://f.test/base/",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
    )


# ------------------------------------------------------------------------------ the rules
@pytest.mark.parametrize(
    ("status", "data", "expected"),
    [
        # a. success exactly true, anything else as it can be read.
        (200, {"success": True}, {"success": True, "transaction": "", "network": N}),
        (
            200,
            {"success": True, "transaction": "", "txHash": "", "transactionHash": " 0xab "},
            {"success": True, "transaction": "0xab", "network": N},
        ),
        (
            200,
            {"success": True, "transaction": TX, "txHash": "0xother", "amount": 20000},
            {"success": True, "transaction": TX, "network": N, "amount": "20000"},
        ),
        (
            299,
            {"success": True, "network": "eip155:8453", "amount": 0.5, "payer": kit.PAYER},
            {
                "success": True,
                "transaction": "",
                "network": "eip155:8453",
                "amount": "0.5",
                "payer": kit.PAYER,
            },
        ),
        (
            200,
            {
                "success": True,
                "network": 8453,
                "amount": True,
                "payer": ["x"],
                "extensions": "x",
                "extra": 3,
                "transaction": 7,
                "unknown": {"deep": [1]},
            },
            {"success": True, "transaction": "", "network": N},
        ),
        (
            200,
            {
                "success": True,
                "network": N,
                "amount": {"value": 1},
                "extensions": {"a": 1},
                "extra": {"gasUsed": 51000},
                "errorReason": None,
            },
            {
                "success": True,
                "transaction": "",
                "network": N,
                "extensions": {"a": 1},
                "extra": {"gasUsed": 51000},
            },
        ),
        # b. success exactly false: a refusal, its reason kept as text.
        (200, {"success": False}, {"success": False, "transaction": "", "network": N}),
        (
            200,
            {"success": False, "error": 4001, "errorMessage": "user  rejected\n the transfer"},
            {
                "success": False,
                "transaction": "",
                "network": N,
                "errorReason": "4001",
                "errorMessage": "user rejected the transfer",
            },
        ),
        (
            200,
            {"success": False, "errorReason": "", "error_reason": "first", "error": "second"},
            {"success": False, "transaction": "", "network": N, "errorReason": "first"},
        ),
        (
            201,
            {"success": False, "errorReason": "settlement_pending", "transaction": TX},
            {
                "success": False,
                "transaction": TX,
                "network": N,
                "errorReason": "settlement_pending",
            },
        ),
        # What the SDK reads as false stays a refusal, transaction id or not.
        (200, {"success": "no", "txHash": TX}, {"success": False, "transaction": TX, "network": N}),
        (200, {"success": 0.0, "txHash": TX}, {"success": False, "transaction": TX, "network": N}),
        # c. no boolean success: a transaction id and no error is a settlement.
        (200, {"transactionHash": TX}, {"success": True, "transaction": TX, "network": N}),
        (
            200,
            {"success": 1, "txHash": TX, "error": None, "errorMessage": ""},
            {"success": True, "transaction": TX, "network": N},
        ),
        (202, {"success": "yes", "txHash": TX}, {"success": True, "transaction": TX, "network": N}),
    ],
)
def test_an_answer_the_sdk_rejects_is_read_leniently(
    status: int, data: Any, expected: dict[str, Any]
) -> None:
    settled, reading = read_settle_answer(status, data, N)
    assert reading == "lenient"
    assert settled.model_dump(by_alias=True, exclude_none=True) == expected


@pytest.mark.parametrize(
    ("data", "transaction", "reason"),
    [
        ({}, "", UNREADABLE),
        ({"success": None}, "", UNREADABLE),
        ({"success": "perhaps", "amount": "20000", "network": N}, "", UNREADABLE),
        ({"success": 1}, "", UNREADABLE),  # truthy is not true: no transaction id, no delivery
        ({"success": "true", "transaction": "   "}, "", UNREADABLE),
        ({"settled": True, "status": "success"}, "", UNREADABLE),
        ({"transaction": TX, "error": "reverted"}, TX, "reverted"),
        ({"transaction": TX, "errorReason": {"code": 3}}, TX, '{"code":3}'),
        ({"txHash": TX, "error_message": "out of gas"}, TX, UNREADABLE),
        ({"error": "x" * 5000}, "", "x" * 200),  # a caller gets an excerpt, not a page
        ([], "", UNREADABLE),
        ([{"success": True, "transaction": TX}], "", UNREADABLE),
        ("success", "", UNREADABLE),
        (None, "", UNREADABLE),
        (True, "", UNREADABLE),
        (1, "", UNREADABLE),
    ],
)
def test_an_answer_that_does_not_say_settled_is_not_a_settlement(
    data: Any, transaction: str, reason: str
) -> None:
    for status in (200, 201):
        settled, reading = read_settle_answer(status, data, N)
        assert reading == "unreadable"
        assert settled.success is False
        assert (settled.transaction, settled.network, settled.error_reason) == (
            transaction,
            N,
            reason,
        )
        assert settled.error_message == settle_module.UNREADABLE_MESSAGE


@pytest.mark.parametrize(
    "data",
    [
        {"success": True, "transaction": TX, "network": N},
        {"success": True, "transaction": "", "network": "base-sepolia", "amount": "1"},
        {"success": False, "errorReason": "insufficient_funds", "transaction": "", "network": N},
        # The SDK's model takes these for booleans; inside its schema that is not changed.
        {"success": "true", "transaction": TX, "network": N},
        {"success": "false", "transaction": TX, "network": N},
    ],
)
def test_an_answer_in_the_sdk_schema_is_the_sdks_reading(data: dict[str, Any]) -> None:
    settled, reading = read_settle_answer(200, data, "another:network")
    assert reading == "schema"
    assert settled == SettleResponse.model_validate(data)


# ------------------------------------------------------------------------------ the client
async def test_the_settle_request_is_the_one_the_sdk_sends() -> None:
    """Same URL, headers and body as ``HTTPFacilitatorClient.settle``: only the reading of the
    answer differs. Fails when an SDK upgrade changes what it sends to /settle."""
    answer = {"success": True, "transaction": TX, "network": N}
    ours: list[httpx.Request] = []
    sdks: list[httpx.Request] = []
    mine = await client_over(LenientSettleClient, httpx.Response(200, json=answer), ours).settle(
        PAYLOAD, REQUIREMENTS
    )
    theirs = await client_over(
        HTTPFacilitatorClient, httpx.Response(200, json=answer), sdks
    ).settle(PAYLOAD, REQUIREMENTS)
    assert mine == theirs
    (a,), (b,) = ours, sdks
    assert (
        (a.method, str(a.url)) == (b.method, str(b.url)) == ("POST", "https://f.test/base/settle")
    )
    assert a.content == b.content
    assert dict(a.headers) == dict(b.headers)
    body = json.loads(a.content)
    assert set(body) == {"x402Version", "paymentPayload", "paymentRequirements"}
    assert body["paymentRequirements"]["amount"] == "20000"


async def test_extension_responses_are_attached_as_the_sdk_attaches_them() -> None:
    """The EXTENSION-RESPONSES header goes to the SDK's hooks and is never serialized to the
    payer: for a schema-valid answer exactly as before, and for a lenient one too."""
    sidechannel = {"bazaar": {"status": "listed"}}
    headers = {"EXTENSION-RESPONSES": base64.b64encode(json.dumps(sidechannel).encode()).decode()}
    for answer in ({"success": True, "transaction": TX, "network": N}, {"success": True}):
        response = httpx.Response(200, json=answer, headers=headers)
        mine = await client_over(LenientSettleClient, response, []).settle(PAYLOAD, REQUIREMENTS)
        assert mine.extension_responses == sidechannel
        assert "extension" not in json.dumps(mine.model_dump(by_alias=True, exclude_none=True))
    theirs = await client_over(
        HTTPFacilitatorClient,
        httpx.Response(200, json=answer | {"transaction": "", "network": N}, headers=headers),
        [],
    ).settle(PAYLOAD, REQUIREMENTS)
    assert theirs.extension_responses == sidechannel
    garbled = httpx.Response(200, json={"success": True}, headers={"EXTENSION-RESPONSES": "%%%"})
    settled = await client_over(LenientSettleClient, garbled, []).settle(PAYLOAD, REQUIREMENTS)
    assert (settled.success, settled.extension_responses) == (True, None)


async def test_nothing_raises_once_a_2xx_was_received(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The transfer may have been made by then: a fault in the reading, or in the SDK's side
    channel, is answered as "not settled" (or leaves the settlement standing), never raised."""

    def broken(*_: Any) -> Any:
        raise RuntimeError("a fault in the reading")

    response = httpx.Response(200, json={"success": True, "transaction": TX, "network": N})
    monkeypatch.setattr(settle_module, "_attach_extension_responses", broken)
    settled = await client_over(LenientSettleClient, response, []).settle(PAYLOAD, REQUIREMENTS)
    assert (settled.success, settled.transaction) == (True, TX)

    monkeypatch.setattr(settle_module, "read_settle_answer", broken)
    with caplog.at_level(logging.INFO, logger="aimart.payments.settle"):
        settled = await client_over(LenientSettleClient, response, []).settle(PAYLOAD, REQUIREMENTS)
    assert (settled.success, settled.error_reason, settled.network) == (False, UNREADABLE, N)
    (line,) = caplog.records
    assert (line.levelno, line.facilitator, line.status) == (
        logging.ERROR,
        "https://f.test/base",
        200,
    )  # type: ignore[attr-defined]


async def test_a_status_outside_2xx_and_a_dead_socket_raise_as_the_sdks_client_does() -> None:
    for status in (199, 300, 404, 503):
        client = client_over(LenientSettleClient, httpx.Response(status, text="no " * 500), [])
        with pytest.raises(
            ValueError, match=rf"Facilitator settle failed \({status}\): no no"
        ) as e:
            await client.settle(PAYLOAD, REQUIREMENTS)
        assert len(str(e.value)) < 250

    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = LenientSettleClient(
        FacilitatorConfig(
            url="https://f.test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(dead))
        )
    )
    with pytest.raises(httpx.ConnectError):
        await client.settle(PAYLOAD, REQUIREMENTS)


# ------------------------------------------------------------------------------ the tripwire
def test_the_sdk_still_has_the_private_helpers_the_settle_client_stands_on() -> None:
    """Fails on an SDK upgrade that removes, renames or re-shapes them, or that changes how
    its own ``_settle_http`` builds the request and reads the answer: read the new
    ``_settle_http`` and bring ``LenientSettleClient`` in line before touching this test."""
    parameters = {
        HTTPFacilitatorClient._get_async_client: ["self"],
        HTTPFacilitatorClient._build_request_body: [
            "self",
            "version",
            "payload_dict",
            "requirements_dict",
        ],
        HTTPFacilitatorClient._get_settle_headers: ["self"],
        sdk_client._attach_extension_responses: ["result", "response"],
    }
    for helper, expected in parameters.items():
        assert list(inspect.signature(helper).parameters) == expected, helper.__qualname__
    assert not inspect.iscoroutinefunction(HTTPFacilitatorClient._get_async_client)
    assert isinstance(HTTPFacilitatorClient.url, property)
    source = inspect.getsource(HTTPFacilitatorClient._settle_http)
    for used in (
        "self._get_async_client()",
        "self._build_request_body(version, payload_dict, requirements_dict)",
        'f"{self._url}/settle"',
        "headers=self._get_settle_headers()",
        "json=request_body",
        "response.status_code != 200",
        '_parse_facilitator_response(response, SettleResponse, "settle")',
        "_attach_extension_responses(result, response)",
    ):
        assert used in source, used
    public = inspect.getsource(HTTPFacilitatorClient.settle)
    assert public.count("model_dump(by_alias=True, exclude_none=True)") == 2
    assert "payload.x402_version" in public
    # The only names the subclass defines: everything else is the SDK's, verify included.
    assert {n for n in vars(LenientSettleClient) if not n.startswith("__")} == {
        "settle",
        "_post_settle",
    }
