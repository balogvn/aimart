"""x402 payments: one resource server shared by the REST paywall and the MCP paywall.

Money flow per paid call: the caller signs a USDC transfer authorization for the quoted price
to our payTo address; we ask the facilitator to *verify* it before doing any work; we run the
operation; only if it succeeded do we ask the facilitator to *settle* (move the funds). A
failed operation is never settled, so a caller is never charged for an error.

Two rules on either side of that: a request that cannot succeed (an invalid body or tool
arguments, a tool this deployment does not serve) is refused before any price is quoted, and
an output whose result is withheld (settlement failed, or the call failed after storing it)
is deleted, because nobody paid for it and it would otherwise evict paying customers' files.

The server holds no private key. A facilitator submits the transfer and pays the gas.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

import anyio
import anyio.to_thread
from fastapi.responses import JSONResponse
from x402 import x402ResourceServer
from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
from x402.http import FacilitatorConfig
from x402.http.middleware.fastapi import payment_middleware
from x402.http.types import PaymentOption, RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.mechanisms.svm import SOLANA_DEVNET_CAIP2, SOLANA_MAINNET_CAIP2
from x402.mechanisms.svm.exact import ExactSvmServerScheme
from x402.schemas import ResourceInfo
from x402.schemas.config import ResourceConfig
from x402.server_base import is_retryable_settlement_pending

from aimart_parquet.artifacts import discard_withheld, outputs_of
from aimart_parquet.config import PAYMENT_WINDOW_S, SETTLE_MARGIN_S, NetworkMode, Settings
from aimart_parquet.errors import AimartError
from aimart_parquet.payments import cdp
from aimart_parquet.payments.bazaar import EXAMPLES, example_output
from aimart_parquet.payments.mcp_gate import (
    PaymentsUnavailable,
    X402McpGate,
    payments_unavailable,
)
from aimart_parquet.payments.pricing import Operation, active_operations, usd_to_atomic
from aimart_parquet.payments.settle import LenientSettleClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Request, Response
    from x402 import FacilitatorClient, SettleResponse, SupportedResponse, VerifyResponse
    from x402.schemas import PaymentPayload, PaymentRequirements

    from aimart_parquet.artifacts import ArtifactStore

log = logging.getLogger("aimart.payments")

BASE_MAINNET = "eip155:8453"
BASE_SEPOLIA = "eip155:84532"

NETWORKS: dict[NetworkMode, dict[str, str]] = {
    NetworkMode.TESTNET: {"evm": BASE_SEPOLIA, "svm": SOLANA_DEVNET_CAIP2},
    NetworkMode.MAINNET: {"evm": BASE_MAINNET, "svm": SOLANA_MAINNET_CAIP2},
}

# Free, keyless facilitators. Mainnet order matters: the first facilitator that answered GET
# /supported and lists a (network, scheme) serves it. xpay, then Primer, settle USDC on Base
# with standard EIP-3009 transfers (the payer signs one authorization: no approval, no ETH);
# neither lists Solana, so Dexter, last, settles it. Dexter also lists Base, but as Permit2,
# which is not what a 402 here asks a payer to sign: that kind is ignored (``_Answered``), so
# with both others down Base is refused (503, unpaid), not handed to Dexter. Heurist (first
# until 2026-11-15) answered /supported with a 502 at every probe from 11-12 to 11-15. See
# docs/adr/0003, the amendments.
DEFAULT_FACILITATORS: dict[NetworkMode, tuple[str, ...]] = {
    NetworkMode.TESTNET: ("https://x402.org/facilitator",),
    NetworkMode.MAINNET: (
        "https://facilitator.xpay.sh",
        "https://x402.primer.systems",
        "https://x402.dexter.cash",
    ),
}

# One facilitator's GET /supported. The SDK's HTTP client waits 90 s (one timeout for
# /supported, verify and settle): a host that accepts the connection and never answers would
# hold the first paid request of every cold start that long, and two of them past the 150 s
# a request may last on Modal, which is a dropped connection, not the typed 503.
SUPPORTED_TIMEOUT_S = 10.0
# How long a facilitator that failed /supported, or an accepted network nobody offered, is
# left alone before one request asks every facilitator again (see ``ensure_initialized``).
RETRY_SKIPPED_S = 300.0
# How many of those rounds in a row a facilitator that answered before may fail /supported and
# keep its place, with its last answer: its verify and settle can work while /supported does
# not, and one 502 must not move a network to the next in the list. After them (15 minutes)
# it is dropped until it answers again, so a host that died is not quoted until a restart.
KEPT_ROUNDS = 3
# How long a facilitator whose verify or settle failed (a non-200, a timeout: not an answer that
# the payment is invalid or refused) is passed over for verify while another facilitator offers
# the network: CDP failing verify mid-run used to make every Base call a 402 while xpay stood
# behind it, and a timeout would otherwise cost each call ``facilitator_timeout_s`` before the
# next. A failed settle rests it too (review of 2026-11-26): a CDP whose verify worked and whose
# settle failed got every later Base call, each run and then refused at settle.
VERIFY_REST_S = 60.0
# The most payments remembered between verify and settle (``_Failover``): far above what the
# job queue lets run at once. One pushed out settles with the facilitator first in line, which
# did not verify it: that settle fails, and the call is unpaid, never charged twice.
SETTLERS_KEPT = 1024

# Where a paying client puts its signed payload (v2, then v1): what the SDK middleware reads.
PAYMENT_HEADERS = ("payment-signature", "x-payment")

SERVICE_NAME = "AImart Parquet"  # <= 32 ASCII chars, or facilitator catalogs drop it
TAGS = ["data", "parquet", "etl", "csv", "mcp"]  # <= 5 tags


def carries_payment(request: Request) -> bool:
    return any(request.headers.get(name) for name in PAYMENT_HEADERS)


def facilitator_urls(settings: Settings) -> list[str]:
    """The facilitators, in order. CDP first when its key is configured on mainnet (cdp.py).

    An explicit ``AIMART_FACILITATOR_URLS`` is the whole list, as it always was: CDP is in it
    only where the operator put it, and gets the key there.
    """
    if settings.facilitator_urls:
        return [u.strip() for u in settings.facilitator_urls.split(",") if u.strip()]
    defaults = list(DEFAULT_FACILITATORS[settings.network_mode])
    if settings.network_mode is NetworkMode.MAINNET and cdp.credentials(settings) is not None:
        return [cdp.CDP_FACILITATOR_URL, *defaults]
    return defaults


class ClientDisconnected(AimartError):
    """The caller hung up before its response was ready: nobody receives it, so it is unpaid.

    499 is nginx's "client closed request"; no standard status says it, and nobody reads it
    but the access log.
    """

    code = "client_disconnected"
    http_status = 499


def _same_units(stated: str, quoted: str) -> bool:
    """Whether a facilitator's amount is the quoted number of atomic units ("020000" is)."""
    # No int(): Python refuses a digit string over 4300 characters, and this runs after the
    # money has moved, where nothing may raise.
    whole = stated.isascii() and stated.isdigit() and quoted.isascii() and quoted.isdigit()
    return stated.lstrip("0") == quoted.lstrip("0") if whole else stated == quoted


