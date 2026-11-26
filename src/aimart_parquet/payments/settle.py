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
* Any other 2xx whose JSON is an object with ``"success": true`` is a settlement, unless
  something beside it says otherwise (an error or reason key, a ``status`` or ``state`` that
  is not a settled word, ``ok``/``settled``/``isValid`` not true); the rest is read leniently:
  what cannot be read is left out, never fatal.
* ``"success": false`` (or what the SDK reads as false) is a refusal, read as leniently.
* ``success`` absent, or a word the SDK reads as true ("true", 1): a settlement only if a
  transaction id that looks like one on the quoted network is named (0x and 64 hex digits,
  not all zeros, on eip155; a base58 signature on solana) and nothing says otherwise, a
  ``message`` included. ``success`` present as anything else (null, "failed", -1, an object)
  is unreadable: no facilitator settles by being vague.
* Anything else in a 2xx (not JSON, not an object, nothing that says "settled") is answered as
  a refusal and logged at ERROR, so the owner can reconcile it against the chain.
* A refusal that names a transaction stays a refusal (the transfer may be pending and still
  land: AGENTS.md, Known gaps) and is logged at WARNING however it was worded.

A timeout, a connection error and a non-2xx are raised, as the SDK raises them, but for one
kind of non-2xx: a JSON object that says ``"success": false`` is the refusal it states
(``_stated_refusal``). CDP answers every settle it rejects that way, as HTTP 400, with
``settlement_pending`` among the reasons: a transfer CDP broadcast and whose receipt it could
not read yet, with the transaction named. Raised, that answer lost the transaction and the SDK's
one retry of a pending settlement, and the caller was told "failed" while the transfer could
still land (review of 2026-11-26); read, the retry runs, and the receipt and a WARNING line name
the transaction. After a 2xx, or such a refusal, nothing here raises: every outcome is a
``SettleResponse``.
"""

from __future__ import annotations

import json
import logging
import re
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
# Free text that, without a boolean success, may be a refusal ("transfer reverted").
WORDING_KEYS = ("message", "msg", "detail", "details", "description")
# Keys (lowercased, without _ or -) whose value, when set, means something went wrong.
FAULT_WORDS = ("error", "reason", "fault", "failure", "revert")
# ``status`` / ``state`` values that do not contradict a settlement.
SETTLED_STATES = frozenset(
    {"settled", "success", "succeeded", "successful", "confirmed", "completed", "complete"}
)
STATE_KEYS = frozenset({"status", "state", "settlementstatus", "txstatus"})
# Flags that must be true, when present, for an answer to be a settlement.
FLAG_KEYS = frozenset({"ok", "settled", "isvalid", "valid", "confirmed", "issettled"})
EVM_TX = re.compile(r"0x[0-9a-fA-F]{64}")
SOLANA_TX = re.compile(r"[1-9A-HJ-NP-Za-km-z]{64,88}")

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


Stated = Literal["settled", "refused", "unsaid", "unreadable"]


def _stated_success(data: dict[str, Any]) -> Stated:
    """What ``success`` says: only a JSON true is "settled".

    "false", "no" and 0 are "refused": the SDK's model reads them so, and a transaction id
    beside one of them (a transfer that was broadcast and failed) must not turn a refusal
    into a settlement. "true", "yes" and 1 are "unsaid", like no ``success`` at all: outside
    the SDK's schema they count only with a transaction id. Anything else (null, "failed",
    -1, an object) is "unreadable": a facilitator that says something no reader can take for
    a boolean has not said it settled, and a transaction id beside it may be a reverted one.
    """
    if "success" not in data:
        return "unsaid"
    value = data["success"]
    if isinstance(value, bool):
        return "settled" if value else "refused"
    try:
        return "unsaid" if _BOOL.validate_python(value) else "refused"
    except ValidationError:
        return "unreadable"


def _key(key: str) -> str:
    return key.lower().replace("_", "").replace("-", "")


def _fault(data: dict[str, Any]) -> str | None:
    """The first error the answer names: the SDK's keys first, then any error or reason key.

    ``errorCode``, ``errors``, ``invalidReason``, ``reason``, ``failureReason``... all count;
    a key that is null, false, zero or empty does not. The error message keys are left to
    ``MESSAGE_KEYS``, as before.
    """
    named = _first(data, REASON_KEYS)
    if named:
        return named
    skip = {_key(k) for k in MESSAGE_KEYS}
    for key, value in data.items():
        norm = _key(str(key))
        if norm not in skip and any(word in norm for word in FAULT_WORDS):
            text = _text(value)
            if text:
                return text
    return None


def _contradicted(data: dict[str, Any]) -> bool:
    """Whether a ``status``/``state`` or an ``ok``/``settled``/``isValid`` flag says no.

    A state must be one of ``SETTLED_STATES`` and a flag exactly true; null is not stated.
    """
    for key, value in data.items():
        norm = _key(str(key))
        if value is None:
            continue
        if norm in STATE_KEYS and not (
            isinstance(value, str) and value.strip().lower() in SETTLED_STATES
        ):
            return True
        if norm in FLAG_KEYS and value is not True:
            return True
    return False


def _looks_like_a_transaction(transaction: str, network: str) -> bool:
    """A real transaction id on the quoted network, not "pending", "0x" or a zero hash."""
    if network.startswith("eip155:"):
        return bool(EVM_TX.fullmatch(transaction)) and bool(transaction[2:].strip("0"))
    if network.startswith("solana:"):
        return bool(SOLANA_TX.fullmatch(transaction)) and bool(transaction.strip("1"))
    return False


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
    stated = _stated_success(data)
    transaction = _transaction(data)
    reason = _fault(data)
    message = _first(data, MESSAGE_KEYS)
    success = stated != "refused"
    if stated == "unreadable":
        return _not_confirmed(network, transaction, reason), "unreadable"
    if stated == "settled" and (reason or message or _contradicted(data)):
        # "Settled" beside an error, a failed status or ok: false contradicts itself: no
        # delivery on it, and no receipt that says both.
        return _not_confirmed(network, transaction, reason), "unreadable"
    if stated == "unsaid":
        # Nothing here says "settled" or "refused". A facilitator must not be able to have
        # work delivered by answering vaguely ({} or {"status": "ok"} with a 200), and the
        # payer must not be charged for nothing because a facilitator spells success another
        # way. The transaction id decides: a facilitator names one once it has submitted the
        # transfer, and that is something the owner and the payer can check on chain. So:
        # settled only with a real transaction id on the quoted network and nothing beside it
        # that says otherwise, not even a message ("transfer reverted"); in every other case
        # not settled, which withholds the result and logs the answer for reconciling.
        wording = message or _first(data, WORDING_KEYS)
        if (
            reason
            or wording
            or _contradicted(data)
            or not _looks_like_a_transaction(transaction, network)
        ):
            return _not_confirmed(network, transaction, reason), "unreadable"
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


def _stated_refusal(response: httpx.Response, network: str) -> SettleResponse | None:
    """A non-2xx answer whose JSON object says ``"success": false``, read as that; else None.

    CDP's ``X402SettlePaymentRejection`` (cdp-sdk ``openapi.yaml``: POST /v2/x402/settle, '400'):
    ``{"success": false, "errorReason", "errorMessage", "payer", "transaction", "network"}``,
    whose ``transaction`` is set when ``errorReason`` is ``settlement_pending``. Coinbase's
    TypeScript client reads such a body as the refusal it is (``SettleError``) and its server
    retries a pending one; the SDK's Python client raises on it. Only a JSON ``false`` counts:
    anything else in a non-2xx stays an error, never a settlement.

    A 429 or a 5xx that names no transaction is raised all the same: the facilitator's own
    trouble, with nothing to keep, and raised it rests the facilitator (``payments._Answered``).
    What reaches the payer is the documented fields only, each capped: the receipt is built from
    a third party's body (a 20 KB ``extra`` once made a 70 KB PAYMENT-RESPONSE header).
    """
    try:
        data = response.json()
    except ValueError:  # not JSON, or not text
        return None
    if not isinstance(data, dict) or data.get("success") is not False:
        return None
    refused, _ = read_settle_answer(response.status_code, data, network)
    if refused.success:
        return None
    status = response.status_code
    if not refused.transaction and (status == 429 or status >= 500):
        return None
    return SettleResponse(
        success=False,
        error_reason=refused.error_reason,  # capped by the reading, as the message is
        error_message=refused.error_message,
        payer=(refused.payer or "")[:TEXT_LIMIT] or None,
        transaction=refused.transaction[:TEXT_LIMIT],
        network=network,  # the quoted one: the refusal is of this payment
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
        network = str(getattr(requirements, "network", ""))
        if not 200 <= status < 300:
            refused = _stated_refusal(response, network)
            if refused is None:
                # As the SDK: an exception is "not settled" to both paywalls. Its message
                # reaches the caller as the reason, so it carries a short excerpt, not a whole
                # error page.
                log.warning(
                    "facilitator refused to settle",
                    extra={"facilitator": self.url, "status": status},
                )
                excerpt = " ".join(response.text.split())[:TEXT_LIMIT]
                msg = f"Facilitator settle failed ({status}): {excerpt}"
                raise ValueError(msg)
            # The refusal it states (CDP's every rejection): a returned refusal, so the SDK
            # retries a pending one there, and the receipt keeps the transaction. Its reason
            # and transaction are logged, never the body.
            log.warning(
                "facilitator refused to settle and named a transaction: check it on chain"
                if refused.transaction
                else "facilitator refused to settle",
                extra={
                    "facilitator": self.url,
                    "status": status,
                    "reason": (refused.error_reason or "")[:TEXT_LIMIT],
                    "transaction": refused.transaction[:TEXT_LIMIT],
                },
            )
            try:
                return _attach_extension_responses(refused, response)
            except Exception:  # noqa: BLE001 - a side channel for hooks; the refusal stands
                return refused
        # From here on the transfer may have been made: nothing below may raise.
        try:
            settled, reading = read_settle_answer(status, response.json(), network)
        except Exception:  # noqa: BLE001 - a body that is not JSON, or a fault in the reading
            settled, reading = _not_confirmed(network), "unreadable"
        if settled.success and (settled.error_reason or settled.error_message):
            # Only the SDK's schema gets here ("success": true beside an errorReason): read as
            # the SDK reads it, a settlement, but flagged, and the receipt does not say both.
            log.warning(
                "facilitator settled and named an error: the error is left out of the receipt",
                extra={
                    "facilitator": self.url,
                    "status": status,
                    "transaction": settled.transaction[:TEXT_LIMIT],
                    "reason": (settled.error_reason or "")[:TEXT_LIMIT],
                },
            )
            settled = settled.model_copy(update={"error_reason": None, "error_message": None})
        # One line per answer the SDK would have refused: the owner reconciles from these.
        # Never the body (a third party's content) and never the payload. A refusal that
        # names a transaction gets its line in any wording, the SDK's included: a transfer
        # was submitted and may still land (pending), and this line is all that records it.
        refused_with_a_transaction = not settled.success and bool(settled.transaction)
        if reading != "schema" or refused_with_a_transaction:
            log.log(
                logging.ERROR if reading == "unreadable" else logging.WARNING,
                "facilitator's settle answer could not be read: treated as not settled"
                if reading == "unreadable"
                else "facilitator refused to settle and named a transaction: check it on chain"
                if refused_with_a_transaction
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
