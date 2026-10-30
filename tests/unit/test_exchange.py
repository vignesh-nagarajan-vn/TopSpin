"""Order book, Polymarket US payload parsing, and identity matching."""

from __future__ import annotations

import copy
import json
from decimal import Decimal as D
from pathlib import Path

import pytest

from topspin.discovery.matching import (
    ExternalMatch,
    MappingRejected,
    OverrideFile,
    link_by_names,
    link_same_payload,
    names_match,
    normalize_text,
)
from topspin.discovery.tournaments import TournamentClassifier
from topspin.feeds.market_feed import BookSync, MarketBookTracker
from topspin.feeds.polymarket_us import (
    ParseError,
    parse_book,
    parse_event_score,
    parse_market,
    parse_score_string,
    tournament_hint,
)
from topspin.models.market import (
    BookLevel,
    BookSnapshot,
    InvalidBookError,
    MarketKind,
    MarketState,
    OrderBookView,
)
from topspin.models.match import MatchStatus

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "polymarket_us"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def events():
    return json.loads((FIX / "events_snapshot_2026-10-30.json").read_text(encoding="utf-8"))


def ev_by_slug(events, slug):
    return next(e for e in events if e["slug"] == slug)


# ---------------------------------------------------------------- order book


def lv(p, q):
    return BookLevel(D(p), D(q))


def test_best_bid_and_ask_ignore_array_order():
    book = OrderBookView(
        [lv("0.40", 5), lv("0.45", 3), lv("0.42", 1)], [lv("0.55", 2), lv("0.47", 4), lv("0.50", 1)]
    )
    assert book.best_bid == D("0.45") and book.best_ask == D("0.47")
    assert book.spread == D("0.02") and book.mid == D("0.46")


def test_depth_and_walk():
    book = OrderBookView(
        [lv("0.45", 3), lv("0.44", 7)], [lv("0.47", 4), lv("0.48", 6), lv("0.52", 10)]
    )
    assert book.depth("ask") == D(20)
    assert book.depth("ask", within=D("0.01")) == D(10)
    w = book.walk("ask", D(8), limit=D("0.48"))
    assert (
        w.filled_qty == D(8)
        and w.notional == D("0.47") * 4 + D("0.48") * 4
        and w.worst_price == D("0.48")
    )
    w2 = book.walk("ask", D(30), limit=D("0.48"))
    assert w2.filled_qty == D(10)  # limit stops the walk
    w3 = book.walk("bid", D(5))
    assert w3.levels[0].price == D("0.45")


def test_duplicate_price_levels_aggregate():
    book = OrderBookView([lv("0.40", 2), lv("0.40", 3)], [])
    assert book.bids[0].qty == D(5) and not book.two_sided


def test_crossed_or_invalid_book_rejected():
    with pytest.raises(InvalidBookError):
        OrderBookView([lv("0.50", 1)], [lv("0.49", 1)]).validate()
    with pytest.raises(InvalidBookError):
        OrderBookView([lv("1.20", 1)], [])


def snap(
    slug="m",
    bids=(("0.45", "10"),),
    asks=(("0.47", "10"),),
    t=1_000,
    tt=1_000,
    state=MarketState.OPEN,
):
    return BookSnapshot(
        market_slug=slug,
        bids=list(bids),
        asks=list(asks),
        state=state,
        transact_time_ns=tt,
        local_ts_ns=t,
    )


def test_tracker_sync_states_duplicates_and_out_of_order():
    tr = MarketBookTracker("m", max_staleness_s=5)
    assert tr.tradeable(0) == (False, "book_unsynced")
    assert tr.apply(snap(t=1_000_000_000, tt=10)).reason == "applied"
    assert tr.tradeable(2_000_000_000)[0]
    # identical re-delivery only refreshes
    assert tr.apply(snap(t=3_000_000_000, tt=10)).reason == "duplicate_refresh"
    assert tr.duplicates == 1 and tr.last_receipt_ns == 3_000_000_000
    # older transact time is out of order and ignored
    assert not tr.apply(snap(bids=(("0.30", "1"),), t=4_000_000_000, tt=5)).applied
    assert tr.view.best_bid == D("0.45")
    # stale after max age
    assert tr.tradeable(9_000_000_000) == (False, "book_stale")
    # gap -> unsynced until a fresh snapshot rebuilds it
    tr.mark_gap()
    assert tr.tradeable(3_100_000_000) == (False, "book_unsynced")
    assert tr.apply(snap(t=10_000_000_000, tt=20)).applied and tr.sync == BookSync.SYNCED


