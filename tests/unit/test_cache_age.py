"""Public REST data is CDN-cached for up to 30 s: data age must drive every freshness decision."""

from __future__ import annotations

from decimal import Decimal as D

import httpx
import pytest

from tests.synthetic import SLUG, T0, S, market
from topspin.analytics.latency import latency_study
from topspin.analytics.pnl import BookIndex
from topspin.analytics.reports import gate_evidence
from topspin.config import AppConfig, SimulationConfig
from topspin.feeds.market_feed import BookSync, MarketBookTracker
from topspin.feeds.polymarket_us import PolymarketUSPublicClient, cache_info, parse_book
from topspin.models.market import BookSnapshot, MarketState
from topspin.models.match import MatchStatus, ScoreSnapshot
from topspin.models.orders import Order, OrderIntent, OrderState
from topspin.models.positions import Ledger
from topspin.simulation.broker import PaperBroker
from topspin.simulation.fees import FeeModel

CC = {"cache-control": "public, max-age=30"}


# ---------------------------------------------------------------- reading response headers


@pytest.mark.parametrize(
    ("headers", "age"),
    [
        ({**CC, "age": "18", "cf-cache-status": "HIT"}, 18.0),
        ({**CC, "cf-cache-status": "HIT"}, 30.0),  # a hit of unknown age: assume the worst
        ({**CC, "cf-cache-status": "STALE"}, 30.0),
        ({"cf-cache-status": "HIT"}, None),  # cached, and no way to tell how old
        ({**CC, "cf-cache-status": "MISS"}, 0.0),
        ({**CC, "cf-cache-status": "EXPIRED"}, 0.0),
        ({}, 0.0),
    ],
)
def test_cache_info(headers, age):
    assert cache_info(headers).age_s == age


async def test_client_reports_cache_age():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"marketData": {"marketSlug": "m"}},
            headers={**CC, "age": "7", "cf-cache-status": "HIT"},
        )

    client = PolymarketUSPublicClient(transport=httpx.MockTransport(handler), per_second=1000)
    try:
        raw, cache = await client.fetch_book("m")
    finally:
        await client.aclose()
    assert cache.age_s == 7.0
    snap = parse_book(raw, local_ts_ns=T0, cache=cache)
    assert snap.cache_age_s == 7.0 and snap.as_of_ns() == T0 - 7 * S


# ---------------------------------------------------------------- as-of time


def book(t, age=None, source="rest", tt=None, bid="0.58", ask="0.60"):
    return BookSnapshot(
        market_slug=SLUG,
        bids=[(bid, "100")],
        asks=[(ask, "100")],
        state=MarketState.OPEN,
        transact_time_ns=tt,
        local_ts_ns=t,
        source=source,
        cache_age_s=age,
    )


def test_as_of_rules():
    assert book(T0, age=12).as_of_ns(30) == T0 - 12 * S
    assert book(T0).as_of_ns(30) == T0 - 30 * S  # public route, age not reported
    assert book(T0, source="websocket").as_of_ns(30) == T0  # streamed data has no cache
    score = ScoreSnapshot(
        match_key="e",
        provider="polymarket_us_event_state",
        status=MatchStatus.LIVE,
        competitor_ids=("1", "2"),
        local_ts_ns=T0,
    )
    assert score.as_of_ns(30) == T0 - 30 * S
    assert score.model_copy(update={"cache_age_s": 4.0}).as_of_ns(30) == T0 - 4 * S
    assert score.model_copy(update={"provider": "licensed"}).as_of_ns(30) == T0


# ---------------------------------------------------------------- book tracker


def test_cached_book_is_stale_even_if_just_received():
    tr = MarketBookTracker(SLUG, max_staleness_s=5, unknown_cache_age_s=30)
    tr.apply(book(T0, age=29, tt=1))
    assert tr.tradeable(T0) == (False, "book_stale")
    assert tr.age_ns(T0) == 29 * S


def test_low_age_response_is_tradeable_and_unknown_age_is_not():
    tr = MarketBookTracker(SLUG, 5, 30)
    tr.apply(book(T0, age=2, tt=1))
    assert tr.tradeable(T0) == (True, "ok")
    old = MarketBookTracker(SLUG, 5, 30)
    old.apply(book(T0, tt=1))  # recorded before cache ages were captured
    assert old.tradeable(T0) == (False, "book_stale")


def test_same_book_with_lower_age_confirms_freshness():
    tr = MarketBookTracker(SLUG, 5, 30)
    tr.apply(book(T0, age=29, tt=1))
    assert tr.apply(book(T0 + S, age=1, tt=1)).reason == "duplicate_refresh"
    assert tr.tradeable(T0 + S)[0]
    # A staler copy from another cache node must not make the book look older.
    tr.apply(book(T0 + 2 * S, age=25, tt=1))
    assert tr.tradeable(T0 + 2 * S)[0]


def test_older_data_from_another_cache_node_is_dropped():
    tr = MarketBookTracker(SLUG, 5, 30)
    tr.apply(book(T0, age=1, bid="0.70", ask="0.72"))
    res = tr.apply(book(T0 + S, age=20, bid="0.58", ask="0.60"))
    assert res == (False, "out_of_order") or not res.applied
    assert tr.view is not None and tr.view.best_bid == D("0.70") and tr.out_of_order == 1


