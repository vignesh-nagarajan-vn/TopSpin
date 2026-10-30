"""End-to-end engine behaviour through deterministic replay (synthetic inputs only)."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest

from tests.synthetic import SET1, SET2, T0, S, book, market, rec, scenario_lagging_market, score
from topspin.analytics.latency import latency_study
from topspin.analytics.pnl import execution_summary, pnl_summary
from topspin.analytics.reports import build_report, render_markdown
from topspin.config import AppConfig
from topspin.discovery.tournaments import TournamentClassifier
from topspin.models.match import MatchStatus
from topspin.simulation.replay import replay_files, replay_records
from topspin.storage.journal import JournalWriter, read_journal

ROOT = Path(__file__).resolve().parents[2]
CLS = TournamentClassifier.from_file(ROOT / "config" / "tournaments.yaml")


def cfg(**over):
    return AppConfig.model_validate(over)


def decisions(res, action=None):
    return [d for d in res.decisions() if action is None or d["action"] == action]


def test_lagging_market_produces_entry_fill_and_settlement():
    res = replay_records(cfg(), scenario_lagging_market(), CLS)
    buys = decisions(res, "BUY")
    assert (
        len(buys) == 1 and buys[0]["reason"] == "ok" and buys[0]["numbers"]["event_kind"] == "break"
    )
    fills = res.journal.of_kind("fill")
    assert len(fills) == 1 and fills[0]["payload"]["fill"]["intent"] == "ORDER_INTENT_BUY_LONG"
    led = res.engine.ledger
    assert led.open_positions() == [] and led.settled_pnl > 0
    # Accounting identity: cash change == settled P&L (all fees inside).
    assert led.cash - led.starting_cash == led.realized_pnl
    # Fill happened after the configured delay, never at decision time.
    assert fills[0]["local_ts_ns"] - buys[0]["local_ts_ns"] >= 500_000_000


def test_market_repricing_first_means_no_break_entry():
    res = replay_records(cfg(), scenario_lagging_market(reprice_before_score=True), CLS)
    skips = [d for d in decisions(res, "SKIP") if d.get("numbers", {}).get("event_kind") == "break"]
    assert skips and skips[0]["reason"] == "market_already_repriced"
    assert not any(d["numbers"].get("event_kind") == "break" for d in decisions(res, "BUY"))


def test_replay_is_deterministic():
    a = replay_records(cfg(), scenario_lagging_market(), CLS)
    b = replay_records(cfg(), scenario_lagging_market(), CLS)
    assert a.fingerprint() == b.fingerprint()
    assert a.decisions() == b.decisions()


def test_replay_from_journal_files_matches_in_memory(tmp_path):
    recs = scenario_lagging_market()
    w = JournalWriter(tmp_path, "t")
    # Write in a shuffled-file layout: two files, interleaved by time.
    w2 = JournalWriter(tmp_path, "u")
    for i, r in enumerate(recs):
        (w if i % 2 else w2).write(r["kind"], r["payload"], r["local_ts_ns"])
    w.close()
    w2.close()
    files = sorted(tmp_path.glob("*.jsonl"))
    times = [r["local_ts_ns"] for r in read_journal(files)]
    assert times == sorted(times)
    from_files = replay_files(cfg(), files, CLS)
    in_mem = replay_records(cfg(), recs, CLS)
    assert from_files.fingerprint() == in_mem.fingerprint()


def test_no_lookahead_future_book_does_not_change_past_decision():
    base = scenario_lagging_market()
    res_base = replay_records(cfg(), base, CLS)
    first_buy = decisions(res_base, "BUY")[0]
    cut = first_buy["local_ts_ns"]
    # Mutate every book strictly after the decision; the decision itself must not change.
    mutated = [
        (
            rec("book", book(r["local_ts_ns"], "0.20", "0.21"), r["local_ts_ns"])
            if r["kind"] == "book" and r["local_ts_ns"] > cut + 2 * S
            else r
        )
        for r in base
    ]
    res_mut = replay_records(cfg(), mutated, CLS)
    assert decisions(res_mut, "BUY")[0] == first_buy


def test_out_of_order_records_are_refused():
    eng = replay_records(cfg(), [], CLS).engine
    eng.handle("tick", {}, 10)
    with pytest.raises(ValueError):
        eng.handle("tick", {}, 5)


def test_slower_execution_and_lower_fills_hurt_not_help():
    base = replay_records(cfg(), scenario_lagging_market(), CLS)
    slow = replay_records(cfg(simulation={"taker_delay_ms": 7000}), scenario_lagging_market(), CLS)
    # With 7 s of latency the order arrives after the market repriced: no fill.
    assert base.journal.of_kind("fill") and not [
        f for f in slow.journal.of_kind("fill") if f["payload"]["fill"]["price"] == "0.60"
    ]
    thin = replay_records(
        cfg(simulation={"paper_fill_fraction": 0.01}), scenario_lagging_market(), CLS
    )
    q_base = sum(D(f["payload"]["fill"]["qty"]) for f in base.journal.of_kind("fill"))
    q_thin = sum(D(f["payload"]["fill"]["qty"]) for f in thin.journal.of_kind("fill"))
    assert q_thin < q_base


def test_first_seen_in_play_is_never_anchored():
    recs = [
        r
        for r in scenario_lagging_market()
        if not (r["kind"] == "score" and r["payload"]["status"] == "not_started")
    ]
    res = replay_records(cfg(), recs, CLS)
    anchors = res.journal.of_kind("anchor")
    assert anchors and anchors[0]["payload"]["error"] == "first_observed_after_start"
    assert not decisions(res, "BUY")
    assert {d["reason"] for d in decisions(res, "SKIP") if d.get("trigger")} <= {
        "no_anchor",
        "match_not_live",
        "near_certain_outcome",
    }


def test_unsupported_tournament_never_trades():
    recs = []
    for r in scenario_lagging_market():
        if r["kind"] == "score" and r["payload"].get("tournament_name"):
            r = {
                **r,
                "payload": {
                    **r["payload"],
                    "tournament_name": "ATP Challenger Somewhere Men Singles",
                },
            }
        recs.append(r)
    res = replay_records(cfg(), recs, CLS)
    assert not decisions(res, "BUY") and not res.journal.of_kind("order_submitted")
    cls = res.journal.of_kind("classification")
    assert (
        cls
        and cls[0]["payload"]["supported"] is False
        and "challenger" in cls[0]["payload"]["reason"]
    )


def test_suspension_cancels_and_blocks_entries():
    recs = scenario_lagging_market()
    # Suspend the match right after the break is received (before the order can arrive).
    brk_t = next(
        r["local_ts_ns"]
        for r in recs
        if r["kind"] == "score" and r["payload"]["sets"] == [{"100": 2, "200": 0}]
    )
    susp = score(brk_t + S // 4, MatchStatus.SUSPENDED, [(2, 0)], None, None)
    recs = sorted(
        [*recs, rec("score", susp.model_dump(mode="json"), brk_t + S // 4)],
        key=lambda r: r["local_ts_ns"],
    )
    res = replay_records(cfg(), recs, CLS)
    # A cancel is requested immediately; it cannot beat an order already arriving
    # (cancel latency > remaining arrival time), so a fill may still occur - realistically.
    assert any(d["action"] == "CANCEL_ORDERS" for d in res.decisions())
    later_buys = [d for d in decisions(res, "BUY") if d["local_ts_ns"] > brk_t]
    assert later_buys == []


def test_daily_loss_latch_blocks_new_entries():
    recs = scenario_lagging_market()
    eng_res = replay_records(cfg(), [], CLS)
    eng_res.engine.latch.trip("daily_loss_limit", 0)
    for r in recs:
        eng_res.engine.handle(r["kind"], r["payload"], r["local_ts_ns"])
    assert not eng_res.journal.of_kind("fill")


def test_record_mode_makes_no_decisions():
    from topspin.orchestration.engine import TradingEngine
    from topspin.risk.circuit_breaker import HaltLatch
    from topspin.storage.journal import MemoryJournal

    j = MemoryJournal()
    eng = TradingEngine(cfg(), j.write, CLS, mode="record", latch=HaltLatch(None))
    assert eng.broker is None
    for r in scenario_lagging_market():
        eng.handle(r["kind"], r["payload"], r["local_ts_ns"])
    assert not j.of_kind("decision") and not j.of_kind("order_submitted")
    assert j.of_kind("tennis_event")


def test_reports_and_analytics_on_replay_output():
    res = replay_records(cfg(), scenario_lagging_market(), CLS)
    recs = res.journal.records
    pnl = pnl_summary(recs, 1000.0)
    assert pnl["round_trips_closed"] == 1 and pnl["net_realized_pnl"] == pytest.approx(
        float(res.engine.ledger.realized_pnl)
    )
    assert pnl["match_clustered_mean"]["clusters"] == 1
    ex = execution_summary(recs)
    assert ex["orders_submitted"] == 1 and ex["fully_filled"] == 1 and ex["fill_rate"] == 1.0
    lat = latency_study(recs)
    assert lat["verdict"].startswith("insufficient_data")
    rep = build_report(recs, 1000.0, source="replay")
    assert rep["gates"]["gate5_human_approval"]["passed"] is False
    assert (
        rep["gates"]["gate3_replay_profitability"]["passed"] is False
    )  # one match is nowhere near enough
    assert "Latency study" in render_markdown(rep)


def test_latency_study_detects_market_leading_the_feed():
    lead = replay_records(
        cfg(), scenario_lagging_market(reprice_before_score=True), CLS
    ).journal.records
    lag = replay_records(cfg(), scenario_lagging_market(), CLS).journal.records
    ls_lead = latency_study(lead, window_s=30)
    ls_lag = latency_study(lag, window_s=30)
    brk_lead = next(e for e in ls_lead["events"] if e["kind"] == "break" and "pre_share" in e)
    brk_lag = next(e for e in ls_lag["events"] if e["kind"] == "break" and "pre_share" in e)
    assert brk_lead["pre_share"] == pytest.approx(1.0) and brk_lag["pre_share"] == pytest.approx(
        0.0
    )


def test_synthetic_scenario_is_internally_consistent():
    assert len(SET1) == 9 and len(SET2) == 10
    assert market().min_qty == D(1) and T0 > 0


def test_truncated_final_journal_line_is_tolerated_but_corruption_is_not(tmp_path):
    w = JournalWriter(tmp_path, "t")
    for r in scenario_lagging_market()[:5]:
        w.write(r["kind"], r["payload"], r["local_ts_ns"])
    w.close()
    f = next(tmp_path.glob("*.jsonl"))
    f.write_text(
        f.read_text(encoding="utf-8") + '{"v": 1, "seq": 99, "kind": "bo', encoding="utf-8"
    )
    assert len(list(read_journal([f]))) == 5
    lines = f.read_text(encoding="utf-8").splitlines()
    lines.insert(1, "{garbage")
    f.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(ValueError):
        list(read_journal([f]))


def test_anchor_must_predate_first_live_observation_by_margin():
    """A book captured just before the first in-play snapshot may already be in-play if
    the provider's "not started" lags the first serve: it must not become the anchor."""
    recs = scenario_lagging_market()
    live_t = next(
        r["local_ts_ns"] for r in recs if r["kind"] == "score" and r["payload"]["status"] == "live"
    )
    late_book = rec("book", book(live_t - 5 * S, "0.70", "0.71"), live_t - 5 * S)
    late_ns = score(live_t - 4 * S, MatchStatus.NOT_STARTED, None, None, None)
    recs = sorted(
        [*recs, late_book, rec("score", late_ns.model_dump(mode="json"), live_t - 4 * S)],
        key=lambda r: r["local_ts_ns"],
    )
    res = replay_records(cfg(), recs, CLS)
    anchor = res.journal.of_kind("anchor")[0]["payload"]
    assert anchor["error"] is None and anchor["prob"] == pytest.approx(0.595)