def test_tracker_crossed_and_suspended():
    tr = MarketBookTracker("m", 5)
    assert not tr.apply(snap(bids=(("0.50", "1"),), asks=(("0.49", "1"),))).applied
    assert tr.sync == BookSync.INVALID
    tr.apply(snap(t=2, tt=2, state=MarketState.SUSPENDED))
    assert tr.sync == BookSync.NOT_OPEN and not tr.tradeable(3)[0]


# ---------------------------------------------------------------- Polymarket US parsing


def test_parse_real_book_fixture():
    raw = json.loads((FIX / "book_wta_tokyo_live.json").read_text(encoding="utf-8"))
    b = parse_book(raw, local_ts_ns=1)
    v = b.view()
    assert v.best_bid == D("0.58") and v.best_ask == D("0.59") and b.state == MarketState.OPEN
    assert (
        b.transact_time_ns is not None and b.transact_time_ns % 1_000 != 0
    )  # nanosecond precision kept
    # Reversed arrays parse identically.
    raw2 = copy.deepcopy(raw)
    raw2["marketData"]["bids"].reverse()
    raw2["marketData"]["offers"].reverse()
    v2 = parse_book(raw2, local_ts_ns=1).view()
    assert (v2.best_bid, v2.best_ask) == (v.best_bid, v.best_ask)


def test_score_string_parsing():
    sets, pts = parse_score_string("7-6(7-3), 2-1:15-40")
    assert sets == [(7, 6, (7, 3)), (2, 1, None)] and pts == ("15", "40")
    assert parse_score_string("3-3:A-40")[1] == ("A", "40")
    with pytest.raises(ParseError):
        parse_score_string("6-x")


def test_parse_live_event_maps_points_by_competitor(events):
    e = ev_by_slug(events, "wta-miapoh-alypar-2026-10-29")
    s = parse_event_score(e, local_ts_ns=5)
    assert s.status == MatchStatus.LIVE and s.server_id == "8479"
    assert s.sets == [{"8479": 3, "4711": 2}] and s.points == {"8479": "0", "4711": "0"}
    assert s.capabilities.points and s.capabilities.server and s.capabilities.source_timestamp
    assert s.tournament_name == "WTA Tokyo, Japan Women Singles"


def test_ambiguous_score_order_yields_unknown_points(events):
    e = ev_by_slug(events, "atp-yardem-harmay-2026-10-29")  # "3-3:30-0"
    s = parse_event_score(e, local_ts_ns=5)
    assert s.points is None and "score_string_order_ambiguous" in s.quality_flags
    assert not s.capabilities.points


def test_parse_suspended_finished_and_not_started(events):
    assert (
        parse_event_score(ev_by_slug(events, "atp-emicam-joasil-2026-10-29"), 1).status
        == MatchStatus.SUSPENDED
    )
    fin = parse_event_score(ev_by_slug(events, "wta-tamzid-alilin-2026-10-29"), 1)
    assert fin.status == MatchStatus.FINISHED and fin.tiebreaks[0] == {"3389": 7, "4598": 3}
    ns = parse_event_score(ev_by_slug(events, "atp-meralk-yuhhsu-2026-10-29"), 1)
    assert ns.status == MatchStatus.NOT_STARTED and ns.sets == [] and ns.server_id is None


def test_parse_market_orientation_and_metadata(events):
    e = ev_by_slug(events, "atp-omajas-yunchu-2026-10-30")
    mi = parse_market(e, e["markets"][0])
    assert mi.kind == MarketKind.MATCH_WINNER and mi.tour == "ATP"
    assert mi.long.team_id == "4716" and mi.long.name == "Omar Jasika"  # YES = long side
    assert mi.short.team_id == "4944"
    assert (
        mi.tick_size == D("0.01") and mi.min_qty == D("0.01") and mi.fee_coefficient == D("0.0695")
    )
    assert mi.long.provider_ids["PROVIDER_SPORTRADAR"].startswith("sr:competitor:")


