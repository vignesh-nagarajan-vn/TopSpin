"""Paper broker: delayed, pessimistic, partial, and never filled at vanished prices."""

from __future__ import annotations

from decimal import Decimal as D

from tests.synthetic import SLUG, T0, S, market
from topspin.config import SimulationConfig
from topspin.models.market import BookSnapshot, MarketState, TradePrint
from topspin.models.orders import Order, OrderIntent, OrderState, TimeInForce
from topspin.models.positions import Ledger
from topspin.simulation.broker import PaperBroker
from topspin.simulation.fees import FeeModel

MKT = market()


def mk_broker(**sim):
    cfg = SimulationConfig(**({"taker_delay_ms": 500, "paper_fill_fraction": 1.0} | sim))
    events = []
    led = Ledger(starting_cash=D(1000), cash=D(1000))
    b = PaperBroker(
        cfg,
        led,
        lambda s: FeeModel(),
        lambda s: MKT,
        max_book_staleness_s=5,
        sink=lambda k, p: events.append((k, p)),
    )
    return b, led, events


def bk(t, bids, asks, state=MarketState.OPEN):
    return BookSnapshot(
        market_slug=SLUG,
        bids=[[p, q] for p, q in bids],
        asks=[[p, q] for p, q in asks],
        state=state,
        transact_time_ns=t,
        local_ts_ns=t,
    )


def order(
    t, intent=OrderIntent.BUY_LONG, px="0.60", qty="10", tif=TimeInForce.IOC, post_only=False
):
    return Order(
        client_order_id=f"o{t}",
        market_slug=SLUG,
        match_key="e",
        intent=intent,
        tif=tif,
        limit_price=D(px),
        qty=D(qty),
        created_local_ts_ns=t,
        post_only=post_only,
    )


def test_taker_waits_for_delay_and_uses_book_at_arrival():
    b, led, _ = mk_broker()
    b.on_book(bk(T0, [("0.58", "100")], [("0.60", "100")]))
    o = b.submit(order(T0), T0)
    assert o.state == OrderState.PENDING_NEW and led.cash == D(1000)  # no fill, no profit yet
    b.on_book(bk(T0 + S, [("0.58", "100")], [("0.60", "100")]))
    assert o.state == OrderState.FILLED and o.filled_qty == D(10)
    assert led.position(SLUG).qty == D(10)


def test_price_that_vanished_before_arrival_is_not_filled():
    b, _, _ = mk_broker()
    b.on_book(bk(T0, [("0.58", "100")], [("0.60", "100")]))
    o = b.submit(order(T0, px="0.60"), T0)
    # Market reprices before the order arrives: 0.60 offer gone.
    b.on_book(bk(T0 + S, [("0.70", "100")], [("0.72", "100")]))
    assert o.state == OrderState.CANCELED and o.filled_qty == 0


def test_intersect_policy_takes_min_of_before_and_after():
    b, _, _ = mk_broker()
    b.on_book(bk(T0, [("0.58", "100")], [("0.60", "50"), ("0.61", "50")]))
    o = b.submit(order(T0, px="0.61", qty="100"), T0)
    b.on_book(bk(T0 + S, [("0.58", "100")], [("0.60", "20"), ("0.61", "50")]))
    assert o.filled_qty == D(70)  # 20 @0.60 (min of 50, 20) + 50 @0.61; IOC remainder canceled
    assert o.state == OrderState.CANCELED


def test_fill_fraction_and_partial_fill():
    b, _, events = mk_broker(paper_fill_fraction=0.5)
    b.on_book(bk(T0, [], [("0.60", "10")]))
    o = b.submit(order(T0, qty="10"), T0)
    b.on_book(bk(T0 + S, [], [("0.60", "10")]))
    assert o.filled_qty == D(5)
    fills = [p for k, p in events if k == "fill"]
    assert fills and fills[0]["fill"]["fee"] == str(FeeModel().taker_fee(D(5), D("0.60")))


def test_rejected_when_market_suspended_or_book_stale():
    b, _, _ = mk_broker()
    b.on_book(bk(T0, [("0.58", "100")], [("0.60", "100")], state=MarketState.SUSPENDED))
    o = b.submit(order(T0), T0)
    b.on_book(bk(T0 + S, [("0.58", "100")], [("0.60", "100")], state=MarketState.SUSPENDED))
    assert o.state == OrderState.REJECTED
    b2, _, _ = mk_broker()
    b2.on_book(bk(T0, [("0.58", "100")], [("0.60", "100")]))
    o2 = b2.submit(order(T0 + 20 * S), T0 + 20 * S)
    b2.on_book(bk(T0 + 21 * S, [("0.58", "100")], [("0.60", "100")]))
    assert o2.state == OrderState.REJECTED and o2.reason == "stale_book_at_arrival"