def _refused(client: Any) -> bool:
    """Whether a facilitator refused this service's key (CDP: a 401 or 403, ``cdp.py``)."""
    return bool(getattr(client, "refused", False))


def _payment_key(payload: Any) -> str:
    """What identifies one payment between its verify and its settle: its signed part."""
    inner = getattr(payload, "payload", None)
    try:
        return json.dumps(inner, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(inner)


class _Failover:
    """Per paid call: verify may fall through to the next facilitator; settle never does.

    The SDK gives a network to one facilitator for the life of an initialization. When that
    one's verify fails (raises: a non-200, a timeout, a refused key), the next facilitator that
    offers the network is asked instead, once. Verify moves no money, so asking another is
    safe; the facilitator that verified a payment is remembered and settles it, so a payment is
    never settled by one that did not verify it, and a settle that fails is never repeated
    elsewhere (the call is then unpaid, as always). A facilitator whose verify or settle failed
    is passed over for ``VERIFY_REST_S`` (a refused key: for the life of the process) while
    another can take the network.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._resting: dict[int, float] = {}  # id(client) -> when it may verify again
        self._settlers: dict[str, Any] = {}  # payment -> the client that verified it

    def usable(self, client: Any) -> bool:
        return not _refused(client) and self._clock() >= self._resting.get(id(client), 0.0)

    def rest(self, client: Any) -> None:
        self._resting[id(client)] = self._clock() + VERIFY_REST_S

    def remember(self, payload: Any, client: Any) -> None:
        self._settlers[_payment_key(payload)] = client
        while len(self._settlers) > SETTLERS_KEPT:
            del self._settlers[next(iter(self._settlers))]

    def settler(self, payload: Any) -> Any | None:
        return self._settlers.get(_payment_key(payload))

    def forget(self, payload: Any) -> None:
        self._settlers.pop(_payment_key(payload), None)


class _Answered:
    """A facilitator, with the answer its GET /supported gave this initialization.

    The SDK's ``initialize()`` asks each client itself, in order, and stops at the first that
    raises: on 2026-11-12 one 502 from the first mainnet default made every paid call a 503
    while the second answered. The SDK is handed these instead: only the facilitators that
    answered, each repeating its answer. Verify and settle go to the client untouched.
    """

    def __init__(
        self,
        client: FacilitatorClient,
        supported: SupportedResponse,
        methods: dict[str, str],
        failover: _Failover | None = None,
    ) -> None:
        """``methods``: per accepted network, the assetTransferMethod a 402 on it names.

        An 'exact' kind that names another one is left out, so this facilitator counts as not
        offering that network: Dexter lists Base as Permit2 while the SDK quotes USDC on Base
        as EIP-3009, and it was never shown to verify or settle what such a quote makes a
        payer sign. A kind that names no method means the scheme's default, as in a quote.
        """
        self.client = client
        self.url = str(getattr(client, "url", type(client).__name__))
        kinds = []
        for kind in supported.kinds:
            stated = (kind.extra or {}).get("assetTransferMethod")
            quoted = methods.get(kind.network)
            if kind.scheme == "exact" and quoted and stated and stated != quoted:
                log.info(
                    "facilitator's kind ignored: it settles with another transfer method",
                    extra={"facilitator": self.url, "network": kind.network, "method": stated},
                )
                continue
            kinds.append(kind)
        self.supported = supported.model_copy(update={"kinds": kinds})
        self.failover = failover or _Failover(time.monotonic)
        self.behind: list[_Answered] = []  # the facilitators after it, in order (``Payments``)

    def get_supported(self) -> SupportedResponse:
        return self.supported

    async def verify(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> VerifyResponse:
        network = getattr(requirements, "network", None)
        after = next((a for a in self.behind if network and a.offers(network)), None)
        if after is None or self.failover.usable(self.client):
            try:
                return await self.client.verify(payload, requirements)
            except Exception as exc:
                if after is None:
                    raise
                if not _refused(self.client):
                    self.failover.rest(self.client)
                # The type only: the SDK puts the facilitator's body in the message.
                log.warning(
                    "facilitator verify failed: the next facilitator verifies and settles",
                    extra={
                        "facilitator": self.url,
                        "next": after.url,
                        "network": network,
                        "error": type(exc).__name__,
                    },
                )
        verified = await after.client.verify(payload, requirements)
        self.failover.remember(payload, after.client)
        return verified

    def _rest_after_failed_settle(
        self, client: Any, requirements: PaymentRequirements, exc: Exception
    ) -> None:
        """A settle that raised rests the facilitator first in line, as a verify that raised does.

        This call stays unpaid: settle never falls through. The calls after it are verified and
        settled by the next facilitator offering the network, for ``VERIFY_REST_S``, then this
        one is asked again. Without it, a CDP whose verify works and whose settle fails (its
        documented 402 ``payment_method_required`` once the free settlements are used up, a
        settle-only 429 or 5xx, a timeout) got every later Base call, each verified, run and
        then refused at settle, while xpay stood behind it (review of 2026-11-26). A refusal
        the facilitator states (``"success": false``) does not rest it: that is its answer about
        this payment, often the payer's doing, and one naming a transaction may still land.
        """
        if client is not self.client:  # the one behind, settling what it verified
            return
        network = getattr(requirements, "network", None)
        after = next((a for a in self.behind if network and a.offers(network)), None)
        if after is None:
            return
        if not _refused(client):
            self.failover.rest(client)
        # The type only: the SDK and settle.py put the facilitator's body in the message.
        log.warning(
            "facilitator settle failed: the next facilitator verifies and settles the calls "
            "after this one",
            extra={
                "facilitator": self.url,
                "next": after.url,
                "network": network,
                "error": type(exc).__name__,
            },
        )

    async def settle(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> SettleResponse:
        # Whoever verified it, never another: a settle is not repeated elsewhere.
        client = self.failover.settler(payload) or self.client
        url = str(getattr(client, "url", type(client).__name__))
        try:
            settled = await client.settle(payload, requirements)
        except Exception as exc:
            self.failover.forget(payload)
            self._rest_after_failed_settle(client, requirements, exc)
            raise
        if not is_retryable_settlement_pending(settled):
            # Kept for the SDK's one retry of a pending settlement, which must go there too.
            self.failover.forget(payload)
        # x402 v2 makes the amount optional and xpay leaves it out: the first receipts on
        # mainnet named a transaction and no amount, and the example client crashed on them.
        # Under 'exact' the payer signed a transfer of exactly the quoted amount, so that is
        # what a settlement that succeeded moved, and what the receipt (PAYMENT-RESPONSE on
        # REST, _meta on MCP) states: also when the facilitator states "19999", "2e4" or ""
        # (a stated amount was once passed on as it came, a receipt misstating what was
        # paid). The money has moved by now: nothing here may raise (getattr: a v1
        # requirement has no ``amount``, and then the receipt stays as it came).
        quoted = getattr(requirements, "amount", None)
        if settled.success and quoted and getattr(requirements, "scheme", None) == "exact":
            quoted = str(quoted)
            stated = "" if settled.amount is None else str(settled.amount).strip()
            if stated and not _same_units(stated, quoted):
                # For the owner: a facilitator that disagrees about the amount is one to ask.
                log.warning(
                    "facilitator's receipt states another amount than was quoted: "
                    "the receipt names the quoted amount",
                    extra={
                        "facilitator": url,
                        "stated_amount": stated[:80],
                        "quoted_amount": quoted,
                    },
                )
            if settled.amount != quoted:
                settled = settled.model_copy(update={"amount": quoted})
        return settled

    def offers(self, network: str) -> bool | None:
        """Whether it lists x402 v2 'exact' on ``network``; None when it lists no 'exact' there."""
        versions = {
            kind.x402_version
            for kind in self.supported.kinds
            if kind.network == network and kind.scheme == "exact"
        }
        return (2 in versions) if versions else None

    def price_floor(self, network: str) -> int | None:
        """The least it settles on ``network``, in atomic units; None when it names none."""
        for kind in self.supported.kinds:
            if (kind.x402_version, kind.network, kind.scheme) != (2, network, "exact"):
                continue
            floor = (kind.extra or {}).get("minPaymentAmountAtomic")
            if floor is None or floor == "":
                return None
            try:
                return int(str(floor))
            except ValueError:
                # "0.78", 1e3, true: int() raised here, after the new SDK server was swapped
                # in and before it counted as initialized, so every paid call was a 503.
                # Not the value: logs hold no third party's content.
                log.warning(
                    "facilitator's price floor ignored: not a whole number of atomic units",
                    extra={"facilitator": self.url, "network": network},
                )
                return None
        return None


def _quoted_method(scheme: Any, network: str) -> str:
    """The assetTransferMethod a quote on ``network`` names: the SDK's asset table decides."""
    extra = scheme.parse_price("$1", network).extra or {}
    return str(extra.get("assetTransferMethod") or scheme.default_asset_transfer_method)


class Payments:
    def __init__(self, settings: Settings, facilitators: list[FacilitatorClient]) -> None:
        self.settings = settings
        self._facilitators = facilitators
        nets = NETWORKS[settings.network_mode]
        self.accepted: list[tuple[str, str]] = []
        self._schemes: dict[str, Any] = {}  # per accepted network, what builds its quotes
        if settings.pay_to_evm:
            self.accepted.append((nets["evm"], settings.pay_to_evm))
            # The SDK's own Protocol annotations disagree with its scheme classes (payment_flows).
            self._schemes[nets["evm"]] = ExactEvmServerScheme()  # type: ignore[no-untyped-call]
        if settings.pay_to_svm:
            self.accepted.append((nets["svm"], settings.pay_to_svm))
            self._schemes[nets["svm"]] = ExactSvmServerScheme()
        self._methods = {n: _quoted_method(scheme, n) for n, scheme in self._schemes.items()}
        # Each facilitator's last answer to GET /supported, by its place in the list, and how
        # many rounds in a row it has failed since.
        self._answers: dict[int, SupportedResponse] = {}
        self._misses: dict[int, int] = {}
        # Replaced by each initialization that serves something: until the first, it exists
        # so the routes can be checked when the app is built.
        self.server = self._new_server(facilitators)
        # The accepted networks a facilitator can verify and settle: what a 402 quotes.
        self.served: list[tuple[str, str]] = []
        if not self.accepted:
            # Paid routes with nobody to pay would either crash or, worse, serve work for free.
            msg = (
                "payments are enabled but no payee is configured: set AIMART_PAY_TO_EVM and/or "
                "AIMART_PAY_TO_SVM (or AIMART_PAYMENTS_ENABLED=false for a free deployment)"
            )
            raise ValueError(msg)
        self._check_payment_window()
        self._initialized = False
        self._init_lock = anyio.Lock()
        self._failed_rounds = 0  # so a request that waited behind one does not repeat it
        self._retry_at: float | None = None  # when to ask again; None when nothing was skipped
        self._clock = time.monotonic
        self._failover = _Failover(self._now)  # tests move _clock: read it per call
        self._mcp_requirements: dict[str, list[PaymentRequirements]] = {}
        # Only what this deployment serves: the quantum tools (off by default) used to be
        # quoted at $0.50/$1.00 and verified, and only then answered "Unknown tool".
        self._by_tool: dict[str, Operation] = {}
        for op in active_operations(settings):
            self._by_tool.setdefault(op.tool, op)

    def _check_payment_window(self) -> None:
        """Refuse to start when a paid call could outlast its own payment authorization.

        ``queue_wait_s`` was never read, and a call queued behind 4 x 180 s of jobs ran its work
        after the authorization expired: unsettleable, so served for free. ``fetch_timeout_s``
        counts once because it bounds all of a call's downloads together
        (``sources.downloads_deadline``, opened by ``Service._job``): a query over `data` and 7
        URL tables used to get one each, 480 s at the defaults. Unset, the queue wait is what
        the window leaves each operation (``Settings.queue_wait_for``), so only the job
        timeouts and the download allowance can fail this; set, it must fit the longest job.
        """
        s = self.settings
        longest = max(
            s.convert_timeout_s,
            s.query_timeout_s + 5,  # Service.query's outer deadline
            s.quantum_timeout_s if s.enable_quantum else 0.0,
        )
        needed = s.queue_wait_for(longest) + s.fetch_timeout_s + longest
        if needed + SETTLE_MARGIN_S > PAYMENT_WINDOW_S:
            msg = (
                f"queue_wait_s + fetch_timeout_s + the longest operation timeout is {needed:g} s; "
                f"with {SETTLE_MARGIN_S} s to verify and settle it exceeds the {PAYMENT_WINDOW_S} s "
                "a payment authorization is valid for, so paid calls could not settle. Lower "
                "AIMART_QUEUE_WAIT_S, AIMART_FETCH_TIMEOUT_S or the operation timeouts."
            )
            raise ValueError(msg)

    @classmethod
    def from_settings(cls, settings: Settings) -> Payments:
        # Not the SDK's client as it is: that one reads a settle answer it cannot parse, or
        # a 201, as "not settled" after the money has moved (payments/settle.py).
        clients: list[FacilitatorClient] = []
        if cdp.half_configured(settings):
            log.error("CDP facilitator not used: set both %s, or neither", cdp.SETTINGS_NAMES)
        for url in facilitator_urls(settings):
            if url.rstrip("/") == cdp.CDP_FACILITATOR_URL:
                # Its subclass: the same reading, plus the key. A key that cannot be read
                # leaves CDP out (one error line), as a facilitator that fails /supported is.
                client = cdp.cdp_client(settings)
                if client is not None:
                    clients.append(client)
                continue
            clients.append(
                LenientSettleClient(
                    FacilitatorConfig(url=url, timeout=settings.facilitator_timeout_s)
                )
            )
        return cls(settings, clients)

    def _new_server(self, facilitators: list[Any]) -> x402ResourceServer:
        server = x402ResourceServer(facilitators)
        for network, scheme in self._schemes.items():
            server.register(network, scheme)
        return server

    # ------------------------------------------------------------------ lifecycle
    async def ensure_initialized(self, *, paying: bool = False) -> None:
        """Fetch facilitator capabilities lazily, and again when some were missing.

        Not at boot: a scale-to-zero host cold-starts on the first request, and a facilitator
        blip at that moment must cost one failed (unpaid) request, not a crash-looping server.

        A facilitator whose GET /supported fails is skipped, and the others serve in the
        configured order. While one is skipped, or an accepted network has nobody to settle it,
        one request every ``RETRY_SKIPPED_S`` asks them all again, so a preferred facilitator
        that comes back serves again without a restart and a dead host is not called on every
        request. A request that carries a payment (``paying``) never starts that round: its
        authorization is already running out, and a quote always comes before it.

        In such a round a facilitator that answered before and fails now keeps its place with
        its last answer (``KEPT_ROUNDS`` rounds in a row, then it is dropped until it answers):
        a network moves only on an answer, never on one failed GET.

        Raises ``PaymentsUnavailable`` (typed, unpaid) while no accepted network can be served;
        the next request then asks again.
        """
        if not self._due(paying):
            return
        seen = self._failed_rounds
        async with self._init_lock:
            if not self._due(paying):  # double-checked: another task may have finished meanwhile
                return
            if self._failed_rounds != seen:
                # This request waited behind a round that has just failed: every waiter used
                # to repeat it, one after the other, against the hosts that had just failed.
                raise payments_unavailable()
            try:
                await self._initialize()
            except Exception:
                self._failed_rounds += 1
                raise

    def _now(self) -> float:
        return self._clock()

    def _due(self, paying: bool) -> bool:
        if not self._initialized:
            return True
        if paying:
            return False
        if any(_refused(self._facilitators[i]) for i in self._answers):
            # A key refused since the last round (CDP, mid-call): the next quote is built
            # without it. Its paid calls fall through to the next facilitator meanwhile.
            return True
        return self._retry_at is not None and self._clock() >= self._retry_at

    async def _initialize(self) -> None:
        if self._initialized:
            # Whatever happens below, this round is not repeated by the next request.
            self._retry_at = self._clock() + RETRY_SKIPPED_S
        fresh: dict[int, SupportedResponse] = {}

        async def ask(index: int, client: FacilitatorClient) -> None:
            answer = await self._supported(client)
            if answer is not None:
                fresh[index] = answer

        # A facilitator that refused this service's key is not asked again: the key is only
        # changed by a redeploy, which starts a new process.
        asked = [i for i, client in enumerate(self._facilitators) if not _refused(client)]
        async with anyio.create_task_group() as tg:
            for index in asked:
                tg.start_soon(ask, index, self._facilitators[index])
        for index, client in enumerate(self._facilitators):
            if index in fresh and not _refused(client):
                self._answers[index], self._misses[index] = fresh[index], 0
            elif index in self._answers and _refused(client):
                # Not KEPT_ROUNDS: a refused key does not come back by itself.
                del self._answers[index]
                log.warning(
                    "facilitator dropped: it refused this service's key",
                    extra={"facilitator": getattr(client, "url", type(client).__name__)},
                )
            elif index in self._answers:
                # One failed GET moved Base from the facilitator serving it to the next that
                # answered, for 300 s or more: its last answer stands, for a while.
                self._misses[index] += 1
                if self._misses[index] > KEPT_ROUNDS:
                    del self._answers[index]
                    log.warning(
                        "facilitator dropped: GET /supported has failed for too long",
                        extra={"facilitator": getattr(client, "url", type(client).__name__)},
                    )
        answered = [
            _Answered(self._facilitators[i], self._answers[i], self._methods, self._failover)
            for i in sorted(self._answers)
        ]
        for place, facilitator in enumerate(answered):
            facilitator.behind = answered[place + 1 :]
        routes = {n: self._serving(answered, n) for n, _ in self.accepted}
        served = [(n, pay_to) for n, pay_to in self.accepted if routes[n] is not None]
        if not served:
            # Also when it was serving until now: a quote nobody can settle can only fail.
            self._initialized = False
            log.error(
                "no facilitator offers x402 v2 'exact' on an accepted network: paid calls are "
                "refused (503, unpaid) until one does",
                extra={"networks": [n for n, _ in self.accepted]},
            )
            raise payments_unavailable()
        server = self._new_server(answered)
        server.initialize()  # no network: each _Answered repeats the answer it already gave
        self._check_price_floors(routes)
        self.server, self.served = server, served
        self._mcp_requirements.clear()
        self._initialized = True
        for network in sorted({n for n, _ in self.accepted} - {n for n, _ in served}):
            log.warning(
                "network left out of quotes: no facilitator offers x402 v2 'exact' on it",
                extra={"network": network},
            )
        missing = len(fresh) < len(asked) or len(served) < len(self.accepted)
        self._retry_at = self._clock() + RETRY_SKIPPED_S if missing else None

    async def _supported(self, client: FacilitatorClient) -> SupportedResponse | None:
        """One facilitator's GET /supported; None, with one warning, when it has no answer."""
        try:
            with anyio.fail_after(SUPPORTED_TIMEOUT_S):
                # Abandoned at the deadline: the SDK's blocking GET cannot be cancelled, and
                # ends by itself at its own 90 s timeout.
                return await anyio.to_thread.run_sync(client.get_supported, abandon_on_cancel=True)
        except Exception as exc:  # noqa: BLE001 - HTTP status, timeout, DNS, TLS, not JSON: skip
            # The type only. The SDK puts the response body in the message, and on
            # 2026-11-12 that body was a Cloudflare error page.
            log.warning(
                "facilitator skipped: GET /supported failed",
                extra={
                    "facilitator": getattr(client, "url", type(client).__name__),
                    "error": type(exc).__name__,
                },
            )
            return None

    @staticmethod
    def _serving(answered: list[_Answered], network: str) -> _Answered | None:
        """Who the SDK, given ``answered``, has verify and settle v2 'exact' on ``network``.

        The SDK gives a (network, scheme) to the first facilitator that lists it, in any x402
        version, so that one decides: a later facilitator's v2 offer is never used.
        """
        for facilitator in answered:
            offers = facilitator.offers(network)
            if offers is not None:
                return facilitator if offers else None
        return None

    def _check_price_floors(self, routes: dict[str, _Answered | None]) -> None:
        for network, facilitator in routes.items():
            floor = facilitator.price_floor(network) if facilitator else None
            if floor is None:
                continue
            for op in active_operations(self.settings):
                atomic = usd_to_atomic(op.price(self.settings))
                if atomic < floor:
                    log.error(
                        "price below facilitator minimum: these payments will be rejected",
                        extra={
                            "operation": op.key,
                            "network": network,
                            "price_atomic": atomic,
                            "floor_atomic": floor,
                        },
                    )

    def is_initialized(self) -> bool:
        return self._initialized

    async def aclose(self) -> None:
        for client in self._facilitators:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()

    # ------------------------------------------------------------------ shared config
    def networks(self) -> list[dict[str, str]]:
        return [
            {"network": n, "asset": "USDC", "payTo": a, "scheme": "exact"} for n, a in self.accepted
        ]

    def _extra(self) -> dict[str, str] | None:
        return {"tag": self.settings.bazaar_tag} if self.settings.bazaar_tag else None

    def resource_url(self, op: Operation, *, mcp: bool = False) -> str:
        base = self.settings.public_base_url
        return f"{base}/mcp#{op.tool}" if mcp else f"{base}{op.path}"

    # ------------------------------------------------------------------ REST paywall
    def routes(self) -> dict[str, RouteConfig]:
        routes: dict[str, RouteConfig] = {}
        for op in active_operations(self.settings):
            example = EXAMPLES[op.key]
            routes[op.route] = RouteConfig(
                accepts=[
                    PaymentOption(
                        scheme="exact",
                        price=op.price(self.settings),
                        network=network,
                        pay_to=pay_to,
                        max_timeout_seconds=PAYMENT_WINDOW_S,
                        extra=self._extra(),
                    )
                    for network, pay_to in (self.served if self._initialized else self.accepted)
                ],
                resource=self.resource_url(op),
                description=op.summary,
                mime_type="application/json",
                service_name=SERVICE_NAME,
                tags=TAGS,
                extensions=declare_discovery_extension(
                    input=example["input"],
                    input_schema=example["input_schema"],
                    body_type="form-data" if op.key == "convert_upload" else "json",
                    output=OutputConfig(
                        example=example_output(op.key, self.settings.public_base_url)
                    ),
                ),
            )
        return routes

    def http_middleware(
        self,
        *,
        prevalidate: Callable[[Request, bool], Awaitable[Response | None]] | None = None,
        store: ArtifactStore | None = None,
    ) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
        """The SDK middleware, built once, behind our lazy initialization.

        The SDK verifies before the route and settles after a < 400 response. With
        ``sync_facilitator_on_start=False`` it never initializes the resource server itself, so
        paid paths initialize here first; a facilitator outage becomes a 503 (nothing charged).
        The SDK middleware is rebuilt after each initialization, for the networks that one can
        settle: it quoted every accepted network, and one no facilitator offered was a bare
        500 (the SDK's SchemeNotFoundError) on every paid route.

        ``prevalidate(request, paying)`` answers a request that can never succeed (an invalid
        body) before the SDK quotes a price for it or verifies its payment. Artifacts a paid
        request stores are committed to ``store`` when its final status is < 400, and deleted
        otherwise: a failed settlement turns the route's 200 into a 402 and withholds the body,
        so that output was never paid for and must not stay, or have evicted anyone, in the
        artifact budget.

        A paid response whose client has disconnected becomes a 499 before the SDK settles it:
        the SDK settled every response below 400, so a caller that hung up while its call ran
        (its own timeout, or the front end's 504 at Cloud Run's request timeout) was charged
        for a response it never received. A disconnect during the settlement itself still
        settles; the request timeout is the payment window, past which nothing can.
        """
        # Built here too, before any facilitator has answered: a route the SDK refuses must
        # stop the app from starting, not the first paid request.
        inner = payment_middleware(self.routes(), self.server, sync_facilitator_on_start=False)
        inner_server = self.server
        paid = {(op.method, op.path) for op in active_operations(self.settings)}

        def withhold(produced: list[str], reason: str) -> None:
            if store is not None and produced:
                discard_withheld(store, list(produced), reason=reason)

        async def middleware(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            nonlocal inner, inner_server
            if (request.method, request.url.path) not in paid:
                return await inner(request, call_next)
            if prevalidate is not None:
                rejected = await prevalidate(request, carries_payment(request))
                if rejected is not None:
                    return rejected
            try:
                await self.ensure_initialized(paying=carries_payment(request))
                if inner_server is not self.server:
                    inner_server = self.server
                    inner = payment_middleware(
                        self.routes(), inner_server, sync_facilitator_on_start=False
                    )
            except Exception as exc:
                if not isinstance(exc, PaymentsUnavailable):  # that one is already logged
                    log.exception("facilitator initialization failed")
                unavailable = payments_unavailable()
                return JSONResponse(unavailable.to_dict(), status_code=unavailable.http_status)

            async def served(req: Request) -> Response:
                response = await call_next(req)
                if response.status_code < 400 and await req.is_disconnected():
                    gone = ClientDisconnected(
                        "The client disconnected before the response was ready.",
                        hint="Retry the call; you were not charged.",
                    )
                    log.warning("paid response withheld: the client disconnected")
                    return JSONResponse(gone.to_dict(), status_code=gone.http_status)
                return response

            with outputs_of(store) as produced:
                try:
                    response = await inner(request, served)
                except BaseException:
                    withhold(produced, "handler_raised")
                    raise
            if response.status_code >= 400:
                withhold(produced, f"http_{response.status_code}")
            elif store is not None and produced:
                store.commit(produced)
            return response

        return middleware

    # ------------------------------------------------------------------ MCP paywall
    async def mcp_requirements(
        self, op: Operation, *, paying: bool = False
    ) -> tuple[list[PaymentRequirements], ResourceInfo]:
        await self.ensure_initialized(paying=paying)
        reqs = self._mcp_requirements.get(op.tool)
        if reqs is None:
            reqs = []
            for network, pay_to in self.served:
                reqs += self.server.build_payment_requirements(
                    ResourceConfig(
                        scheme="exact",
                        network=network,
                        pay_to=pay_to,
                        price=op.price(self.settings),
                        max_timeout_seconds=PAYMENT_WINDOW_S,
                        extra=self._extra(),
                    )
                )
            self._mcp_requirements[op.tool] = reqs
        resource = ResourceInfo(
            url=self.resource_url(op, mcp=True),
            description=op.summary,
            mime_type="application/json",
        )
        return reqs, resource

    def mcp_extension(self, *, store: ArtifactStore | None = None) -> X402McpGate:
        return X402McpGate(self, store=store)

    def tool_operation(self, tool: str) -> Operation | None:
        """The paid operation behind an MCP tool, if this deployment serves it."""
        return self._by_tool.get(tool)

    # ------------------------------------------------------------------ discovery
    def discovery_document(self) -> dict[str, Any]:
        """x402scan's /.well-known/x402 compatibility format (not part of the x402 spec)."""
        return {
            "version": 1,
            "resources": [self.resource_url(op) for op in active_operations(self.settings)],
            "instructions": (
                "POST JSON (see /openapi.json). Unpaid requests get HTTP 402 with a base64 "
                "PAYMENT-REQUIRED header; retry with PAYMENT-SIGNATURE. MCP: /mcp, pay per tool "
                "call via _meta['x402/payment']."
            ),
        }