def test_unknown_or_table_tennis_market_types_rejected(events):
    e = ev_by_slug(events, "atp-omajas-yunchu-2026-10-30")
    m = copy.deepcopy(e["markets"][0])
    for smt in ("table_tennis_match_winner", "tennis_match_exact_score", "tennis_new_thing", None):
        m["sportsMarketType"] = smt
        with pytest.raises(ParseError):
            parse_market(e, m)
    m["sportsMarketType"] = "tennis_set_1_winner"
    with pytest.raises(ParseError):  # set markets not enabled by default
        parse_market(e, m)


def test_doubles_and_mismatched_participants_rejected(events):
    e = copy.deepcopy(ev_by_slug(events, "atp-omajas-yunchu-2026-10-30"))
    m = e["markets"][0]
    m["marketSides"][0]["team"]["name"] = "Jasika / Purcell"
    with pytest.raises(ParseError):
        parse_market(e, m)
    e2 = copy.deepcopy(ev_by_slug(events, "atp-omajas-yunchu-2026-10-30"))
    e2["teams"][1]["id"] = 99999
    with pytest.raises(ParseError):
        parse_market(e2, e2["markets"][0])


def test_same_payload_link_requires_exact_ids(events):
    e = ev_by_slug(events, "wta-miapoh-alypar-2026-10-29")
    mi = parse_market(e, e["markets"][0])
    s = parse_event_score(e, 1)
    link = link_same_payload(mi, s)
    assert link.long_competitor_id == mi.long.team_id and link.method == "same_payload_ids"
    bad = s.model_copy(update={"competitor_ids": ("8479", "1")})
    with pytest.raises(MappingRejected):
        link_same_payload(mi, bad)


def test_tournament_hint_from_tags():
    assert (
        tournament_hint(
            {"tags": [{"slug": "tennis", "label": "Tennis"}, {"slug": "vienna", "label": "Vienna"}]}
        )
        == "Vienna"
    )
    assert tournament_hint({"tags": [{"slug": "atp", "label": "ATP"}]}) is None


# ---------------------------------------------------------------- names and identity


def test_name_normalization_and_matching():
    assert normalize_text("Joao Lucas Da Silva") == normalize_text("João Lucas da Silva")
    assert names_match("Yun-Seong Chung", "Yun Seong Chung")
    assert names_match("Sinner Jannik", "Jannik Sinner")
    assert names_match("J. Sinner", "Jannik Sinner")
    assert names_match("Stan Wawrinka", "Stanislas Wawrinka") is True  # initials compatible
    assert not names_match("Jannik Sinner", "Carlos Alcaraz")


def test_shared_surname_ambiguity_rejected():
    from topspin.models.market import Participant

    mi = parse_market_like("Alexander Zverev", "Daniil Medvedev")
    cands = [
        ExternalMatch("p", "1", "Mischa Zverev", "Daniil Medvedev", "a", "b", "2026-10-30", True),
    ]
    with pytest.raises(MappingRejected):
        link_by_names(mi, "2026-10-30", cands)
    assert isinstance(mi.long, Participant)


def parse_market_like(long_name: str, short_name: str):
    from topspin.models.market import MarketInfo, Participant

    return MarketInfo(
        slug="s",
        market_id="1",
        event_id="1",
        event_slug="e",
        kind=MarketKind.MATCH_WINNER,
        tour="ATP",
        long=Participant(team_id="1", name=long_name),
        short=Participant(team_id="2", name=short_name),
        scheduled_start_ns=None,
        state=MarketState.OPEN,
        active=True,
        closed=False,
        tick_size=D("0.01"),
        min_qty=D("1"),
        fee_coefficient=D("0.0695"),
    )