def test_no_book_after_arrival_rejects():
    b, _, _ = mk_broker()
    b.on_book(bk(T0, [("0.58", "100")], [("0.60", "100")]))
    o = b.submit(order(T0), T0)
    b.advance(T0 + 10 * S)
    assert o.state == OrderState.REJECTED and o.reason == "no_book_after_arrival"


def test_cancel_before_arrival_wins():
    b, _, _ = mk_broker(cancel_delay_ms=100)
    b.on_book(bk(T0, [("0.58", "100")], [("0.60", "100")]))
    o = b.submit(order(T0), T0)
    b.cancel(o.client_order_id, T0 + S // 10)
    b.on_book(bk(T0 + S, [("0.58", "100")], [("0.60", "100")]))
    assert o.state == OrderState.CANCELED and o.filled_qty == 0


def test_own_fills_consume_displayed_liquidity():
    b, _, _ = mk_broker()
    b.on_book(bk(T0, [], [("0.60", "10")]))
    o1 = b.submit(order(T0, qty="8"), T0)
    o2 = b.submit(order(T0 + 1, qty="8"), T0 + 1)
    b.on_book(bk(T0 + S, [], [("0.60", "10")]))
    assert o1.filled_qty + o2.filled_qty == D(10)


def test_short_entry_consumes_bids():
    b, led, _ = mk_broker()
    b.on_book(bk(T0, [("0.58", "100")], [("0.60", "100")]))
    o = b.submit(order(T0, intent=OrderIntent.BUY_SHORT, px="0.58", qty="10"), T0)
    b.on_book(bk(T0 + S, [("0.58", "100")], [("0.60", "100")]))
    pos = led.position(SLUG)
    assert o.filled_qty == D(10) and pos.side == "NO" and pos.avg_cost > D("0.42")


def test_maker_needs_queue_and_trades_to_fill():
    b, _, _ = mk_broker()
    b.on_book(bk(T0, [("0.58", "30")], [("0.62", "100")]))
    o = b.submit(order(T0, px="0.58", qty="10", tif=TimeInForce.GTC, post_only=True), T0)
    b.on_book(bk(T0 + S, [("0.58", "30")], [("0.62", "100")]))
    assert o.state == OrderState.OPEN and o.queue_ahead == D(30)
    b.on_trade(
        TradePrint(
            market_slug=SLUG, price="0.58", qty="25", trade_time_ns=None, local_ts_ns=T0 + 2 * S
        )
    )
    assert o.filled_qty == 0 and o.queue_ahead == D(5)  # still behind the queue
    b.on_trade(
        TradePrint(
            market_slug=SLUG, price="0.58", qty="10", trade_time_ns=None, local_ts_ns=T0 + 3 * S
        )
    )
    assert o.filled_qty == D(5)
    b.on_trade(
        TradePrint(
            market_slug=SLUG, price="0.57", qty="50", trade_time_ns=None, local_ts_ns=T0 + 4 * S
        )
    )
    assert o.state == OrderState.FILLED


def test_post_only_crossing_rejected():
    b, _, _ = mk_broker()
    b.on_book(bk(T0, [("0.58", "30")], [("0.60", "100")]))
    o = b.submit(order(T0, px="0.60", tif=TimeInForce.GTC, post_only=True), T0)
    b.on_book(bk(T0 + S, [("0.58", "30")], [("0.60", "100")]))
    assert o.state == OrderState.REJECTED


def test_settlement_after_fill_only():
    b, led, _ = mk_broker()
    b.on_book(bk(T0, [("0.58", "100")], [("0.60", "100")]))
    b.submit(order(T0, qty="10"), T0)
    b.on_book(bk(T0 + S, [("0.58", "100")], [("0.60", "100")]))
    pnl = b.settle(SLUG, D(1), T0 + 10 * S)
    fee = FeeModel().taker_fee(D(10), D("0.60"))
    assert pnl == D(10) - D(6) - fee and led.cash == D(1000) + pnl


def test_slippage_stress_worsens_price():
    b, _, events = mk_broker(slippage_stress_ticks=1)
    b.on_book(bk(T0, [], [("0.60", "100")]))
    b.submit(order(T0, px="0.61"), T0)
    b.on_book(bk(T0 + S, [], [("0.60", "100")]))
    f = next(p for k, p in events if k == "fill")
    assert D(f["fill"]["price"]) == D("0.61")
