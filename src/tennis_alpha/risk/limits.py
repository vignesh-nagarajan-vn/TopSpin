"""Independent pre-trade risk engine. No order is authorized because a strategy wants it.

Budgets are expressed as *maximum settlement loss including fees*. Positions and resting
orders in the same match share one match-level budget and are never netted (a match
winner and a set winner on the same match are correlated). Exposure favouring different
players in the same match is rejected as contradictory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from tennis_alpha.config import RiskConfig
from tennis_alpha.models.market import ONE, ZERO, MarketInfo
from tennis_alpha.models.orders import Order, OrderIntent
from tennis_alpha.models.positions import Ledger
from tennis_alpha.risk.circuit_breaker import CircuitBreakers, HaltLatch


@dataclass
class RiskDecision:
    approved: bool
    qty: Decimal
    reasons: list[str] = field(default_factory=list)
    budgets: dict[str, str] = field(default_factory=dict)


def order_max_loss(order: Order, fee_rate_cap: Decimal = Decimal("0.0695") / 4) -> Decimal:
    """Worst-case settlement loss if a resting/pending opening order fully fills."""
    if not order.intent.opens:
        return ZERO
    side_px = order.intent.side_price(order.limit_price)
    return order.remaining * (side_px + fee_rate_cap)


def favoured_player(market: MarketInfo, holding: str) -> str:
    return market.long.team_id if holding == "YES" else market.short.team_id


class RiskEngine:
    def __init__(
        self,
        cfg: RiskConfig,
        latch: HaltLatch,
        breakers: CircuitBreakers,
        *,
        live_max_order_value: Decimal | None = None,
    ) -> None:
        self.cfg = cfg
        self.latch = latch
        self.breakers = breakers
        self.live_max_order_value = live_max_order_value

    @property
    def per_match_cap(self) -> Decimal:
        return self.cfg.bankroll * Decimal(str(self.cfg.max_risk_per_match_frac))

    @property
    def portfolio_cap(self) -> Decimal:
        return self.cfg.bankroll * Decimal(str(self.cfg.max_portfolio_risk_frac))

    def match_exposure(self, ledger: Ledger, open_orders: list[Order], match_key: str) -> Decimal:
        pending = sum((order_max_loss(o) for o in open_orders if o.match_key == match_key), ZERO)
        return ledger.match_max_loss(match_key) + pending

    def portfolio_exposure(self, ledger: Ledger, open_orders: list[Order]) -> Decimal:
        return ledger.total_max_loss() + sum((order_max_loss(o) for o in open_orders), ZERO)

    def remaining_budget(self, ledger: Ledger, open_orders: list[Order], match_key: str) -> Decimal:
        m = self.per_match_cap - self.match_exposure(ledger, open_orders, match_key)
        p = self.portfolio_cap - self.portfolio_exposure(ledger, open_orders)
        return max(ZERO, min(m, p))

    def entry_blocks(self, match_key: str) -> list[str]:
        reasons = []
        if self.latch.halted:
            reasons.append(f"halt_latch:{self.latch.reason}")
        reasons.extend(self.breakers.entry_block_reasons(match_key))
        return reasons

    def authorize_entry(
        self,
        *,
        market: MarketInfo,
        intent: OrderIntent,
        qty: Decimal,
        limit_price: Decimal,
        max_loss: Decimal,
        ledger: Ledger,
        open_orders: list[Order],
        markets: dict[str, MarketInfo],
        active_matches: set[str],
    ) -> RiskDecision:
        reasons = self.entry_blocks(market.match_key)
        if not intent.opens:
            reasons.append("not_an_entry_intent")
        if qty <= 0 or max_loss <= 0:
            reasons.append("non_positive_size")
        if qty < market.min_qty or (qty / market.min_qty) % 1 != 0:
            reasons.append("invalid_quantity_increment")
        if (limit_price / market.tick_size) % 1 != 0 or not (ZERO < limit_price < ONE):
            reasons.append("invalid_price_increment")

        # Contradictory / duplicate exposure in the same match.
        want = favoured_player(market, intent.holding)
        for pos in ledger.open_positions():
            if pos.match_key != market.match_key:
                continue
            held_market = markets.get(pos.market_slug)
            if held_market is None or favoured_player(held_market, pos.side) != want:
                reasons.append("contradictory_exposure")
        for o in open_orders:
            if o.match_key != market.match_key or o.state.terminal:
                continue
            if o.market_slug == market.slug:
                reasons.append("order_already_working_in_market")
            om = markets.get(o.market_slug)
            if o.intent.opens and (om is None or favoured_player(om, o.intent.holding) != want):
                reasons.append("contradictory_working_order")

        if market.match_key not in active_matches:
            reasons.append("match_not_tracked")

        match_exp = self.match_exposure(ledger, open_orders, market.match_key)
        port_exp = self.portfolio_exposure(ledger, open_orders)
        if match_exp + max_loss > self.per_match_cap:
            reasons.append("match_risk_budget")
        if port_exp + max_loss > self.portfolio_cap:
            reasons.append("portfolio_risk_budget")
        pending_open = sum((order_max_loss(o) for o in open_orders), ZERO)
        if pending_open + max_loss > self.cfg.bankroll * Decimal(str(self.cfg.max_unmatched_order_exposure_frac)):
            reasons.append("unmatched_order_exposure")
        if max_loss > ledger.cash - pending_open:
            reasons.append("insufficient_cash")
        if self.live_max_order_value is not None and max_loss > self.live_max_order_value:
            reasons.append("live_order_value_cap")
        budgets = {
            "match_exposure": str(match_exp),
            "per_match_cap": str(self.per_match_cap),
            "portfolio_exposure": str(port_exp),
            "portfolio_cap": str(self.portfolio_cap),
        }
        return RiskDecision(not reasons, qty if not reasons else ZERO, sorted(set(reasons)), budgets)

    def authorize_exit(self, *, market: MarketInfo, intent: OrderIntent, qty: Decimal, ledger: Ledger) -> RiskDecision:
        """Exits reduce risk; they are allowed even when entries are halted, but may never
        exceed the open position or flip it."""
        reasons: list[str] = []
        pos = ledger.position(market.slug)
        if intent.opens:
            reasons.append("not_an_exit_intent")
        if pos is None or pos.qty <= 0 or pos.side != intent.holding:
            reasons.append("no_matching_position")
        elif qty > pos.qty:
            reasons.append("exit_exceeds_position")
        if qty <= 0 or (qty / market.min_qty) % 1 != 0:
            reasons.append("invalid_quantity_increment")
        return RiskDecision(not reasons, qty if not reasons else ZERO, reasons)
