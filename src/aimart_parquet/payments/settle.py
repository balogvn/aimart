"""Reading a facilitator's answer to POST /settle.

By the time a facilitator answers, the payer's money may have moved. The SDK's HTTP client
treats every answer but an HTTP 200 its strict ``SettleResponse`` model accepts as an error,
and an error at this point means "not settled": the caller gets a 402 (REST) or ``isError``
(MCP) and the output is deleted. So a facilitator that settled and then answered with the
amount as a JSON number, without ``network``, with the hash under ``txHash``, or with a 201,
left the payer charged for nothing (reproduced in review on 2026-11-16; xpay's answers are
schema-valid, Primer's have never been seen).

The rules, in ``read_settle_answer``:

* An HTTP 200 the SDK's model accepts is read by that model, exactly as before.
* Any other 2xx whose JSON is an object with ``"success": true`` is a settlement, and the rest
  is read leniently: what cannot be read is left out, never fatal.
* ``"success": false`` is a refusal, read as leniently.
* No boolean ``success``: a settlement only if a transaction id is named and no error is.
* Anything else in a 2xx (not JSON, not an object, nothing that says "settled") is answered as
  a refusal and logged at ERROR, so the owner can reconcile it against the chain.

A non-2xx, a timeout and a connection error are raised, as the SDK raises them. After a 2xx
nothing here raises: every outcome is a ``SettleResponse``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Literal

from pydantic import TypeAdapter, ValidationError
from x402 import SettleResponse
from x402.http import HTTPFacilitatorClient

# Private to the SDK, like the three methods ``LenientSettleClient`` calls:
# ``unit/test_fix_settle_reading.py`` fails when an upgrade renames any of them.
from x402.http.facilitator_client import _attach_extension_responses

if TYPE_CHECKING:
    import httpx
    from x402.schemas import PaymentPayload, PaymentRequirements
    from x402.schemas.v1 import PaymentPayloadV1, PaymentRequirementsV1

log = logging.getLogger("aimart.payments.settle")

Reading = Literal["schema", "lenient", "unreadable"]

TRANSACTION_KEYS = ("transaction", "txHash", "transactionHash")
REASON_KEYS = ("errorReason", "error_reason", "error")
MESSAGE_KEYS = ("errorMessage", "error_message")

UNREADABLE = "facilitator_answer_unreadable"
UNREADABLE_MESSAGE = (
    "The facilitator answered the settlement request without saying that the payment settled, "
    "so the result is withheld. The transfer may have been made all the same: look up the "
    "transaction named here, or your wallet's transfers to payTo, before paying again."
)
# What a caller, a response header or a log line gets of a facilitator's free text.
TEXT_LIMIT = 200

_BOOL = TypeAdapter(bool)


def _text(value: Any) -> str | None:
    """A JSON value as one line of text; None for null, false, zero and anything empty."""
    if not value:
        return None
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    return " ".join(text.split())[:TEXT_LIMIT] or None


def _string(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _first(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    return next((text for key in keys if (text := _text(data.get(key)))), None)


def _transaction(data: dict[str, Any]) -> str:
    return next((tx for key in TRANSACTION_KEYS if (tx := _string(data.get(key)))), "")


def _amount(value: Any) -> str | None:
    """Any JSON scalar as its string; 20000.0 (and 2e4) as "20000"."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _stated_success(value: Any) -> bool | None:
    """True or False when the answer says so with a boolean; None when it does not say.

    "false", "no" and 0 count as False: the SDK's model reads them so, and a transaction id
    beside one of them (a transfer that was broadcast and failed) must not turn a refusal
    into a settlement. "true", "yes" and 1 are not a boolean true: outside the SDK's own
    schema they only count with a transaction id, like an answer with no ``success`` at all.
    """
    if isinstance(value, bool):
        return value
    try:
        return False if _BOOL.validate_python(value) is False else None
    except ValidationError:
        return None


def _not_confirmed(
    network: str, transaction: str = "", reason: str | None = None
) -> SettleResponse:
    return SettleResponse(
        success=False,
        error_reason=reason or UNREADABLE,
        error_message=UNREADABLE_MESSAGE,
        transaction=transaction,
        network=network,
    )


