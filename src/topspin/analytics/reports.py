"""Research reports and promotion-gate evidence.

Gate evidence is computed from data, never typed in by hand. Gate 5 (human approval) is
never satisfied by code.
"""

from __future__ import annotations

import json
from collections import Counter
from itertools import pairwise
from pathlib import Path
from typing import Any

from topspin.analytics.latency import MAX_CACHED_BOOK_SHARE, latency_study
from topspin.analytics.pnl import execution_summary, model_quality, pnl_summary

GATE3_MIN_MATCHES = 50
GATE4_MIN_MATCHES = 30
GATE4_MIN_TOURNAMENTS = 3


def _largest_gaps(records: list[dict[str, Any]], top: int = 5) -> list[float]:
    """Longest silences between consecutive records (recording outages)."""
    ts = [r["local_ts_ns"] for r in records]
    gaps = sorted(((b - a) / 1e9 for a, b in pairwise(ts)), reverse=True)
    return [round(g, 1) for g in gaps[:top]]


def data_quality(records: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [r["payload"] for r in records if r["kind"] == "score"]
    caps: Counter[str] = Counter()
    flags: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    tournaments = set()
    for s in scores:
        for k, v in (s.get("capabilities") or {}).items():
            if v:
                caps[k] += 1
        flags.update(s.get("quality_flags") or [])
        statuses[s.get("status")] += 1
        if s.get("tournament_name"):
            tournaments.add(s["tournament_name"])
    live = [s for s in scores if s.get("status") == "live"]
    events = Counter(r["payload"]["kind"] for r in records if r["kind"] == "tennis_event")
    n = len(scores)
    nl = len(live)
    return {
        "markets_discovered": len(
            {r["payload"]["slug"] for r in records if r["kind"] == "market_info"}
        ),
        "mappings_ok": sum(1 for r in records if r["kind"] == "mapping"),
        "mappings_rejected": sum(1 for r in records if r["kind"] == "mapping_rejected"),
        "classifications": Counter(
            r["payload"]["reason"] for r in records if r["kind"] == "classification"
        ),
        "book_snapshots": sum(1 for r in records if r["kind"] == "book"),
        "book_gaps": sum(1 for r in records if r["kind"] == "feed_gap"),
        "book_invalid": sum(1 for r in records if r["kind"] == "book_invalid"),
        "score_snapshots": n,
        "live_score_snapshots": nl,
        "score_capability_rates_live": {
            k: (sum(1 for s in live if (s.get("capabilities") or {}).get(k)) / nl if nl else None)
            for k in ("sets", "games", "points", "server", "status", "source_timestamp")
        },
        "score_quality_flags": dict(flags.most_common()),
        "score_statuses": dict(statuses),
        "tournaments_seen": sorted(tournaments),
        "tennis_events": dict(events),
        "score_invalid": sum(1 for r in records if r["kind"] == "score_invalid"),
        "score_out_of_order": sum(1 for r in records if r["kind"] == "score_out_of_order"),
        "anchors": Counter(
            (r["payload"].get("error") or "ok") for r in records if r["kind"] == "anchor"
        ),
        "calibrations": Counter(
            ("ok" if not r["payload"].get("error") else "failed")
            for r in records
            if r["kind"] == "calibration"
        ),
        "health_errors": sum(1 for r in records if r["kind"] == "health"),
        "suspensions": [
            r["payload"].get("gap_s")
            for r in records
            if r["kind"] == "health" and r["payload"].get("event") == "suspension_detected"
        ],
        "largest_recording_gaps_s": _largest_gaps(records),
    }


def gate_evidence(records: list[dict[str, Any]], bankroll: float, *, source: str) -> dict[str, Any]:
    dq = data_quality(records)
    lat = latency_study(records)
    pnl = pnl_summary(records, bankroll)
    rates = dq["score_capability_rates_live"]
    gate1_checks = {
        "real_markets_discovered": dq["markets_discovered"] > 0,
        "score_to_market_mapping": dq["mappings_ok"] > 0 and dq["mappings_rejected"] == 0,
        "order_books_captured": dq["book_snapshots"] > 0,
        "timestamps_persisted": (rates.get("source_timestamp") or 0) > 0.9,
        "latency_measured": lat["score_feed_delay_ms"]["n"] > 0,
        "breaks_and_sets_detected": dq["tennis_events"].get("break", 0) > 0
        and dq["tennis_events"].get("set_complete", 0) > 0,
        "uncached_market_data": lat["cached_book_share"] <= MAX_CACHED_BOOK_SHARE,
        "latency_verdict_not_negative": not lat["verdict"].startswith(
            ("not_viable", "insufficient_data", "invalid_source")
        ),
    }
    mc = pnl["match_clustered_mean"]
    clustered_ci_positive = mc.get("ci_low") is not None and mc["ci_low"] > 0
    matches = mc.get("clusters") or 0
    tournaments = len(dq["tournaments_seen"])
    gate3 = {
        "source": source,
        "min_matches": matches >= GATE3_MIN_MATCHES,
        "clustered_ci_lower_bound_positive": clustered_ci_positive,
        "note": "must be a replay over a later sample than threshold design; stress tests required separately",
    }
    gate4 = {
        "source": source,
        "min_matches": matches >= GATE4_MIN_MATCHES,
        "min_tournaments": tournaments >= GATE4_MIN_TOURNAMENTS,
        "clustered_ci_lower_bound_positive": clustered_ci_positive,
    }
    return {
        "gate1_data_quality": {"passed": all(gate1_checks.values()), "checks": gate1_checks},
        "gate3_replay_profitability": {
            "passed": False
            if source != "replay"
            else all(v for k, v in gate3.items() if isinstance(v, bool)),
            "checks": gate3,
        },
        "gate4_forward_paper": {
            "passed": False
            if source != "paper"
            else all(v for k, v in gate4.items() if isinstance(v, bool)),
            "checks": gate4,
        },
        "gate5_human_approval": {"passed": False, "note": "never set by software"},
        "latency_verdict": lat["verdict"],
    }


def build_report(records: list[dict[str, Any]], bankroll: float, *, source: str) -> dict[str, Any]:
    return {
        "source": source,
        "records": len(records),
        "data_quality": data_quality(records),
        "latency_study": {k: v for k, v in latency_study(records).items() if k != "events"},
        "execution": execution_summary(records),
        "pnl": pnl_summary(records, bankroll),
        "model_quality": model_quality(records),
        "gates": gate_evidence(records, bankroll, source=source),
    }


def render_markdown(report: dict[str, Any]) -> str:
    dq, lat, ex, pnl, gates = (
        report[k] for k in ("data_quality", "latency_study", "execution", "pnl", "gates")
    )
    lines = [
        f"# TopSpin report ({report['source']})",
        "",
        f"Records analysed: {report['records']}",
        "",
        "## Is it making money under realistic execution?",
        "",
        f"* Closed round trips: {pnl['round_trips_closed']} (open: {pnl['round_trips_open']})",
        f"* Net realized P&L: {pnl['net_realized_pnl']:.2f} USD "
        f"(gross {pnl['gross_realized_pnl']:.2f}, fees {pnl['total_fees']:.2f})",
        f"* Match-clustered mean P&L per match: {pnl['match_clustered_mean']}",
        f"* Max drawdown: {pnl['max_drawdown']:.2f}; worst trade: {pnl['worst_trade']}; "
        f"profit factor: {pnl['profit_factor']}",
        f"* Markouts (executable): {pnl['markouts']}",
        "",
        "## Signals and execution",
        "",
        f"* Decisions: {ex['decisions']}, triggered signals: {ex['signals']}, skips: {ex['skipped']}",
        f"* Skip reasons: {ex['skip_reasons']}",
        f"* Orders: {ex['orders_submitted']} submitted, {ex['fully_filled']} full, {ex['partially_filled']} partial, "
        f"{ex['unfilled']} unfilled, {ex['rejected']} rejected (fill rate {ex['fill_rate']})",
        "",
        "## Latency study (Gate 1)",
        "",
        f"* Verdict: **{lat['verdict']}**",
        f"* Break/set events: {lat['break_and_set']}",
        f"* Share of books from the cached public route: {lat['cached_book_share']:.0%}; "
        f"reported cache age (s): {lat['book_cache_age_s']}",
        f"* Score feed delay vs provider timestamp (ms): {lat['score_feed_delay_ms']}; "
        f"net of cache age: {lat['score_feed_delay_net_of_cache_ms']}",
        f"* Book request RTT (ms): {lat['book_request_rtt_ms']}",
        "",
        "## Data quality",
        "",
        f"* Markets: {dq['markets_discovered']}, mappings ok/rejected: {dq['mappings_ok']}/{dq['mappings_rejected']}",
        f"* Books: {dq['book_snapshots']} (gaps {dq['book_gaps']}, invalid {dq['book_invalid']})",
        f"* Scores: {dq['score_snapshots']} ({dq['live_score_snapshots']} live); "
        f"capability rates (live): {dq['score_capability_rates_live']}",
        f"* Tennis events: {dq['tennis_events']}",
        f"* Anchors: {dict(dq['anchors'])}; calibrations: {dict(dq['calibrations'])}",
        f"* Tournaments: {dq['tournaments_seen']}",
        f"* Largest recording gaps (s): {dq['largest_recording_gaps_s']}; "
        f"detected suspensions: {dq['suspensions']}",
        "",
        "## Promotion gates",
        "",
    ]
    for name, g in gates.items():
        if isinstance(g, dict):
            lines.append(
                f"* {name}: {'PASSED' if g.get('passed') else 'not passed'} - {g.get('checks', g.get('note'))}"
            )
    lines.append("")
    return "\n".join(lines)


def write_report(report: dict[str, Any], out_dir: str | Path, stem: str) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    jp = out / f"{stem}.json"
    mp = out / f"{stem}.md"
    jp.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    mp.write_text(render_markdown(report), encoding="utf-8")
    return jp, mp