def test_sensitivity_grid_degrades_with_harsher_execution():
    from topspin.analytics.sensitivity import sensitivity_grid

    rows = sensitivity_grid(
        cfg(),
        scenario_lagging_market(),
        CLS,
        {
            "taker_delay_ms": (500, 8000),
            "paper_fill_fraction": (0.5,),
            "slippage_stress_ticks": (0,),
        },
    )
    fast, slow = rows
    assert fast["net_pnl"] > 0 and slow["net_pnl"] <= fast["net_pnl"]


def test_latency_study_never_bridges_recording_gaps():
    from topspin.analytics.pnl import BookIndex

    recs = [rec("book", book(T0, "0.40", "0.42"), T0)]
    idx = BookIndex(recs, max_age_s=10)
    assert idx.mid("aec-atp-synthetic-a-b", T0 + 5 * S) == pytest.approx(0.41)
    assert idx.mid("aec-atp-synthetic-a-b", T0 + 3600 * S) is None


def test_feed_delay_counts_each_provider_update_once():
    snaps = []
    for k in range(5):  # the same provider state polled five times
        s_ = score(T0 + k * S, MatchStatus.LIVE, [(1, 0)], (0, 0), "B")
        payload = s_.model_dump(mode="json")
        snaps.append(
            {"kind": "score", "payload": payload, "local_ts_ns": T0 + k * S, "source_ts_ns": T0 - S}
        )
    lat = latency_study(snaps)
    assert lat["score_feed_delay_ms"]["n"] == 1 and lat["score_feed_delay_ms"][
        "p50"
    ] == pytest.approx(1000.0)


