"""Polymarket US live execution adapter. DISABLED BY DEFAULT and unreachable from the
recorder and paper processes (nothing there imports this package).

Design constraints from the verified API (docs/venue-verification.md):

* ``POST /v1/orders`` has no client-order-id field. Idempotency is local: every intent is
  persisted *before* submission, and a create call that times out is never blindly
  retried - its state is unknown until reconciliation finds (or fails to find) it.
* Orders carry ``manualOrderIndicator = AUTOMATIC``.
* Only limit orders with explicit prices are sent; IOC for entries/exits.
* During exchange latency spikes, unprocessed orders are rejected after 5 s with a
  "Global Rate Limit Exceeded" message - treated as a transient reject, not success.
* A cancel request is not a cancel: orders are canceled only when the exchange confirms.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from topspin.models.market import ONE, ZERO, MarketInfo
from topspin.models.orders import Order, TimeInForce


class OrderClient(Protocol):
    """The subset of the ``polymarket_us`` async client the adapter uses."""

    async def create_order(self, params: dict[str, Any]) -> dict[str, Any]: ...
    async def get_order(self, order_id: str) -> dict[str, Any]: ...
    async def cancel_order(self, order_id: str, market_slug: str) -> dict[str, Any]: ...
    async def open_orders(self) -> list[dict[str, Any]]: ...
    async def positions(self) -> dict[str, Any]: ...
    async def balances(self) -> dict[str, Any]: ...


class SdkOrderClient:
    """Thin wrapper over the official SDK. Imported lazily so that installing or importing
    the rest of the engine never requires (or loads) the order-capable SDK."""

    def __init__(self, key_id: str, secret_key: str, timeout_s: float = 10.0) -> None:
        from polymarket_us import AsyncPolymarketUS

        self._c = AsyncPolymarketUS(key_id=key_id, secret_key=secret_key, timeout=timeout_s)

    async def create_order(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._c.orders.create(params)

    async def get_order(self, order_id: str) -> dict[str, Any]:
        res = await self._c.orders.retrieve(order_id)
        return res.get("order", res)

    async def cancel_order(self, order_id: str, market_slug: str) -> dict[str, Any]:
        return await self._c.orders.cancel(order_id, {"marketSlug": market_slug})

    async def open_orders(self) -> list[dict[str, Any]]:
        res = await self._c.orders.list()
        return list(res.get("orders", []))

    async def positions(self) -> dict[str, Any]:
        return await self._c.portfolio.positions()

    async def balances(self) -> dict[str, Any]:
        return await self._c.account.balances()

    async def aclose(self) -> None:
        await self._c.close()


class OrderValidationError(ValueError):
    pass


def validate_order(order: Order, market: MarketInfo, max_order_value: Decimal) -> None:
    if not (ZERO < order.limit_price < ONE):
        raise OrderValidationError("price outside (0, 1)")
    if order.limit_price < Decimal("0.01") or order.limit_price > Decimal("0.99"):
        raise OrderValidationError("price outside exchange limits [0.01, 0.99]")
    if (order.limit_price / market.tick_size) % 1 != 0:
        raise OrderValidationError("price not on tick")
    if order.qty < market.min_qty or (order.qty / market.min_qty) % 1 != 0:
        raise OrderValidationError("quantity not a multiple of the minimum trade quantity")
    worst_cost = order.qty * order.intent.side_price(order.limit_price)
    if order.intent.opens and worst_cost > max_order_value:
        raise OrderValidationError("order value exceeds live cap")


def order_params(order: Order) -> dict[str, Any]:
    tif = (
        order.tif
        if order.tif in (TimeInForce.IOC, TimeInForce.GTD, TimeInForce.FOK)
        else TimeInForce.IOC
    )
    return {
        "marketSlug": order.market_slug,
        "intent": order.intent.value,
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": f"{order.limit_price}", "currency": "USD"},
        "quantity": float(order.qty),
        "tif": tif.value,
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    }


@dataclass
class OrderStore:
    """Append-only local record of every bot order intent and its exchange id."""

    path: Path

    def append(self, event: str, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": event, **payload}, default=str) + "\n")

    def bot_exchange_ids(self) -> set[str]:
        if not self.path.exists():
            return set()
        ids: set[str] = set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("exchange_order_id"):
                ids.add(str(rec["exchange_order_id"]))
        return ids

    def unresolved_intents(self) -> list[str]:
        """Client ids submitted without a known exchange id (unknown state)."""
        if not self.path.exists():
            return []
        intents: dict[str, bool] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            cid = rec.get("client_order_id")
            if not cid:
                continue
            if rec["event"] == "intent":
                intents.setdefault(cid, False)
            elif rec["event"] in ("acknowledged", "resolved"):
                intents[cid] = True
        return [c for c, ok in intents.items() if not ok]


class LiveExecutionAdapter:
    def __init__(self, client: OrderClient, store: OrderStore, max_order_value: Decimal) -> None:
        if max_order_value <= 0:
            raise ValueError("live adapter requires a positive order cap")
        self.client = client
        self.store = store
        self.max_order_value = max_order_value

    async def submit(self, order: Order, market: MarketInfo) -> str | None:
        """Submit once. Returns the exchange order id, or None if the outcome is unknown."""
        validate_order(order, market, self.max_order_value)
        self.store.append(
            "intent",
            {"client_order_id": order.client_order_id, "order": order.model_dump(mode="json")},
        )
        try:
            res = await self.client.create_order(order_params(order))
        except Exception as exc:  # timeout / connection: state unknown, do NOT retry
            self.store.append(
                "submit_unknown",
                {"client_order_id": order.client_order_id, "error": repr(exc)[:200]},
            )
            return None
        oid = str(res.get("id") or res.get("orderId") or "")
        if not oid:
            self.store.append(
                "submit_unknown", {"client_order_id": order.client_order_id, "response": res}
            )
            return None
        self.store.append(
            "acknowledged", {"client_order_id": order.client_order_id, "exchange_order_id": oid}
        )
        return oid

    async def cancel(self, exchange_order_id: str, market_slug: str) -> bool:
        """Request a cancel, then confirm by reading the order back."""
        try:
            await self.client.cancel_order(exchange_order_id, market_slug)
            o = await self.client.get_order(exchange_order_id)
        except Exception as exc:
            self.store.append(
                "cancel_unconfirmed",
                {"exchange_order_id": exchange_order_id, "error": repr(exc)[:200]},
            )
            return False
        state = str(o.get("state", ""))
        confirmed = state in (
            "ORDER_STATE_CANCELED",
            "ORDER_STATE_FILLED",
            "ORDER_STATE_REJECTED",
            "ORDER_STATE_EXPIRED",
        )
        self.store.append(
            "cancel_result",
            {"exchange_order_id": exchange_order_id, "state": state, "confirmed": confirmed},
        )
        return confirmed
