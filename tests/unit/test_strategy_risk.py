"""Entry edge cascade, exit policy, risk engine, circuit breakers and config safety."""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from tests.synthetic import SLUG, market
from topspin.config import AppConfig, StrategyConfig, load_config
from topspin.models.market import BookLevel, OrderBookView
from topspin.models.orders import Fill, Liquidity, Order, OrderIntent
from topspin.models.positions import Ledger, Position
from topspin.pricing.uncertainty import ProbEstimate
from topspin.risk.circuit_breaker import CircuitBreakers, DailyLossMonitor, HaltLatch
from topspin.risk.limits import RiskEngine
from topspin.simulation.fees import FeeModel
from topspin.strategy.edge import SizingInputs, best_entry, evaluate_side
from topspin.strategy.exit_policy import evaluate_exit
from topspin.strategy.signals import Action, Reason

CFG = StrategyConfig()
SIZ = SizingInputs(
    bankroll=D(1000),
    kelly_multiplier=0.25,
    max_loss_budget=D(20),
    cash_available=D(1000),
    min_qty=D(1),
    max_qty=D(5000),
)


def book(bid, ask, qty="500", levels=3):
    t = D("0.01")
    return OrderBookView(
        [BookLevel(D(bid) - i * t, D(qty)) for i in range(levels)],
        [BookLevel(D(ask) + i * t, D(qty)) for i in range(levels)],
    )


def ev(side, est, bk, **kw):
    return evaluate_side(
        side=side,
        market=market(),
        book=bk,
        estimate=est,
        fee_model=FeeModel(),
        cfg=kw.get("cfg", CFG),
        sizing=kw.get("sizing", SIZ),
        hold_minutes=60,
    )


def test_viable_long_entry_and_limit_price():
    c = ev("YES", ProbEstimate(0.73, 0.71, 0.75), book("0.59", "0.60"))
    assert c.ok and c.intent == OrderIntent.BUY_LONG and c.reason == Reason.OK
    assert c.limit_price is not None and c.limit_price >= D("0.60")
    assert c.max_loss <= D(20) and c.expected_net_pnl > CFG.min_expected_net_pnl
    assert c.net_edge_per_contract >= CFG.min_net_edge


def test_edge_cascade_reasons():
    assert (
        ev("YES", ProbEstimate(0.60, 0.58, 0.62), book("0.60", "0.61")).reason
        == Reason.MARKET_ALREADY_REPRICED
    )
    assert (
        ev("YES", ProbEstimate(0.615, 0.60, 0.63), book("0.60", "0.61")).reason
        == Reason.EDGE_CONSUMED_BY_FEES
    )
    # Point estimate would clear the bar; the conservative band does not.
    assert (
        ev("YES", ProbEstimate(0.68, 0.62, 0.74), book("0.60", "0.61")).reason
        == Reason.EDGE_WITHIN_MODEL_UNCERTAINTY
    )
    assert (
        ev("YES", ProbEstimate(0.645, 0.64, 0.65), book("0.60", "0.61")).reason
        == Reason.EDGE_BELOW_THRESHOLD
    )
    assert (
        ev("YES", ProbEstimate(0.8, 0.78, 0.82), book("0.50", "0.60")).reason
        == Reason.SPREAD_TOO_WIDE
    )
    assert (
        ev("YES", ProbEstimate(0.8, 0.78, 0.82), book("0.59", "0.60", qty="5", levels=1)).reason
        == Reason.INSUFFICIENT_DEPTH
    )


def test_short_side_uses_bids_and_complement():
    c = ev("NO", ProbEstimate(0.45, 0.43, 0.47), book("0.60", "0.61"))
    assert c.ok and c.intent == OrderIntent.BUY_SHORT
    assert c.best_side_price == D("0.40")  # 1 - best bid
    assert c.q_cons == pytest.approx(0.53)


def test_best_entry_prefers_viable():
    yes = ev("YES", ProbEstimate(0.73, 0.71, 0.75), book("0.59", "0.60"))
    no = ev("NO", ProbEstimate(0.73, 0.71, 0.75), book("0.59", "0.60"))
    assert not no.ok and best_entry([no, yes]) is yes


