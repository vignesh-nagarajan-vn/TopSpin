"""Versioned Polymarket US fee model.

Schedule ``polymarket-us-2026-10-19`` (https://docs.polymarket.us/fees, effective
2026-10-19 13:00 ET; verified 2026-10-29):

* Taker fee per fill = theta * C * p * (1 - p), theta = 0.0695 standard (Table Tennis 0.10,
  excluded from v1). ``p`` is the YES trade price, ``C`` the contract count.
* Fees are rounded to $0.01 with banker's rounding (round half to even).
* For an aggressive order filling against several resting orders, each fill is charged
  its rounded fee, adjusted so the order's total never exceeds the rounding of the
  cumulative exact fee. The adjustment only ever reduces a fill's charge.
* Maker rebates (tiers 2-4) exist but are never counted as expected profit in v1;
  tier 1 (below $10M monthly maker volume) receives no rebate.
* No fee on cancel / expiry / rejection.

A market's own ``feeCoefficient`` field overrides the default theta when configured.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from tennis_alpha.models.market import ONE, ZERO

CENT = Decimal("0.01")
SCHEDULE_VERSION = "polymarket-us-2026-10-19"
STANDARD_TAKER_THETA = Decimal("0.0695")


def bankers_round_cents(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class FeeModel:
    taker_theta: Decimal = STANDARD_TAKER_THETA
    maker_theta: Decimal = ZERO  # tier-1 maker: no rebate; rebates never modelled as profit
    version: str = SCHEDULE_VERSION

    def for_market(self, market_theta: Decimal | None, use_market: bool = True) -> FeeModel:
        if use_market and market_theta is not None and market_theta > 0:
            return FeeModel(taker_theta=market_theta, maker_theta=self.maker_theta, version=self.version)
        return self

    def exact_taker_fee(self, qty: Decimal, price: Decimal) -> Decimal:
        if not (ZERO < price < ONE):
            raise ValueError(f"price {price} outside (0, 1)")
        if qty < 0:
            raise ValueError("negative quantity")
        return self.taker_theta * qty * price * (ONE - price)

    def taker_fee(self, qty: Decimal, price: Decimal) -> Decimal:
        """Rounded fee for a single fill."""
        return bankers_round_cents(self.exact_taker_fee(qty, price))

    def order_fill_fees(self, fills: list[tuple[Decimal, Decimal]]) -> list[tuple[Decimal, Decimal]]:
        """Per-fill (charged, exact) taker fees for one aggressive order's fills.

        ``fills`` is a list of (qty, price) in execution order.
        """
        out: list[tuple[Decimal, Decimal]] = []
        charged_total = ZERO
        exact_total = ZERO
        for qty, price in fills:
            exact = self.exact_taker_fee(qty, price)
            exact_total += exact
            charge = bankers_round_cents(exact)
            cap = bankers_round_cents(exact_total)
            if charged_total + charge > cap:
                charge = max(ZERO, cap - charged_total)
            charged_total += charge
            out.append((charge, exact))
        return out

    def per_contract_rate(self, price: Decimal) -> Decimal:
        """Exact (unrounded) taker fee per contract at ``price`` - for sizing/EV."""
        return self.taker_theta * price * (ONE - price)