def _with_cache_age(records, age):
    """Mark every book and score as served from a cache at ``age`` seconds."""
    out = []
    for r in records:
        if r["kind"] in ("book", "score"):
            r = {**r, "payload": {**r["payload"], "cache_age_s": age}}
        out.append(r)
    return out


def test_old_cached_data_blocks_entries_that_fresh_data_allows():
    base = scenario_lagging_market()
    assert decisions(replay_records(cfg(), _with_cache_age(base, 1.0), CLS), "BUY")
    stale = replay_records(cfg(), _with_cache_age(base, 16.0), CLS)
    assert not decisions(stale, "BUY") and not stale.journal.of_kind("order_submitted")
    reasons = {d["reason"] for d in decisions(stale, "SKIP")}
    assert "event_window_expired" in reasons
    expired = next(d for d in decisions(stale, "SKIP") if d["reason"] == "event_window_expired")
    assert expired["numbers"]["event_data_age_s"] >= 15


def test_recordings_without_cache_ages_fail_closed_on_the_public_route():
    """Journals recorded before cache ages were captured must not look fresh."""
    recs = []
    for r in scenario_lagging_market():
        if r["kind"] == "book":
            r = {**r, "payload": {**r["payload"], "source": "rest"}}
        elif r["kind"] == "score":
            r = {**r, "payload": {**r["payload"], "provider": "polymarket_us_event_state"}}
        recs.append(r)
    res = replay_records(cfg(), recs, CLS)
    assert not decisions(res, "BUY") and not res.journal.of_kind("order_submitted")
    # The same inputs with the assumption disabled (research only) still reach an entry.
    open_ = replay_records(cfg(feeds={"unknown_cache_age_s": 0}), recs, CLS)
    assert decisions(open_, "BUY")