def test_exit_policy_holds_when_bid_below_value_and_sells_when_overpriced():
    pos = Position(market_slug=SLUG, match_key="e", side="YES", qty=D(30), cost_basis=D("18.5"))
    hold = evaluate_exit(
        position=pos,
        estimate=ProbEstimate(0.80, 0.78, 0.82),
        book=book("0.74", "0.75"),
        fee_model=FeeModel(),
        base_wealth=D(1000),
        qty_step=D(1),
    )
    assert hold.action == Action.HOLD
    sell = evaluate_exit(
        position=pos,
        estimate=ProbEstimate(0.80, 0.78, 0.82),
        book=book("0.90", "0.91"),
        fee_model=FeeModel(),
        base_wealth=D(1000),
        qty_step=D(1),
    )
    assert sell.action == Action.SELL and sell.intent == OrderIntent.SELL_LONG and sell.qty == D(30)
    # NO position closes with SELL_SHORT, valued at 1 - ask.
    npos = Position(market_slug=SLUG, match_key="e", side="NO", qty=D(30), cost_basis=D("9"))
    s2 = evaluate_exit(
        position=npos,
        estimate=ProbEstimate(0.80, 0.78, 0.82),
        book=book("0.05", "0.06"),
        fee_model=FeeModel(),
        base_wealth=D(1000),
        qty_step=D(1),
    )
    assert s2.action in (Action.SELL, Action.REDUCE) and s2.intent == OrderIntent.SELL_SHORT


def test_exit_reduces_when_depth_is_thin():
    pos = Position(market_slug=SLUG, match_key="e", side="YES", qty=D(100), cost_basis=D(60))
    thin = OrderBookView(
        [BookLevel(D("0.95"), D(40)), BookLevel(D("0.50"), D(500))], [BookLevel(D("0.96"), D(500))]
    )
    res = evaluate_exit(
        position=pos,
        estimate=ProbEstimate(0.80, 0.78, 0.82),
        book=thin,
        fee_model=FeeModel(),
        base_wealth=D(1000),
        qty_step=D(1),
    )
    assert res.action == Action.REDUCE and res.qty == D(40) and res.limit_price == D("0.95")


# ---------------------------------------------------------------- risk engine


def risk(tmp_path, **kw):
    cfg = AppConfig()
    latch = HaltLatch(tmp_path / "latch.json")
    br = CircuitBreakers()
    return RiskEngine(cfg.risk, latch, br), latch, br


def fill(intent, slug=SLUG, qty="10", price="0.6", fid="f"):
    return Fill(
        fill_id=fid,
        client_order_id="o",
        market_slug=slug,
        match_key=market().match_key,
        intent=intent,
        price=D(price),
        qty=D(qty),
        fee=D("0.1"),
        exact_fee=D("0.1"),
        liquidity=Liquidity.TAKER,
        local_ts_ns=1,
    )


def test_risk_budgets_and_contradictory_exposure(tmp_path):
    eng, latch, _ = risk(tmp_path)
    led = Ledger(starting_cash=D(1000), cash=D(1000))
    mk = market()
    kw = dict(
        market=mk, ledger=led, open_orders=[], markets={mk.slug: mk}, active_matches={mk.match_key}
    )
    ok = eng.authorize_entry(
        intent=OrderIntent.BUY_LONG, qty=D(30), limit_price=D("0.60"), max_loss=D("18.5"), **kw
    )
    assert ok.approved
    too_big = eng.authorize_entry(
        intent=OrderIntent.BUY_LONG, qty=D(40), limit_price=D("0.60"), max_loss=D("24.7"), **kw
    )
    assert not too_big.approved and "match_risk_budget" in too_big.reasons
    led.apply_fill(fill(OrderIntent.BUY_LONG))
    contra = eng.authorize_entry(
        intent=OrderIntent.BUY_SHORT, qty=D(5), limit_price=D("0.59"), max_loss=D(3), **kw
    )
    assert not contra.approved and "contradictory_exposure" in contra.reasons
    bad_tick = eng.authorize_entry(
        intent=OrderIntent.BUY_LONG, qty=D(1), limit_price=D("0.605"), max_loss=D(1), **kw
    )
    assert "invalid_price_increment" in bad_tick.reasons
    latch.trip("daily_loss_limit", 1)
    halted = eng.authorize_entry(
        intent=OrderIntent.BUY_LONG, qty=D(1), limit_price=D("0.60"), max_loss=D(1), **kw
    )
    assert not halted.approved and any(r.startswith("halt_latch") for r in halted.reasons)
    # Exits remain possible while halted, but never larger than the position.
    assert eng.authorize_exit(
        market=mk, intent=OrderIntent.SELL_LONG, qty=D(10), ledger=led
    ).approved
    assert not eng.authorize_exit(
        market=mk, intent=OrderIntent.SELL_LONG, qty=D(11), ledger=led
    ).approved


