"""Position management: hold, reduce or close, by expected log wealth.

For a position of ``n`` contracts on a side that wins with model probability ``q`` (point
estimate), selling ``k`` contracts now at executable prices yields net proceeds ``P(k)``
(walking the closing side of the book, minus rounded taker fees). With ``base`` the
bankroll excluding this position, expected log wealth is

    U(k) = q * ln(base + P(k) + (n - k)) + (1 - q) * ln(base + P(k))

The policy picks the ``k`` that maximizes ``U`` over a grid (book levels and 10% steps).
This compares continuation value, immediate liquidation, holding to settlement and the
risk of an adverse state in one well-defined utility, without fixed take-profit targets.
Exits never assume the midpoint, and nothing is sold into a suspended or untradeable book.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

from topspin.models.market import ONE, ZERO, OrderBookView
from topspin.models.orders import OrderIntent
from topspin.models.positions import Position
from topspin.pricing.uncertainty import ProbEstimate
from topspin.risk.sizing import round_down_to_step
from topspin.simulation.fees import FeeModel
from topspin.strategy.signals import Action, Reason


@dataclass
class ExitDecision:
    action: Action
    reason: Reason
    qty: Decimal = ZERO
    intent: OrderIntent | None = None
    limit_price: Decimal | None = None  # YES price
    numbers: dict[str, float | str | None] = field(default_factory=dict)


def _proceeds(
    book: OrderBookView, side: str, k: Decimal, fee_model: FeeModel
) -> tuple[Decimal, Decimal | None, Decimal]:
    """(net proceeds, worst YES price touched, qty actually fillable) for selling k."""
    if k <= 0:
        return ZERO, None, ZERO
    walk = book.walk("bid" if side == "YES" else "ask", k)
    gross = walk.notional if side == "YES" else walk.filled_qty - walk.notional
    fees = sum(
        (c for c, _ in fee_model.order_fill_fees([(lv.qty, lv.price) for lv in walk.levels])), ZERO
    )
    return gross - fees, walk.worst_price, walk.filled_qty


def evaluate_exit(
    *,
    position: Position,
    estimate: ProbEstimate,
    book: OrderBookView,
    fee_model: FeeModel,
    base_wealth: Decimal,
    qty_step: Decimal,
    min_improvement: float = 0.0,
) -> ExitDecision:
    n = position.qty
    if n <= 0:
        return ExitDecision(Action.HOLD, Reason.CONTINUATION_BEATS_LIQUIDATION)
    q = estimate.fair if position.side == "YES" else 1.0 - estimate.fair
    base = float(base_wealth)
    if base <= 0:
        base = 1e-9

    def utility(k: Decimal) -> tuple[float, Decimal, Decimal | None, Decimal]:
        proceeds, worst, filled = _proceeds(book, position.side, k, fee_model)
        remaining = float(n - filled)
        p = float(proceeds)
        u = q * math.log(base + p + remaining) + (1.0 - q) * math.log(base + p)
        return u, proceeds, worst, filled

    grid: set[Decimal] = {ZERO, n}
    for i in range(1, 10):
        grid.add(round_down_to_step(n * Decimal(i) / 10, qty_step))
    cum = ZERO
    for lv in book.bids if position.side == "YES" else book.asks:
        cum += lv.qty
        if cum >= n:
            break
        grid.add(round_down_to_step(cum, qty_step))
    grid = {k for k in grid if ZERO <= k <= n}

    u_hold = utility(ZERO)[0]
    best_k, best_u, best_worst, best_filled, best_proceeds = ZERO, u_hold, None, ZERO, ZERO
    for k in sorted(grid):
        if k == 0:
            continue
        u, proceeds, worst, filled = utility(k)
        if u > best_u + 1e-15:
            best_k, best_u, best_worst, best_filled, best_proceeds = k, u, worst, filled, proceeds

    hold_ce = math.exp(u_hold)
    numbers: dict[str, float | str | None] = {
        "q_point": round(q, 6),
        "position_qty": float(n),
        "avg_cost": float(position.avg_cost),
        "ce_wealth_hold": hold_ce,
        "ce_wealth_best": math.exp(best_u),
        "best_sell_qty": float(best_k),
        "best_sell_proceeds": float(best_proceeds),
    }
    gain_per_contract = (math.exp(best_u) - hold_ce) / float(n)
    if best_k == 0 or best_filled <= 0 or gain_per_contract <= min_improvement:
        return ExitDecision(Action.HOLD, Reason.CONTINUATION_BEATS_LIQUIDATION, numbers=numbers)
    intent = OrderIntent.SELL_LONG if position.side == "YES" else OrderIntent.SELL_SHORT
    action = Action.SELL if best_filled >= n else Action.REDUCE
    reason = (
        Reason.LIQUIDATION_BEATS_HOLD
        if action == Action.SELL
        else Reason.PARTIAL_LIQUIDATION_OPTIMAL
    )
    return ExitDecision(action, reason, best_filled, intent, best_worst, numbers)


def liquidation_value(
    position: Position, book: OrderBookView | None, fee_model: FeeModel
) -> Decimal:
    """Conservative mark: what selling everything now would net (zero if no book)."""
    if position.qty <= 0 or book is None:
        return ZERO
    proceeds, _, _ = _proceeds(book, position.side, position.qty, fee_model)
    return max(ZERO, proceeds)


def settlement_value(position: Position, yes_wins: bool) -> Decimal:
    payout = ONE if (position.side == "YES") == yes_wins else ZERO
    return payout * position.qty