def test_unchanged_book_after_gap_restores_sync():
    tr = MarketBookTracker(SLUG, 5)
    tr.apply(book(T0, tt=1))
    tr.mark_gap()
    assert tr.sync == BookSync.UNSYNCED
    tr.apply(book(T0 + S, tt=1))
    assert tr.tradeable(T0 + S)[0]


def test_valid_book_recovers_after_invalid_snapshot():
    tr = MarketBookTracker(SLUG, 5)
    tr.apply(book(T0, tt=1))
    assert not tr.apply(book(T0 + S, tt=1, bid="0.70", ask="0.60")).applied  # crossed
    assert tr.sync == BookSync.INVALID
    assert tr.apply(book(T0 + 2 * S, tt=1)).reason == "applied"
    assert tr.tradeable(T0 + 2 * S)[0]


# ---------------------------------------------------------------- paper broker


def broker():
    led = Ledger(starting_cash=D(1000), cash=D(1000))
    return PaperBroker(
        SimulationConfig(taker_delay_ms=500, paper_fill_fraction=1.0),
        led,
        lambda s: FeeModel(),
        lambda s: market(),
        max_book_staleness_s=5,
        unknown_cache_age_s=30,
    )


def order(t):
    return Order(
        client_order_id=f"o{t}",
        market_slug=SLUG,
        match_key="e",
        intent=OrderIntent.BUY_LONG,
        limit_price=D("0.60"),
        qty=D(10),
        created_local_ts_ns=t,
    )


def test_cached_response_received_after_arrival_does_not_fill():
    b = broker()
    b.on_book(book(T0, age=0))
    o = b.submit(order(T0), T0)
    # Received after arrival, but its data is 10 s old: it says nothing about the market then.
    b.on_book(book(T0 + S, age=10))
    assert o.state == OrderState.PENDING_NEW and o.filled_qty == 0
    b.on_book(book(T0 + 2 * S, age=1))
    assert o.state == OrderState.FILLED


def test_order_is_rejected_when_no_data_postdates_arrival():
    b = broker()
    b.on_book(book(T0, age=0))
    o = b.submit(order(T0), T0)
    b.on_book(book(T0 + S, age=20))
    b.advance(T0 + 10 * S)
    assert o.state == OrderState.REJECTED and o.reason == "no_book_after_arrival"


def test_stale_prior_book_rejects_on_arrival():
    b = broker()
    b.on_book(book(T0, age=25))
    o = b.submit(order(T0), T0)
    b.on_book(book(T0 + S, age=0))
    assert o.state == OrderState.REJECTED and o.reason == "stale_book_at_arrival"


# ---------------------------------------------------------------- analytics


def book_rec(t, age=None, source="rest", bid="0.58", ask="0.60"):
    payload = book(t, age=age, source=source, bid=bid, ask=ask).model_dump(mode="json")
    return {"kind": "book", "payload": payload, "local_ts_ns": t, "source_ts_ns": None}


def test_book_index_uses_data_time():
    recs = [
        book_rec(T0, age=20, bid="0.40", ask="0.42"),
        book_rec(T0 + S, age=1, bid="0.50", ask="0.52"),
    ]
    idx = BookIndex(recs)
    assert idx.mid(SLUG, T0 - 15 * S) == pytest.approx(0.41)
    assert idx.mid(SLUG, T0 + S) == pytest.approx(0.51)


def test_cached_route_gets_invalid_source_verdict_and_fails_gate():
    lat = latency_study([book_rec(T0, age=3)])
    assert lat["cached_book_share"] == 1.0 and lat["verdict"].startswith("invalid_source")
    assert lat["book_cache_age_s"]["n"] == 1
    gate = gate_evidence([book_rec(T0, age=3)], 1000.0, source="paper")["gate1_data_quality"]
    assert gate["checks"]["uncached_market_data"] is False and gate["passed"] is False


def test_uncached_source_keeps_normal_verdicts():
    lat = latency_study([book_rec(T0, source="websocket")])
    assert lat["cached_book_share"] == 0.0 and lat["verdict"].startswith("insufficient_data")


def test_feed_delay_is_reported_net_of_cache_age():
    score = ScoreSnapshot(
        match_key="e",
        provider="polymarket_us_event_state",
        status=MatchStatus.LIVE,
        competitor_ids=("1", "2"),
        local_ts_ns=T0,
        cache_age_s=15.0,
    ).model_dump(mode="json")
    rec = {"kind": "score", "payload": score, "local_ts_ns": T0, "source_ts_ns": T0 - 16 * S}
    lat = latency_study([rec])
    assert lat["score_feed_delay_ms"]["p50"] == pytest.approx(16000)
    assert lat["score_feed_delay_net_of_cache_ms"]["p50"] == pytest.approx(1000)


def test_default_config_assumes_worst_case_for_unknown_age():
    assert AppConfig().feeds.unknown_cache_age_s == 30.0