def test_working_orders_count_against_budget(tmp_path):
    eng, _, _ = risk(tmp_path)
    led = Ledger(starting_cash=D(1000), cash=D(1000))
    mk = market()
    o = Order(
        client_order_id="w",
        market_slug="other",
        match_key=mk.match_key,
        intent=OrderIntent.BUY_LONG,
        limit_price=D("0.6"),
        qty=D(30),
        created_local_ts_ns=0,
    )
    res = eng.authorize_entry(
        market=mk,
        intent=OrderIntent.BUY_LONG,
        qty=D(10),
        limit_price=D("0.6"),
        max_loss=D("6.2"),
        ledger=led,
        open_orders=[o],
        markets={mk.slug: mk},
        active_matches={mk.match_key},
    )
    assert not res.approved and "match_risk_budget" in res.reasons


def test_halt_latch_persists_and_requires_human_reset(tmp_path):
    p = tmp_path / "latch.json"
    HaltLatch(p).trip("daily_loss_limit", 123)
    reloaded = HaltLatch(p)
    assert reloaded.halted and reloaded.reason == "daily_loss_limit"
    with pytest.raises(ValueError):
        reloaded.reset(operator="op", reason="r", reconciled=False, ts_ns=1)
    with pytest.raises(ValueError):
        reloaded.reset(operator=" ", reason="r", reconciled=True, ts_ns=1)
    reloaded.reset(operator="op", reason="reviewed", reconciled=True, ts_ns=2)
    assert not HaltLatch(p).halted
    assert [h["event"] for h in HaltLatch(p).state["history"]] == ["trip", "reset"]


def test_daily_loss_monitor_trips_and_does_not_reset_at_midnight(tmp_path):
    latch = HaltLatch(tmp_path / "l.json")
    mon = DailyLossMonitor(latch, 0.05)
    day = 86_400 * 1_000_000_000
    assert not mon.update(D(1000), 10)
    assert mon.update(D(949), 20)
    assert latch.halted
    mon.update(D(1000), day + 10)  # next UTC day
    assert latch.halted


def test_emergency_stop_file(tmp_path):
    br = CircuitBreakers(emergency_stop_file=tmp_path / "STOP")
    assert br.entry_block_reasons() == []
    (tmp_path / "STOP").write_text("x")
    assert "emergency_stop" in br.entry_block_reasons()


# ---------------------------------------------------------------- configuration safety


@pytest.mark.parametrize(
    "override",
    [
        {"strategy": {"point_scalping": True}},
        {"strategy": {"market_making": True}},
        {"strategy": {"allow_swing_ev_entry": True}},
        {"risk": {"kelly_multiplier": 1.0}},
        {"risk": {"max_risk_per_match_frac": 0.2, "max_portfolio_risk_frac": 0.1}},
        {"risk": {"bankroll": "0"}},
        {"live": {"enabled": True}},  # live flag outside live mode
        {"model": {"serve_baseline": {"ATP": 0.95}}},
        {"strategy": {"unknown_key": 1}},
    ],
)
def test_unsafe_or_malformed_config_rejected(override):
    with pytest.raises(ValidationError):
        AppConfig.model_validate(override)


def test_default_config_files_load_and_are_safe():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    cfg = load_config(root / "config" / "default.yaml", root / "config" / "paper.yaml")
    assert cfg.live.enabled is False and cfg.live.max_order_value == 0
    assert cfg.risk.kelly_multiplier == 0.25 and cfg.risk.bankroll == D(1000)
    assert cfg.strategy.point_scalping is False and cfg.strategy.market_making is False
    with pytest.raises(ValidationError):
        load_config(root / "config" / "default.yaml", root / "config" / "live.example.yaml")