def test_older_provider_state_from_a_cache_node_is_not_a_correction():
    recs = scenario_lagging_market()
    i, r = next(
        (i, r)
        for i, r in enumerate(recs)
        if r["kind"] == "score" and r["payload"]["sets"] == [{"100": 2, "200": 0}]
    )
    t = r["local_ts_ns"] + S // 10
    stale = score(t, MatchStatus.LIVE, [(1, 0)], (0, 0), "B").model_dump(mode="json")
    stale["source_ts_ns"] = r["payload"]["source_ts_ns"] - 30 * S
    res = replay_records(cfg(), [*recs[: i + 1], rec("score", stale, t), *recs[i + 1 :]], CLS)
    kinds = [e["payload"]["kind"] for e in res.journal.of_kind("tennis_event")]
    assert "correction" not in kinds and res.journal.of_kind("score_out_of_order")
    assert decisions(res, "BUY")


def test_unchanged_prematch_book_keeps_the_anchor_fresh():
    """A quiet market re-delivers the same book; the anchor must not go stale because of it."""
    recs = scenario_lagging_market()
    live_t = next(
        r["local_ts_ns"] for r in recs if r["kind"] == "score" and r["payload"]["status"] == "live"
    )
    t = live_t - 35 * S
    # Same content and transact time as the last prematch book, delivered again later.
    same = rec("book", {**book(T0 + 4 * S, "0.59", "0.60"), "local_ts_ns": t}, t)
    ns = score(t + S, MatchStatus.NOT_STARTED, None, None, None).model_dump(mode="json")
    merged = sorted([*recs, same, rec("score", ns, t + S)], key=lambda r: r["local_ts_ns"])
    res = replay_records(cfg(), merged, CLS)
    anchor = res.journal.of_kind("anchor")[0]["payload"]
    assert anchor["error"] is None and anchor["book_ts_ns"] == t
