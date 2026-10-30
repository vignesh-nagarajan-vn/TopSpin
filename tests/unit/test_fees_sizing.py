"""Gate 2: fee schedule, execution direction, sizing and risk budgets."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from topspin.models.market import BookLevel, OrderBookView
from topspin.models.orders import Fill, Liquidity, OrderIntent
from topspin.models.positions import Ledger, Position
from topspin.risk.sizing import kelly_fraction, round_down_to_step, size_position
from topspin.simulation.fees import FeeModel, bankers_round_cents

FM = FeeModel()


# ---------------------------------------------------------------- fees (docs.polymarket.us/fees examples)


@pytest.mark.parametrize(
    ("qty", "price", "fee"),
    [
        (1000, "0.10", "6.26"),
        (1000, "0.65", "15.81"),
        (1000, "0.30", "14.60"),
        (1000, "0.90", "6.26"),
        (1000, "0.50", "17.38"),  # 17.375 -> banker's rounding to even
        (100, "0.50", "1.74"),
        (100, "0.01", "0.07"),
        (100, "0.99", "0.07"),
    ],
)
def test_documented_taker_fee_examples(qty, price, fee):
    assert FM.taker_fee(D(qty), D(price)) == D(fee)


def test_fee_symmetric_and_maximal_at_fifty_cents():
    assert FM.exact_taker_fee(D(100), D("0.5")) == D("1.7375")
    for p in ("0.1", "0.25", "0.4"):
        assert FM.exact_taker_fee(D(10), D(p)) == FM.exact_taker_fee(D(10), 1 - D(p))
        assert FM.exact_taker_fee(D(10), D(p)) < FM.exact_taker_fee(D(10), D("0.5"))


def test_bankers_rounding():
    assert bankers_round_cents(D("0.025")) == D("0.02")
    assert bankers_round_cents(D("0.035")) == D("0.04")
    assert bankers_round_cents(D("0.0251")) == D("0.03")


def test_table_tennis_theta_is_not_the_tennis_default():
    assert FM.taker_theta == D("0.0695")
    tt = FM.for_market(D("0.10"))
    assert tt.taker_fee(D(1000), D("0.5")) == D("25.00")
    assert FM.for_market(D("0.10"), use_market=False).taker_theta == D("0.0695")


def test_multi_fill_cumulative_cap_only_reduces():
    # Three fills of 1 contract at 0.5: exact 0.017375 each -> rounded 0.02 each = 0.06,
    # but the cumulative exact fee 0.052125 rounds to 0.05 -> the last charge is reduced.
    fills = [(D(1), D("0.5"))] * 3
    out = FM.order_fill_fees(fills)
    charged = [c for c, _ in out]
    assert sum(charged) == D("0.05")
    assert all(c <= bankers_round_cents(e) for c, e in out)
    # Running total is capped at each step: 0.02, then 0.03 (cap) - 0.02, then 0.05 - 0.03.
    assert charged == [D("0.02"), D("0.01"), D("0.02")]


def test_no_fee_outside_price_range():
    with pytest.raises(ValueError):
        FM.exact_taker_fee(D(1), D("1.0"))


# ---------------------------------------------------------------- intents and direction


def test_intent_directions():
    assert OrderIntent.BUY_LONG.consumes == "ask" and OrderIntent.SELL_LONG.consumes == "bid"
    assert OrderIntent.BUY_SHORT.consumes == "bid" and OrderIntent.SELL_SHORT.consumes == "ask"
    assert OrderIntent.BUY_SHORT.side_price(D("0.83")) == D("0.17")
    assert OrderIntent.BUY_LONG.closing_intent() == OrderIntent.SELL_LONG


def test_long_entry_uses_ask_and_exit_uses_bid():
    book = OrderBookView([BookLevel(D("0.55"), D(10))], [BookLevel(D("0.57"), D(10))])
    buy = book.walk(OrderIntent.BUY_LONG.consumes, D(5))
    sell = book.walk(OrderIntent.SELL_LONG.consumes, D(5))
    assert buy.avg_price == D("0.57") and sell.avg_price == D("0.55")
    short = book.walk(OrderIntent.BUY_SHORT.consumes, D(5))
    assert OrderIntent.BUY_SHORT.side_price(short.avg_price) == D("0.45")  # NO costs 1 - bid


# ---------------------------------------------------------------- positions / ledger


def fill(intent, price, qty, fee="0", fid="f1", slug="m", ts=1):
    return Fill(
        fill_id=fid,
        client_order_id="o",
        market_slug=slug,
        match_key="x",
        intent=intent,
        price=D(price),
        qty=D(qty),
        fee=D(fee),
        exact_fee=D(fee),
        liquidity=Liquidity.TAKER,
        local_ts_ns=ts,
    )


def test_ledger_long_round_trip_and_idempotent_fills():
    led = Ledger(starting_cash=D(100), cash=D(100))
    led.apply_fill(fill(OrderIntent.BUY_LONG, "0.40", 10, "0.17", "f1"))
    assert led.cash == D("95.83")
    led.apply_fill(fill(OrderIntent.BUY_LONG, "0.40", 10, "0.17", "f1"))  # duplicate ignored
    assert led.cash == D("95.83")
    pnl = led.apply_fill(fill(OrderIntent.SELL_LONG, "0.60", 10, "0.17", "f2"))
    # proceeds 6.00 - exit fee 0.17 - released cost (4.00 + entry fee 0.17)
    assert pnl == D("1.66") and led.cash == D("101.66")
    assert led.realized_pnl == D("1.66")


def test_ledger_short_settlement():
    led = Ledger(starting_cash=D(100), cash=D(100))
    led.apply_fill(fill(OrderIntent.BUY_SHORT, "0.70", 10, "0.15"))  # NO at 0.30
    pos = led.position("m")
    assert pos.side == "NO" and pos.cost_basis == D("3.15") and pos.max_settlement_loss == D("3.15")
    pnl = led.settle("m", D(0))  # YES loses -> NO pays 1
    assert pnl == D("10") - D("3.15") and led.cash == D("106.85")


def test_position_cannot_over_close():
    p = Position(market_slug="m", match_key="x", side="YES")
    p.apply_fill(fill(OrderIntent.BUY_LONG, "0.5", 2))
    with pytest.raises(ValueError):
        p.apply_fill(fill(OrderIntent.SELL_LONG, "0.5", 3, fid="f9"))


# ---------------------------------------------------------------- sizing


def test_kelly_fraction_with_fees():
    assert kelly_fraction(0.6, 0.5) == pytest.approx(0.2)
    assert kelly_fraction(0.5, 0.5) == 0.0
    assert kelly_fraction(0.4, 0.5) == 0.0


def test_quarter_kelly_capped_by_risk_budget():
    res = size_position(
        q_conservative=0.70,
        side_price=D("0.55"),
        fee_per_contract=D("0.017"),
        bankroll=D(1000),
        kelly_multiplier=0.25,
        max_loss_budget=D(20),
        cash_available=D(1000),
        qty_step=D("0.01"),
        min_qty=D("1"),
        max_qty=D(5000),
    )
    assert res.binding_constraint == "risk_budget"
    assert res.qty * res.cost_per_contract <= D(20)
    uncapped = size_position(
        q_conservative=0.70,
        side_price=D("0.55"),
        fee_per_contract=D("0.017"),
        bankroll=D(1000),
        kelly_multiplier=0.25,
        max_loss_budget=D(10_000),
        cash_available=D(10_000),
        qty_step=D("0.01"),
        min_qty=D(1),
        max_qty=D(10_000),
    )
    f_full = (0.70 - 0.567) / (1 - 0.567)
    assert float(uncapped.qty * uncapped.cost_per_contract) == pytest.approx(
        0.25 * f_full * 1000, rel=1e-3
    )


def test_minimum_size_above_budget_rejected():
    res = size_position(
        q_conservative=0.7,
        side_price=D("0.5"),
        fee_per_contract=D("0.017"),
        bankroll=D(1000),
        kelly_multiplier=0.25,
        max_loss_budget=D("0.30"),
        cash_available=D(1000),
        qty_step=D(1),
        min_qty=D(1),
        max_qty=D(100),
    )
    assert res.qty == 0 and res.binding_constraint.startswith("below_min_qty")


def test_round_down_to_step():
    assert round_down_to_step(D("3.999"), D("0.01")) == D("3.99")
    assert round_down_to_step(D("7"), D("5")) == D("5")
