"""Fractional-Kelly sizing for binary contracts with fees.

Holding one contract of the chosen side costs ``c = price + fee_per_contract`` up front
(the taker fee is paid whether the position wins or loses). It pays $1 if the side wins.
So per contract the net win is ``1 - c`` and the net loss is ``c``, and the Kelly fraction
of bankroll *staked* is

    f* = (q - c) / (1 - c)            (zero when q <= c)

with ``q`` the conservative win probability. The engine stakes ``kelly_multiplier * f*``
of bankroll, capped by every risk budget, rounded *down* to the market's quantity step.
This is valid for both long YES (``price`` = YES ask) and long NO / short YES
(``price`` = 1 - YES bid): both are fully collateralized and lose exactly ``c`` at worst.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from topspin.models.market import ONE, ZERO


def kelly_fraction(q: float, cost: float) -> float:
    if not (0.0 < cost < 1.0) or q <= cost:
        return 0.0
    return (q - cost) / (1.0 - cost)


def round_down_to_step(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("quantity step must be positive")
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass(frozen=True, slots=True)
class SizingResult:
    qty: Decimal
    kelly_full: float
    kelly_applied: float
    cost_per_contract: Decimal
    binding_constraint: str


def size_position(
    *,
    q_conservative: float,
    side_price: Decimal,
    fee_per_contract: Decimal,
    bankroll: Decimal,
    kelly_multiplier: float,
    max_loss_budget: Decimal,
    cash_available: Decimal,
    qty_step: Decimal,
    min_qty: Decimal,
    max_qty: Decimal,
    max_order_value: Decimal | None = None,
) -> SizingResult:
    cost = side_price + fee_per_contract
    if not (ZERO < cost < ONE):
        return SizingResult(ZERO, 0.0, 0.0, cost, "cost_out_of_range")
    f_full = kelly_fraction(q_conservative, float(cost))
    f = f_full * kelly_multiplier
    if f <= 0:
        return SizingResult(ZERO, f_full, f, cost, "no_kelly_edge")
    candidates: dict[str, Decimal] = {
        "kelly": Decimal(str(f)) * bankroll / cost,
        "risk_budget": max(ZERO, max_loss_budget) / cost,
        "cash": max(ZERO, cash_available) / cost,
        "max_qty": max_qty,
    }
    if max_order_value is not None:
        candidates["order_value_cap"] = max(ZERO, max_order_value) / cost
    binding = min(candidates, key=lambda k: candidates[k])
    qty = round_down_to_step(candidates[binding], qty_step)
    if qty < min_qty:
        return SizingResult(ZERO, f_full, f, cost, f"below_min_qty:{binding}")
    return SizingResult(qty, f_full, f, cost, binding)
