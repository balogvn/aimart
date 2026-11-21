"""The Apify side: input, the run's storages and pay-per-event charging (apify SDK 4.2.0).

Charging is read from the SDK's ``ChargingManager`` (docs.apify.com/sdk/python/docs/concepts/
pay-per-event). On the platform the prices come from the Actor's monetization settings; with
``ACTOR_TEST_PAY_PER_EVENT=true`` locally every event costs $1 and nothing is billed (the charges
are logged to a local ``charging-log`` dataset), so ``ACTOR_MAX_TOTAL_CHARGE_USD`` exercises the
limit. A run that is not pay-per-event (before the owner sets prices) charges nothing.
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from apify import Actor

from aimart_actor.core import (
    DATASET_ITEM_EVENT,
    PARQUET_CONTENT_TYPE,
    build_service,
    pricing_problem,
    run,
)
from aimart_parquet.errors import AimartError

if TYPE_CHECKING:
    from apify.storages import KeyValueStore


class ApifyPlatform:
    """``core.Platform`` over the SDK: the default key-value store and dataset, and charging."""

    def __init__(self, kvs: KeyValueStore) -> None:
        self._kvs = kvs
        self._charging = Actor.get_charging_manager()

    @property
    def _paid(self) -> bool:
        return self._charging.get_pricing_info().is_pay_per_event

    def pricing_problem(self) -> str | None:
        """``core.pricing_problem`` for this run's prices; checked on the platform only (a
        local test run has no prices: every event costs $1 there)."""
        if not self._paid or not Actor.is_at_home():
            return None
        return pricing_problem(self._charging.get_pricing_info().per_event_prices)

    def _price(self, event: str) -> Decimal:
        """An event's price as the SDK charges it: the Actor's price on the platform, $0 there
        for an event the pricing does not list; locally (ACTOR_TEST_PAY_PER_EVENT) $1 for any
        event, the platform's own dataset-item event included (apify 4.2.0,
        ``ChargingManager._get_event_price``)."""
        prices = self._charging.get_pricing_info().per_event_prices
        if event in prices:
            return prices[event]
        return Decimal(0) if Actor.is_at_home() else Decimal(1)

    def affordable(self, event: str) -> bool:
        if not self._paid:
            return True
        # The event, and the dataset item that reports it, which the platform charges too when
        # its own per-item event has a price.
        need = self._price(event) + self._price(DATASET_ITEM_EVENT)
        spent = self._charging.calculate_total_charged_amount()
        return need <= self._charging.get_max_total_charge_usd() - spent

    async def charge(self, event: str, *, key: str) -> bool | None:
        if not self._paid:
            return None  # not pay-per-event (no prices set yet): the SDK would only warn
        result = await Actor.charge(event, idempotency_key=key)
        return result.charged_count >= 1

    async def save(self, key: str, path: Path) -> str:
        data = await anyio.Path(path).read_bytes()
        await self._kvs.set_value(key, data, content_type=PARQUET_CONTENT_TYPE)
        # A record's public URL carries a signature when the store is restricted, and stays
        # valid as long as the record exists (docs.apify.com/account/collaboration/
        # general-resource-access, "Pre-signed URLs").
        return await self._kvs.get_public_url(key)

    async def discard(self, key: str) -> None:
        await self._kvs.delete_value(key)

    async def push(self, item: dict[str, Any]) -> None:
        await Actor.push_data(item)

    async def recall(self, key: str) -> Any:
        return await self._kvs.get_value(key)

    async def remember(self, key: str, value: dict[str, Any]) -> None:
        await self._kvs.set_value(key, value, content_type="application/json")

    async def last_index(self) -> int:
        dataset = await Actor.open_dataset()
        # The platform returns only the field asked for; the local file storage does not take
        # `fields` (it warns), so it reads the whole last item.
        fields = ["index"] if Actor.is_at_home() else None
        page = await dataset.get_data(desc=True, limit=1, fields=fields)
        return int(page.items[0]["index"]) if page.items else 0


async def main() -> None:
    await Actor.init()
    try:
        platform = ApifyPlatform(await Actor.open_key_value_store())
        problem = platform.pricing_problem()
        if problem is not None:
            await Actor.fail(status_message=problem)
            return
        raw = await Actor.get_input()
        with tempfile.TemporaryDirectory(prefix="aimart-") as tmp:
            try:
                summary = await run(raw, build_service(Path(tmp)), platform)
            except AimartError as exc:  # the input itself: nothing fetched, nothing charged
                await Actor.fail(status_message=f"{exc.message} {exc.hint or ''}".strip())
                return
    except Exception as exc:  # noqa: BLE001 - a bug: the run fails, nothing more is charged
        await Actor.fail(exception=exc)
        return
    await Actor.exit(status_message=summary.message)
