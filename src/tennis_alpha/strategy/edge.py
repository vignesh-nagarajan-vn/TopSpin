"""Conservative entry economics on executable prices.

For a candidate side (YES = player A wins, NO = player B wins) priced at the executable
held-side price ``x`` (YES ask for YES; ``1 - YES bid`` for NO), the conservative
expected value of one contract held to settlement is

    EV(x) = q_cons - x - fee(x) - haircut

where ``q_cons`` is the *least favourable* edge of the model band, ``fee(x)`` the exact
taker fee per contract and ``haircut`` = adverse-selection allowance + slippage ticks.
Midpoints are never used. The limit price is the worst book level at which EV still
clears ``min_net_edge``; depth is only counted up to that limit. Swing-trade exit value
is *not* used to justify entries in v1 (no fitted market-response model exists).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

from tennis_alpha.config import StrategyConfig
from tennis_alpha.models.market import ONE, ZERO, MarketInfo, OrderBookView
from tennis_alpha.models.orders import OrderIntent
from tennis_alpha.pricing.markov import ScoreState
from tennis_alpha.pricing.rulesets import Ruleset
from tennis_alpha.pricing.uncertainty import ProbEstimate
from tennis_alpha.risk.sizing import SizingResult, size_position
from tennis_alpha.simulation.fees import FeeModel
from tennis_alpha.strategy.signals import Reason


@dataclass(frozen=True, slots=True)
class SizingInputs:
    bankroll: Decimal
    kelly_multiplier: float
    max_loss_budget: Decimal
    cash_available: Decimal
    min_qty: Decimal
    max_qty: Decimal
    max_order_value: Decimal | None = None


@dataclass
class EntryCandidate:
    side: str
    intent: OrderIntent
    ok: bool = False
    reason: Reason = Reason.OK
    qty: Decimal = ZERO
    limit_price: Decimal | None = None  # YES price
    best_side_price: Decimal | None = None
    avg_side_price: Decimal | None = None
    q_fair: float = 0.0
    q_cons: float = 0.0
    raw_edge: float = 0.0
    edge_after_fees: float = 0.0
    net_edge_best: float = 0.0
    net_edge_per_contract: float = 0.0
    expected_net_pnl: float = 0.0
    fees: Decimal = ZERO
    max_loss: Decimal = ZERO
    available_qty: Decimal = ZERO
    hold_minutes: float = 0.0
    priority_score: float = 0.0
    log_growth: float = 0.0
    sizing: SizingResult | None = None
    extra: dict[str, float | str | None] = field(default_factory=dict)

    def numbers(self) -> dict[str, float | str | None]:
        return {
            "side": self.side,
            "q_fair": round(self.q_fair, 6),
            "q_cons": round(self.q_cons, 6),
            "best_side_price": None if self.best_side_price is None else float(self.best_side_price),
            "avg_side_price": None if self.avg_side_price is None else float(self.avg_side_price),
            "limit_yes_price": None if self.limit_price is None else float(self.limit_price),
            "raw_edge": round(self.raw_edge, 6),
            "edge_after_fees": round(self.edge_after_fees, 6),
            "net_edge_best": round(self.net_edge_best, 6),
            "net_edge_per_contract": round(self.net_edge_per_contract, 6),
            "expected_net_pnl": round(self.expected_net_pnl, 4),
            "qty": float(self.qty),
            "available_qty": float(self.available_qty),
            "fees": float(self.fees),
            "max_loss": float(self.max_loss),
            "hold_minutes": round(self.hold_minutes, 2),
            "priority_score": round(self.priority_score, 6),
            "log_growth": self.log_growth,
            "sizing_binding": None if self.sizing is None else self.sizing.binding_constraint,
            **self.extra,
        }


def expected_remaining_games(state: ScoreState, ruleset: Ruleset) -> float:
    """Screening heuristic only (labelled assumption): ~10 games per set, and future sets
    weighted by a rough chance they are played."""
    cur = max(1.0, 10.0 - (state.games_a + state.games_b))
    need_a = ruleset.sets_to_win - state.sets_a - 1
    need_b = ruleset.sets_to_win - state.sets_b - 1
    future_sets = 0.5 * (need_a + need_b) + 0.5 * min(need_a, need_b)
    return cur + 10.0 * max(0.0, future_sets)


def _side_levels(book: OrderBookView, side: str) -> list[tuple[Decimal, Decimal, Decimal]]:
    """(yes_price, side_price, qty) best-first for buying ``side``."""
    if side == "YES":
        return [(lv.price, lv.price, lv.qty) for lv in book.asks]
    return [(lv.price, ONE - lv.price, lv.qty) for lv in book.bids]


def evaluate_side(
    *,
    side: str,
    market: MarketInfo,
    book: OrderBookView,
    estimate: ProbEstimate,
    fee_model: FeeModel,
    cfg: StrategyConfig,
    sizing: SizingInputs,
    hold_minutes: float,
) -> EntryCandidate:
    intent = OrderIntent.BUY_LONG if side == "YES" else OrderIntent.BUY_SHORT
    est = estimate if side == "YES" else estimate.complement()
    cand = EntryCandidate(side=side, intent=intent, q_fair=est.fair, q_cons=est.low, hold_minutes=hold_minutes)
    levels = _side_levels(book, side)
    if not levels:
        cand.reason = Reason.INSUFFICIENT_DEPTH
        return cand
    haircut = cfg.adverse_selection_allowance + cfg.slippage_allowance_ticks * float(market.tick_size)

    def fee_rate(yes_px: Decimal) -> float:
        return float(fee_model.per_contract_rate(yes_px))

    yes0, x0, _ = levels[0]
    cand.best_side_price = x0
    cand.raw_edge = est.fair - float(x0)
    cand.edge_after_fees = cand.raw_edge - fee_rate(yes0)
    cand.net_edge_best = est.low - float(x0) - fee_rate(yes0) - haircut
    spread = book.spread
    cand.extra["spread"] = None if spread is None else float(spread)
    cand.extra["haircut"] = haircut

    if spread is None or float(spread) > cfg.max_spread + 1e-12:
        cand.reason = Reason.SPREAD_TOO_WIDE
        return cand
    if cand.raw_edge <= 0:
        cand.reason = Reason.MARKET_ALREADY_REPRICED
        return cand
    if cand.edge_after_fees <= 0:
        cand.reason = Reason.EDGE_CONSUMED_BY_FEES
        return cand
    if cand.net_edge_best < cfg.min_net_edge:
        point_net = est.fair - float(x0) - fee_rate(yes0) - haircut
        cand.reason = (
            Reason.EDGE_WITHIN_MODEL_UNCERTAINTY if point_net >= cfg.min_net_edge else Reason.EDGE_BELOW_THRESHOLD
        )
        return cand

    # Worst acceptable level and the depth available up to it.
    available = ZERO
    limit_yes: Decimal | None = None
    for yes_px, x, qty in levels:
        if est.low - float(x) - fee_rate(yes_px) - haircut < cfg.min_net_edge:
            break
        available += qty
        limit_yes = yes_px
    cand.available_qty = available
    cand.limit_price = limit_yes
    if available < Decimal(str(cfg.min_executable_depth)):
        cand.reason = Reason.INSUFFICIENT_DEPTH
        return cand

    sized = size_position(
        q_conservative=est.low - haircut,
        side_price=x0,
        fee_per_contract=fee_model.per_contract_rate(yes0),
        bankroll=sizing.bankroll,
        kelly_multiplier=sizing.kelly_multiplier,
        max_loss_budget=sizing.max_loss_budget,
        cash_available=sizing.cash_available,
        qty_step=market.min_qty,
        min_qty=max(sizing.min_qty, market.min_qty),
        max_qty=min(sizing.max_qty, available),
        max_order_value=sizing.max_order_value,
    )
    cand.sizing = sized
    if sized.qty <= 0:
        cand.reason = Reason.SIZE_BELOW_MINIMUM
        return cand

    walk = book.walk("ask" if side == "YES" else "bid", sized.qty, limit=limit_yes)
    qty = walk.filled_qty
    side_cost = walk.notional if side == "YES" else qty - walk.notional
    fills = [(lv.qty, lv.price) for lv in walk.levels]
    fees = sum((c for c, _ in fee_model.order_fill_fees(fills)), ZERO)
    ev_total = float(qty) * (est.low - haircut) - float(side_cost) - float(fees)
    cand.qty = qty
    cand.avg_side_price = side_cost / qty if qty > 0 else None
    cand.fees = fees
    cand.max_loss = side_cost + fees
    cand.expected_net_pnl = ev_total
    cand.net_edge_per_contract = ev_total / float(qty) if qty > 0 else 0.0
    hours = max(hold_minutes, cfg.min_holding_minutes) / 60.0
    cand.priority_score = ev_total / (float(cand.max_loss) * hours) if cand.max_loss > 0 else 0.0
    w = float(sizing.bankroll)
    win_gain = float(qty) - float(cand.max_loss)
    loss = float(cand.max_loss)
    if w > 0 and loss < w:
        cand.log_growth = est.low * math.log1p(win_gain / w) + (1 - est.low) * math.log1p(-loss / w)

    if cand.net_edge_per_contract < cfg.min_net_edge:
        cand.reason = Reason.EDGE_BELOW_THRESHOLD
        return cand
    if ev_total < cfg.min_expected_net_pnl:
        cand.reason = Reason.EV_BELOW_MIN_PNL
        return cand
    cand.ok = True
    cand.reason = Reason.OK
    return cand


_REASON_PROGRESS = [
    Reason.SPREAD_TOO_WIDE,
    Reason.MARKET_ALREADY_REPRICED,
    Reason.EDGE_CONSUMED_BY_FEES,
    Reason.EDGE_BELOW_THRESHOLD,
    Reason.EDGE_WITHIN_MODEL_UNCERTAINTY,
    Reason.INSUFFICIENT_DEPTH,
    Reason.SIZE_BELOW_MINIMUM,
    Reason.EV_BELOW_MIN_PNL,
    Reason.OK,
]


def best_entry(candidates: list[EntryCandidate]) -> EntryCandidate:
    """Prefer viable candidates by log growth; otherwise the one that got furthest."""
    viable = [c for c in candidates if c.ok]
    if viable:
        return max(viable, key=lambda c: (c.log_growth, c.expected_net_pnl))

    def progress(c: EntryCandidate) -> tuple[int, float]:
        idx = _REASON_PROGRESS.index(c.reason) if c.reason in _REASON_PROGRESS else -1
        return idx, c.raw_edge

    return max(candidates, key=progress)
