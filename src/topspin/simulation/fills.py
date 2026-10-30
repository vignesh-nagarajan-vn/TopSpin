"""Pure fill-simulation helpers (deliberately pessimistic)."""

from __future__ import annotations

from decimal import Decimal

from topspin.models.market import ONE, ZERO, OrderBookView
from topspin.models.orders import OrderIntent
from topspin.risk.sizing import round_down_to_step


def fillable_levels(
    intent: OrderIntent,
    limit_price: Decimal,
    prior: OrderBookView | None,
    after: OrderBookView | None,
    policy: str,
) -> list[tuple[Decimal, Decimal]]:
    """Displayed (price, qty) a marketable order could reach, best-first.

    * ``prior``     - the last book received before the order's arrival time.
    * ``after``     - the first book received at/after arrival.
    * ``intersect`` - only liquidity present in *both*, at the smaller size. Liquidity that
      was pulled around the arrival time is never assumed available.
    """
    side = intent.consumes

    def levels(book: OrderBookView | None) -> dict[Decimal, Decimal]:
        if book is None:
            return {}
        rows = book.asks if side == "ask" else book.bids
        return {lv.price: lv.qty for lv in rows}

    if policy == "prior":
        merged = levels(prior)
    elif policy == "next":
        merged = levels(after)
    else:
        a, b = levels(prior), levels(after)
        merged = {p: min(a[p], b[p]) for p in a.keys() & b.keys()}
    within = [
        (p, q)
        for p, q in merged.items()
        if q > 0 and ((side == "ask" and p <= limit_price) or (side == "bid" and p >= limit_price))
    ]
    within.sort(key=lambda x: x[0], reverse=(side == "bid"))
    return within


def allocate_taker(
    levels: list[tuple[Decimal, Decimal]],
    qty: Decimal,
    fill_fraction: float,
    qty_step: Decimal,
    consumed: dict[Decimal, Decimal],
    stress_ticks: int,
    tick: Decimal,
    intent: OrderIntent,
    limit_price: Decimal,
) -> list[tuple[Decimal, Decimal]]:
    """Return (yes_price, qty) fills. Each level contributes only ``fill_fraction`` of its
    displayed size, minus anything our own earlier fills already consumed."""
    remaining = qty
    fills: list[tuple[Decimal, Decimal]] = []
    frac = Decimal(str(fill_fraction))
    worse = 1 if intent.consumes == "ask" else -1
    for price, shown in levels:
        if remaining <= 0:
            break
        px = price + worse * stress_ticks * tick
        if not (ZERO < px < ONE):
            continue
        if (intent.consumes == "ask" and px > limit_price) or (
            intent.consumes == "bid" and px < limit_price
        ):
            continue
        avail = round_down_to_step(max(ZERO, shown * frac - consumed.get(price, ZERO)), qty_step)
        take = min(remaining, avail)
        if take <= 0:
            continue
        fills.append((px, take))
        consumed[price] = consumed.get(price, ZERO) + take
        remaining -= take
    return fills
