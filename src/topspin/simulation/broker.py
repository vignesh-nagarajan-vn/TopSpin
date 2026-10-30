"""Paper broker: a deliberately pessimistic simulation of Polymarket US execution.

Taker orders
    Reach the simulated exchange ``taker_delay_ms`` after submission. They are matched
    against the book policy (default ``intersect``: liquidity present both in the last book
    before arrival and the first book after it), take at most ``paper_fill_fraction`` of
    each displayed level, respect the limit, walk levels, pay rounded taker fees, allow
    partial fills and complete non-fills, and are rejected if the market is not open or no
    fresh book exists at arrival. An order is never filled at a price that disappeared
    before it arrived.

Maker orders
    Rest at a valid tick, must not cross (post-only), join the *back* of the displayed queue
    and only fill when subsequent trade prints at a better price, or at our price after the
    queue ahead is exhausted, justify it. With no trade-print feed they never fill - a
    disclosed limitation, not an optimistic assumption.

Profit is never reported before a simulated fill exists.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from topspin.config import SimulationConfig
from topspin.models.market import (
    ZERO,
    BookSnapshot,
    MarketInfo,
    MarketState,
    OrderBookView,
    TradePrint,
)
from topspin.models.orders import Fill, Liquidity, Order, OrderState, TimeInForce
from topspin.models.positions import Ledger
from topspin.risk.sizing import round_down_to_step
from topspin.simulation.fees import FeeModel
from topspin.simulation.fills import allocate_taker, fillable_levels
from topspin.timeutil import NS_PER_MS, NS_PER_S

EventSink = Callable[[str, dict[str, object]], None]


@dataclass
class _BookState:
    view: OrderBookView | None = None
    state: MarketState = MarketState.UNKNOWN
    ts_ns: int | None = None
    consumed: dict[Decimal, Decimal] = field(default_factory=dict)


class PaperBroker:
    def __init__(
        self,
        cfg: SimulationConfig,
        ledger: Ledger,
        fee_model_for: Callable[[str], FeeModel],
        market_for: Callable[[str], MarketInfo | None],
        *,
        max_book_staleness_s: float,
        sink: EventSink | None = None,
    ) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self.fee_model_for = fee_model_for
        self.market_for = market_for
        self.max_staleness_ns = int(max_book_staleness_s * NS_PER_S)
        self.sink = sink or (lambda kind, payload: None)
        self.orders: dict[str, Order] = {}
        self.books: dict[str, _BookState] = {}
        self._cancel_at: dict[str, int] = {}
        self._fill_seq = 0

    # ------------------------------------------------------------------ order entry

    def submit(self, order: Order, now_ns: int) -> Order:
        order.state = OrderState.PENDING_NEW
        order.arrival_local_ts_ns = now_ns + self.cfg.taker_delay_ms * NS_PER_MS
        self.orders[order.client_order_id] = order
        self.sink("order_submitted", {"order": order.model_dump(mode="json")})
        return order

    def cancel(self, client_order_id: str, now_ns: int, reason: str = "requested") -> None:
        o = self.orders.get(client_order_id)
        if o is None or o.state.terminal:
            return
        o.state = OrderState.CANCEL_PENDING if o.state != OrderState.PENDING_NEW else o.state
        self._cancel_at[client_order_id] = now_ns + self.cfg.cancel_delay_ms * NS_PER_MS
        self.sink("cancel_requested", {"client_order_id": client_order_id, "reason": reason})

    def open_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if not o.state.terminal]

    # ------------------------------------------------------------------ time and data

    def advance(self, now_ns: int) -> None:
        """Process everything due at or before ``now_ns`` given the books seen so far."""
        for cid, at in sorted(self._cancel_at.items(), key=lambda kv: kv[1]):
            if at <= now_ns:
                self._finish_cancel(cid, at)
        for o in sorted(self.open_orders(), key=lambda x: x.arrival_local_ts_ns or 0):
            if o.state != OrderState.PENDING_NEW or o.arrival_local_ts_ns is None:
                if o.expire_local_ts_ns is not None and now_ns >= o.expire_local_ts_ns:
                    self._terminate(o, OrderState.EXPIRED, "expired", o.expire_local_ts_ns)
                continue
            if o.arrival_local_ts_ns > now_ns:
                continue
            if self._canceled_before_arrival(o):
                continue
            if self.cfg.fill_book_policy == "prior":
                self._arrive(
                    o, prior=self._book(o.market_slug), after=None, at=o.arrival_local_ts_ns
                )
            else:
                # Needs the first book after arrival; if none arrives while the last one is
                # still fresh, the order cannot be simulated honestly -> reject.
                bs = self._book(o.market_slug)
                if bs.ts_ns is not None and now_ns - o.arrival_local_ts_ns > self.max_staleness_ns:
                    self._terminate(o, OrderState.REJECTED, "no_book_after_arrival", now_ns)

    def on_book(self, snap: BookSnapshot) -> None:
        bs = self._book(snap.market_slug)
        try:
            view = snap.view()
            view.validate()
        except ValueError:
            view = None
        # 1. Orders that arrived before this snapshot see (prior, this) books. They share
        #    one view of "after" so our own fills deplete its displayed size.
        nxt = _BookState(view=view, state=snap.state, ts_ns=snap.local_ts_ns)
        for o in sorted(self.open_orders(), key=lambda x: x.arrival_local_ts_ns or 0):
            if o.market_slug != snap.market_slug or o.state != OrderState.PENDING_NEW:
                continue
            if o.arrival_local_ts_ns is None or o.arrival_local_ts_ns > snap.local_ts_ns:
                continue
            if self._canceled_before_arrival(o):
                continue
            if self.cfg.fill_book_policy == "prior":
                self._arrive(o, prior=bs, after=None, at=o.arrival_local_ts_ns)
            else:
                self._arrive(o, prior=bs, after=nxt, at=snap.local_ts_ns)
        # 2. Install the new book; our consumption only persists until it refreshes.
        bs.view = view
        bs.state = snap.state
        bs.ts_ns = snap.local_ts_ns
        bs.consumed = dict(nxt.consumed)

    def on_trade(self, tp: TradePrint) -> None:
        price = Decimal(tp.price)
        qty = Decimal(tp.qty)
        for o in self.open_orders():
            if o.market_slug != tp.market_slug or o.state not in (
                OrderState.OPEN,
                OrderState.PARTIALLY_FILLED,
            ):
                continue
            buying_yes = o.intent.consumes == "ask"  # our resting order is a YES bid
            better = price < o.limit_price if buying_yes else price > o.limit_price
            at_px = price == o.limit_price
            if not (better or at_px):
                continue
            avail = qty
            if at_px:
                ahead = o.queue_ahead or ZERO
                used = min(ahead, avail)
                o.queue_ahead = ahead - used
                avail -= used
            take = min(avail, o.remaining)
            mkt = self.market_for(o.market_slug)
            step = mkt.min_qty if mkt else Decimal("1")
            take = round_down_to_step(take, step)
            if take > 0:
                self._record_fills(o, [(o.limit_price, take)], Liquidity.MAKER, tp.local_ts_ns)

    def on_market_state(self, slug: str, state: MarketState, now_ns: int) -> None:
        self._book(slug).state = state

    def settle(self, slug: str, yes_price: Decimal, now_ns: int) -> Decimal:
        for o in self.open_orders():
            if o.market_slug == slug:
                self._terminate(o, OrderState.CANCELED, "market_settled", now_ns)
        pnl = self.ledger.settle(slug, yes_price)
        self.sink("settlement", {"market_slug": slug, "yes_price": str(yes_price), "pnl": str(pnl)})
        return pnl

    # ------------------------------------------------------------------ internals

    def _canceled_before_arrival(self, o: Order) -> bool:
        at = self._cancel_at.get(o.client_order_id)
        if at is not None and o.arrival_local_ts_ns is not None and at <= o.arrival_local_ts_ns:
            self._terminate(o, OrderState.CANCELED, "canceled_before_arrival", at)
            return True
        return False

    def _book(self, slug: str) -> _BookState:
        return self.books.setdefault(slug, _BookState())

    def _arrive(self, o: Order, prior: _BookState, after: _BookState | None, at: int) -> None:
        mkt = self.market_for(o.market_slug)
        if mkt is None:
            self._terminate(o, OrderState.REJECTED, "unknown_market", at)
            return
        ref = after if after is not None else prior
        if ref.state != MarketState.OPEN or prior.state != MarketState.OPEN:
            self._terminate(o, OrderState.REJECTED, "market_not_open", at)
            return
        if (
            prior.view is None
            or prior.ts_ns is None
            or (o.arrival_local_ts_ns or at) - prior.ts_ns > self.max_staleness_ns
        ):
            self._terminate(o, OrderState.REJECTED, "stale_book_at_arrival", at)
            return
        if (o.limit_price / mkt.tick_size) % 1 != 0 or (o.qty / mkt.min_qty) % 1 != 0:
            self._terminate(o, OrderState.REJECTED, "invalid_increment", at)
            return

        if o.tif in (TimeInForce.IOC, TimeInForce.FOK) or not o.post_only:
            levels = fillable_levels(
                o.intent,
                o.limit_price,
                prior.view,
                after.view if after else None,
                self.cfg.fill_book_policy,
            )
            if (
                o.tif == TimeInForce.FOK
                and sum((q for _, q in levels), ZERO) * Decimal(str(self.cfg.paper_fill_fraction))
                < o.qty
            ):
                self._terminate(o, OrderState.CANCELED, "fok_not_fillable", at)
                return
            consumed = (after or prior).consumed
            fills = allocate_taker(
                levels,
                o.remaining,
                self.cfg.paper_fill_fraction,
                mkt.min_qty,
                consumed,
                self.cfg.slippage_stress_ticks,
                mkt.tick_size,
                o.intent,
                o.limit_price,
            )
            o.state = OrderState.OPEN
            self.sink("order_ack", {"client_order_id": o.client_order_id, "at": at})
            if fills:
                self._record_fills(o, fills, Liquidity.TAKER, at)
            if o.remaining > 0 and o.tif in (TimeInForce.IOC, TimeInForce.FOK):
                self._terminate(o, OrderState.CANCELED, "ioc_remainder", at)
            elif (
                o.remaining > 0
                and o.post_only is False
                and o.tif in (TimeInForce.GTC, TimeInForce.GTD)
            ):
                self._rest(o, prior.view, at)
            return
        # Post-only resting order.
        book = prior.view
        crosses = (
            o.intent.consumes == "ask"
            and book.best_ask is not None
            and o.limit_price >= book.best_ask
        ) or (
            o.intent.consumes == "bid"
            and book.best_bid is not None
            and o.limit_price <= book.best_bid
        )
        if crosses:
            self._terminate(o, OrderState.REJECTED, "post_only_would_cross", at)
            return
        o.state = OrderState.OPEN
        self.sink("order_ack", {"client_order_id": o.client_order_id, "at": at})
        self._rest(o, book, at)

    def _rest(self, o: Order, book: OrderBookView, at: int) -> None:
        same_side = book.bids if o.intent.consumes == "ask" else book.asks
        o.queue_ahead = sum((lv.qty for lv in same_side if lv.price == o.limit_price), ZERO)
        if o.state == OrderState.PENDING_NEW:
            o.state = OrderState.OPEN

    def _record_fills(
        self, o: Order, fills: list[tuple[Decimal, Decimal]], liq: Liquidity, at: int
    ) -> None:
        fee_model = self.fee_model_for(o.market_slug)
        # fills are (price, qty); the fee model takes (qty, price).
        fees = (
            fee_model.order_fill_fees([(q, p) for p, q in fills])
            if liq == Liquidity.TAKER
            else [(ZERO, ZERO)] * len(fills)
        )
        for (px, qty), (charged, exact) in zip(fills, fees, strict=True):
            self._fill_seq += 1
            fill = Fill(
                fill_id=f"{o.client_order_id}-f{self._fill_seq}",
                client_order_id=o.client_order_id,
                market_slug=o.market_slug,
                match_key=o.match_key,
                intent=o.intent,
                price=px,
                qty=qty,
                fee=charged,
                exact_fee=exact,
                liquidity=liq,
                local_ts_ns=at,
            )
            o.filled_qty += qty
            pnl = self.ledger.apply_fill(fill)
            self.sink("fill", {"fill": fill.model_dump(mode="json"), "realized_pnl": str(pnl)})
        o.state = OrderState.FILLED if o.remaining <= 0 else OrderState.PARTIALLY_FILLED
        if o.state == OrderState.FILLED:
            self.sink(
                "order_update",
                {"client_order_id": o.client_order_id, "state": o.state.value, "at": at},
            )

    def _finish_cancel(self, cid: str, at: int) -> None:
        self._cancel_at.pop(cid, None)
        o = self.orders.get(cid)
        if o is None or o.state.terminal:
            return
        self._terminate(o, OrderState.CANCELED, "cancel_confirmed", at)

    def _terminate(self, o: Order, state: OrderState, reason: str, at: int) -> None:
        o.state = state
        o.reason = reason
        self._cancel_at.pop(o.client_order_id, None)
        self.sink(
            "order_update",
            {
                "client_order_id": o.client_order_id,
                "state": state.value,
                "reason": reason,
                "filled_qty": str(o.filled_qty),
                "at": at,
            },
        )