def read_settle_answer(status: int, data: Any, network: str) -> tuple[SettleResponse, Reading]:
    """What a 2xx answer to /settle says, and how it had to be read.

    ``data`` is the decoded JSON body; ``network`` the requirement's, for an answer that names
    none. Never raises for JSON-decoded ``data``.
    """
    if status == 200:
        try:
            return SettleResponse.model_validate(data), "schema"
        except (ValidationError, ValueError, TypeError):
            pass  # where the SDK stops ("invalid data"): the money may have moved all the same
    if not isinstance(data, dict):
        return _not_confirmed(network), "unreadable"
    success = _stated_success(data.get("success"))
    transaction = _transaction(data)
    reason = _first(data, REASON_KEYS)
    message = _first(data, MESSAGE_KEYS)
    if success is None:
        # Nothing here says "settled" or "refused". A facilitator must not be able to have
        # work delivered by answering vaguely ({} or {"status": "ok"} with a 200), and the
        # payer must not be charged for nothing because a facilitator spells success another
        # way. The transaction id decides: a facilitator names one once it has submitted the
        # transfer, and that is something the owner and the payer can check on chain. So:
        # settled only with a transaction id and no error beside it; in every other case not
        # settled, which withholds the result and logs the answer's status for reconciling.
        if not transaction or reason or message:
            return _not_confirmed(network, transaction, reason), "unreadable"
        success = True
    extensions, extra = data.get("extensions"), data.get("extra")
    return (
        SettleResponse(
            success=success,
            error_reason=reason,
            error_message=message,
            payer=_string(data.get("payer")),
            transaction=transaction,
            network=_string(data.get("network")) or network,
            amount=_amount(data.get("amount")),
            extensions=extensions if isinstance(extensions, dict) else None,
            extra=extra if isinstance(extra, dict) else None,
        ),
        "lenient",
    )


class LenientSettleClient(HTTPFacilitatorClient):
    """The SDK's HTTP facilitator client, with this service's reading of the settle answer.

    /supported and verify are the SDK's, untouched. All the coupling to the SDK's private
    helpers is in this class: ``_get_async_client``, ``_build_request_body`` and
    ``_get_settle_headers`` (the request, built as ``HTTPFacilitatorClient._settle_http``
    builds it) and ``_attach_extension_responses`` (the EXTENSION-RESPONSES header, which the
    SDK attaches to the answer for its hooks and never serializes to the payer).
    """

    async def settle(
        self,
        payload: PaymentPayload | PaymentPayloadV1,
        requirements: PaymentRequirements | PaymentRequirementsV1,
    ) -> SettleResponse:
        response = await self._post_settle(payload, requirements)
        status = response.status_code
        if not 200 <= status < 300:
            # As the SDK: an exception is "not settled" to both paywalls. Its message reaches
            # the caller as the reason, so it carries a short excerpt, not a whole error page.
            log.warning(
                "facilitator refused to settle",
                extra={"facilitator": self.url, "status": status},
            )
            excerpt = " ".join(response.text.split())[:TEXT_LIMIT]
            msg = f"Facilitator settle failed ({status}): {excerpt}"
            raise ValueError(msg)
        # From here on the transfer may have been made: nothing below may raise.
        network = str(getattr(requirements, "network", ""))
        try:
            settled, reading = read_settle_answer(status, response.json(), network)
        except Exception:  # noqa: BLE001 - a body that is not JSON, or a fault in the reading
            settled, reading = _not_confirmed(network), "unreadable"
        # One line per answer the SDK would have refused: the owner reconciles from these.
        # Never the body (a third party's content) and never the payload.
        if reading != "schema":
            log.log(
                logging.ERROR if reading == "unreadable" else logging.WARNING,
                "facilitator's settle answer could not be read: treated as not settled"
                if reading == "unreadable"
                else "facilitator's settle answer read leniently",
                extra={
                    "facilitator": self.url,
                    "status": status,
                    "transaction": settled.transaction[:TEXT_LIMIT],
                    "settled": settled.success,
                },
            )
        try:
            return _attach_extension_responses(settled, response)
        except Exception:  # noqa: BLE001 - a side channel for hooks; the settlement stands
            return settled

    async def _post_settle(
        self,
        payload: PaymentPayload | PaymentPayloadV1,
        requirements: PaymentRequirements | PaymentRequirementsV1,
    ) -> httpx.Response:
        body = self._build_request_body(
            payload.x402_version,
            payload.model_dump(by_alias=True, exclude_none=True),
            requirements.model_dump(by_alias=True, exclude_none=True),
        )
        return await self._get_async_client().post(
            f"{self.url}/settle", headers=self._get_settle_headers(), json=body
        )
