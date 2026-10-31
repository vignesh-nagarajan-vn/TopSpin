"""Collect real finished ATP/WTA tour-level matches for the $100 benchmark.

For each match this stores the last pre-match bid and ask (from the public price history)
and the official settlement. Public endpoints only; no credentials. Results are cached in
``data/benchmark_matches.json`` so reports can be rebuilt without refetching.

    python scripts/fetch_market_history.py --start 2026-09-01 --end 2026-10-30
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from topspin.discovery.tournaments import TournamentClassifier
from topspin.feeds.polymarket_us import PolymarketUSPublicClient
from topspin.timeutil import NS_PER_S, parse_ts_ns

SERIES = {"ATP": "16", "WTA": "17"}
OUT = Path("data/benchmark_matches.json")
# The anchor window: at least 5 minutes before the scheduled start, so never in play.
PREMATCH_GAP_S = 300
PREMATCH_WINDOW_S = 6 * 3600


async def list_finished(
    client: PolymarketUSPublicClient, series: str, start: str, end: str
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for page in range(30):
        data = await client._get(
            "/v1/events",
            {
                "seriesId": series,
                "startTimeMin": f"{start}T00:00:00Z",
                "startTimeMax": f"{end}T00:00:00Z",
                "ended": "true",
                "limit": 100,
                "offset": page * 100,
            },
        )
        batch = data.get("events") or []
        events.extend(batch)
        if len(batch) < 100:
            break
    return events


async def prematch_quote(
    client: PolymarketUSPublicClient, slug: str, start_s: int
) -> dict[str, Any] | None:
    data = await client._get(
        "/v1/price-history",
        {
            "symbol": slug,
            "timestamp.startTimestamp": start_s - PREMATCH_WINDOW_S,
            "timestamp.endTimestamp": start_s - PREMATCH_GAP_S,
            "fidelity": 1,
        },
    )
    rows = [
        r
        for r in data.get("history") or []
        if 0 < float(r.get("longPrice") or 0) < 1 and 0 < float(r.get("shortPrice") or 0) < 1
    ]
    if not rows:
        return None
    last = max(rows, key=lambda r: int(r["timestamp"]))
    # longPrice is derived from the best ask; shortPrice from one minus the best bid.
    return {
        "ask": float(last["longPrice"]),
        "bid": round(1.0 - float(last["shortPrice"]), 6),
        "quote_ts": int(last["timestamp"]),
    }


async def main(start: str, end: str, per_second: float) -> None:
    classifier = TournamentClassifier.from_file("config/tournaments.yaml")
    client = PolymarketUSPublicClient(per_second=per_second)
    rows: list[dict[str, Any]] = []
    rejected: dict[str, int] = {}
    try:
        for tour, series in SERIES.items():
            events = await list_finished(client, series, start, end)
            print(tour, "finished events:", len(events))
            for ev in events:
                markets = [
                    m
                    for m in ev.get("markets") or []
                    if m.get("sportsMarketType") == "tennis_match_winner"
                ]
                if not markets:
                    continue
                tennis = (ev.get("eventState") or {}).get("tennisState") or {}
                c = classifier.classify(tour, tennis.get("tournamentName"), tennis.get("round"))
                if not c.supported:
                    rejected[c.reason] = rejected.get(c.reason, 0) + 1
                    continue
                start_ns = parse_ts_ns(ev.get("startTime"))
                if start_ns is None:
                    continue
                m = markets[0]
                longs = [s for s in m.get("marketSides") or [] if s.get("long") is True]
                shorts = [s for s in m.get("marketSides") or [] if s.get("long") is False]
                quote = await prematch_quote(client, str(m["slug"]), start_ns // NS_PER_S)
                settlement = await client.get_settlement(str(m["slug"]))
                if quote is None or settlement is None:
                    rejected["no_quote_or_settlement"] = (
                        rejected.get("no_quote_or_settlement", 0) + 1
                    )
                    continue
                rows.append(
                    {
                        "tour": tour,
                        "event": ev.get("slug"),
                        "market": m["slug"],
                        "start": ev.get("startTime"),
                        "tournament": tennis.get("tournamentName"),
                        "score": ev.get("score"),
                        "long": longs[0].get("description") if longs else None,
                        "short": shorts[0].get("description") if shorts else None,
                        "fee_theta": m.get("feeCoefficient"),
                        "settlement": float(settlement),
                        **quote,
                    }
                )
    finally:
        await client.aclose()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps({"start": start, "end": end, "rejected": rejected, "matches": rows}, indent=1)
    )
    print("kept", len(rows), "rejected", rejected)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-09-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--per-second", type=float, default=4.0)
    a = ap.parse_args()
    asyncio.run(main(a.start, a.end, a.per_second))