def test_name_linking_rules():
    mi = parse_market_like("Jannik Sinner", "Carlos Alcaraz")
    good = ExternalMatch(
        "p", "9", "Alcaraz, Carlos", "Sinner, Jannik", "c2", "c1", "2026-10-30", True
    )
    link = link_by_names(mi, "2026-10-30", [good])
    assert (
        link.long_competitor_id == "c1" and link.short_competitor_id == "c2"
    )  # reversed order handled
    one_player = ExternalMatch(
        "p", "8", "Jannik Sinner", "Taylor Fritz", "c1", "c3", "2026-10-30", True
    )
    with pytest.raises(MappingRejected):
        link_by_names(mi, "2026-10-30", [one_player])
    doubles = (
        good.__class__(**{**good.__dict__, "singles": False}) if hasattr(good, "__dict__") else None
    )
    if doubles is not None:
        with pytest.raises(MappingRejected):
            link_by_names(mi, "2026-10-30", [doubles])
    wrong_day = ExternalMatch(
        "p", "9", "Carlos Alcaraz", "Jannik Sinner", "c2", "c1", "2026-10-31", True
    )
    with pytest.raises(MappingRejected):
        link_by_names(mi, "2026-10-30", [wrong_day])
    dup = ExternalMatch("p", "10", "C. Alcaraz", "J. Sinner", "c2", "c1", "2026-10-30", True)
    with pytest.raises(MappingRejected):
        link_by_names(mi, "2026-10-30", [good, dup])


def test_singles_only_in_name_linking():
    mi = parse_market_like("Jannik Sinner", "Carlos Alcaraz")
    dbl = ExternalMatch(
        "p", "9", "Jannik Sinner", "Carlos Alcaraz", "c1", "c2", "2026-10-30", False
    )
    with pytest.raises(MappingRejected):
        link_by_names(mi, "2026-10-30", [dbl])


def test_override_file_validation(tmp_path):
    with pytest.raises(ValueError):
        OverrideFile([{"market_slug": "s"}])
    of = OverrideFile(
        [
            {
                "market_slug": "s",
                "score_provider": "p",
                "score_match_id": "1",
                "long_player": "Jannik Sinner",
                "short_player": "Carlos Alcaraz",
                "reason": "spelling",
                "added_by": "op",
            }
        ]
    )
    assert of.get(parse_market_like("Jannik Sinner", "Carlos Alcaraz")) is not None
    with pytest.raises(MappingRejected):
        of.get(parse_market_like("Taylor Fritz", "Carlos Alcaraz"))


# ---------------------------------------------------------------- tournaments


@pytest.fixture(scope="module")
def classifier():
    return TournamentClassifier.from_file(ROOT / "config" / "tournaments.yaml")


@pytest.mark.parametrize(
    ("tour", "name", "supported", "best_of"),
    [
        ("WTA", "WTA Tokyo, Japan Women Singles", True, 3),
        ("ATP", "ATP Vienna, Austria Men Singles", True, 3),
        ("ATP", "ATP Challenger Seoul, Korea Republic Men Singles", False, None),
        ("WTA", "WTA 125K Queretaro, Mexico Women Singles", False, None),
        ("ATP", "ITF Men Monastir Singles", False, None),
        ("ATP", "ATP Vienna, Austria Men Doubles", False, None),
        ("ATP", "Wimbledon Men Singles", True, 5),
        ("WTA", "Australian Open Women Singles", True, 3),
        ("ATP", "Australian Open Women Singles", False, None),
        ("ATP", None, False, None),
    ],
)
def test_tournament_classification(classifier, tour, name, supported, best_of):
    c = classifier.classify(tour, name, "Round of 16")
    assert c.supported is supported
    if supported:
        assert c.ruleset is not None and c.ruleset.best_of == best_of


def test_grand_slam_rulesets_have_ten_point_decider(classifier):
    c = classifier.classify("ATP", "US Open Men Singles")
    assert c.grand_slam and c.ruleset.final_set.tiebreak_points == 10
    assert (
        classifier.classify(
            "WTA", "WTA Tokyo, Japan Women Singles", "Qualification Round 1"
        ).supported
        is False
    )
