"""Startup / runtime reconciliation against authoritative exchange records.

Bot-managed orders are identified only by exchange ids recorded in the local order
store (the API offers no client-order-id). Any other open order is treated as a human
order and is never canceled; it is reported instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from topspin.live.execution import LiveExecutionAdapter


@dataclass
class ReconciliationReport:
    bot_open_orders: list[dict[str, Any]] = field(default_factory=list)
    foreign_open_orders: list[dict[str, Any]] = field(default_factory=list)
    unresolved_intents: list[str] = field(default_factory=list)
    canceled: list[str] = field(default_factory=list)
    cancel_failed: list[str] = field(default_factory=list)
    positions: dict[str, Any] = field(default_factory=dict)
    balances: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (
            self.cancel_failed or self.unresolved_intents or self.errors or self.bot_open_orders
        )


async def reconcile_and_cancel_bot_orders(adapter: LiveExecutionAdapter) -> ReconciliationReport:
    rep = ReconciliationReport()
    bot_ids = adapter.store.bot_exchange_ids()
    rep.unresolved_intents = adapter.store.unresolved_intents()
    try:
        open_orders = await adapter.client.open_orders()
    except Exception as exc:
        rep.errors.append(f"open_orders: {exc!r}"[:200])
        return rep
    for o in open_orders:
        (rep.bot_open_orders if str(o.get("id")) in bot_ids else rep.foreign_open_orders).append(o)
    for o in list(rep.bot_open_orders):
        oid, slug = str(o.get("id")), str(o.get("marketSlug"))
        if await adapter.cancel(oid, slug):
            rep.canceled.append(oid)
        else:
            rep.cancel_failed.append(oid)
    # Re-read to confirm nothing bot-owned remains open.
    try:
        remaining = await adapter.client.open_orders()
        rep.bot_open_orders = [o for o in remaining if str(o.get("id")) in bot_ids]
        rep.positions = await adapter.client.positions()
        rep.balances = await adapter.client.balances()
    except Exception as exc:
        rep.errors.append(f"post-cancel reconciliation: {exc!r}"[:200])
    return rep
