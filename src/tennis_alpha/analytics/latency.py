"""The Gate 1 latency study: does the market reprice before our score feed delivers?

For every detected game event (hold / break / set completion) we look at the YES midpoint
around the moment the event was *received locally* (t = 0):

    pre_share = (mid(0) - mid(-W)) / (mid(+W) - mid(-W))

i.e. the fraction of the total market move over the window that had already happened
before we knew about the event. Midpoints are fine here: this is measurement, not
execution. If the median pre_share for breaks/sets is high, any strategy relying on
reacting to our score feed is trading against already-updated prices and is rejected.
"""

from __future__ import annotations

import statistics
from typing import Any

from tennis_alpha.analytics.pnl import BookIndex
from tennis_alpha.timeutil import NS_PER_S

OFFSETS_S = (-60, -30, -10, -5, 0, 5, 10, 30, 60)
MIN_MOVE = 0.01
MIN_EVENTS_FOR_VERDICT = 30


def latency_study(records: list[dict[str, Any]], window_s: int = 60) -> dict[str, Any]:
    idx = BookIndex(records)
    match_to_slug: dict[str, str] = {}
    for r in records:
        if r["kind"] == "market_info":
            match_to_slug.setdefault(r["payload"]["event_slug"], r["payload"]["slug"])
    # Score feed delay vs the provider's own updatedAt stamp.
    feed_delays = [
        (r["local_ts_ns"] - r["source_ts_ns"]) / 1e6
        for r in records
        if r["kind"] == "score" and r.get("source_ts_ns")
    ]
    book_rtts = [r["payload"]["request_latency_ms"] for r in records
                 if r["kind"] == "book" and r["payload"].get("request_latency_ms") is not None]

    rows: list[dict[str, Any]] = []
    for r in records:
        if r["kind"] != "tennis_event":
            continue
        ev = r["payload"]
        if ev["kind"] not in ("hold", "break", "set_complete"):
            continue
        slug = match_to_slug.get(ev["match_key"])
        if slug is None:
            continue
        t0 = int(ev["local_ts_ns"])
        mids = {o: idx.mid(slug, t0 + o * NS_PER_S) for o in OFFSETS_S}
        before, at, after = mids[-window_s], mids[0], mids[window_s]
        row: dict[str, Any] = {"match_key": ev["match_key"], "kind": ev["kind"], "player": ev.get("player"),
                               "ts": t0, "mids": mids}
        if None not in (before, at, after):
            assert before is not None and at is not None and after is not None
            total = after - before
            row["total_move"] = total
            row["post_move"] = after - at
            # Direction expected from the event: favours the game/set winner (A = YES).
            sign = 1.0 if ev.get("player") == "A" else -1.0
            row["post_move_signed"] = (after - at) * sign
            if abs(total) >= MIN_MOVE:
                row["pre_share"] = (at - before) / total
        rows.append(row)

    def summarize(kind_filter: tuple[str, ...]) -> dict[str, Any]:
        sel = [x for x in rows if x["kind"] in kind_filter]
        shares = [x["pre_share"] for x in sel if "pre_share" in x]
        post = [x["post_move_signed"] for x in sel if "post_move_signed" in x]
        return {
            "events": len(sel),
            "with_material_move": len(shares),
            "median_pre_share": statistics.median(shares) if shares else None,
            "frac_mostly_priced_before_receipt": (sum(1 for s in shares if s >= 0.5) / len(shares)) if shares else None,
            "mean_post_receipt_move_toward_winner": statistics.fmean(post) if post else None,
        }

    trig = summarize(("break", "set_complete"))
    n = trig["with_material_move"]
    if n < MIN_EVENTS_FOR_VERDICT:
        verdict = f"insufficient_data ({n} break/set events with material moves; need {MIN_EVENTS_FOR_VERDICT})"
    elif (trig["median_pre_share"] or 0) >= 0.8:
        verdict = "not_viable: the market usually reprices before our score feed delivers the event"
    elif (trig["mean_post_receipt_move_toward_winner"] or 0) > 0.02:
        verdict = "potentially_viable: material repricing continues after receipt; proceed to replay validation"
    else:
        verdict = "inconclusive: little exploitable post-receipt drift"
    return {
        "window_s": window_s,
        "score_feed_delay_ms": _dist(feed_delays),
        "book_request_rtt_ms": _dist(book_rtts),
        "break_and_set": trig,
        "holds": summarize(("hold",)),
        "verdict": verdict,
        "events": rows[:500],
    }


def _dist(xs: list[float]) -> dict[str, float | int | None]:
    if not xs:
        return {"n": 0, "p50": None, "p90": None, "max": None}
    s = sorted(xs)
    return {"n": len(s), "p50": s[len(s) // 2], "p90": s[int(len(s) * 0.9) - 1 if len(s) > 1 else 0], "max": s[-1]}
