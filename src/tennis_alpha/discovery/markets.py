"""Discover and prioritize eligible Polymarket US tennis markets."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tennis_alpha.config import AppConfig
from tennis_alpha.discovery.tournaments import TournamentClassifier
from tennis_alpha.feeds.polymarket_us import ParseError, PolymarketUSPublicClient, parse_event_score, parse_market
from tennis_alpha.models.market import MarketInfo, MarketState
from tennis_alpha.models.match import MatchStatus
from tennis_alpha.timeutil import NS_PER_S


@dataclass
class DiscoveryResult:
    selected: list[MarketInfo] = field(default_factory=list)
    candidates: list[MarketInfo] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)


def prioritize(
    markets: list[MarketInfo],
    statuses: dict[str, str],
    now_ns: int,
    cfg: AppConfig,
) -> list[MarketInfo]:
    """Upcoming matches (anchorable) first, soonest start, then tighter quoted spread and
    volume; live matches after (useful for recording / latency research only)."""

    def key(m: MarketInfo) -> tuple[int, float, float, float]:
        status = statuses.get(m.event_slug, "unknown")
        upcoming = 0 if status == MatchStatus.NOT_STARTED.value else 1
        start = (m.scheduled_start_ns or now_ns) - now_ns
        spread = float(m.best_ask - m.best_bid) if m.best_ask is not None and m.best_bid is not None else 1.0
        vol = -float(m.volume or 0)
        return (upcoming, start / NS_PER_S if upcoming == 0 else 0.0, spread, vol)

    return sorted(markets, key=key)


def select_from_events(
    events: list[dict[str, Any]],
    cfg: AppConfig,
    classifier: TournamentClassifier,
    now_ns: int,
) -> DiscoveryResult:
    res = DiscoveryResult()
    horizon = now_ns + int(cfg.discovery.lookahead_hours * 3600 * NS_PER_S)
    for ev in events:
        for mk in ev.get("markets") or []:
            smt = str(mk.get("sportsMarketType") or "")
            if not smt.startswith("tennis_"):
                continue
            try:
                mi = parse_market(ev, mk, cfg.exchange.supported_market_types, local_ts_ns=now_ns)
            except ParseError as exc:
                res.rejected.append((str(mk.get("slug")), str(exc)))
                continue
            try:
                snap = parse_event_score(ev, now_ns)
                status = snap.status
            except ParseError as exc:
                res.rejected.append((mi.slug, f"score_unparseable:{exc}"))
                continue
            if status.is_terminal:
                res.rejected.append((mi.slug, f"match_{status.value}"))
                continue
            if mi.closed or not mi.active or mi.state not in (MarketState.OPEN, MarketState.PREOPEN):
                res.rejected.append((mi.slug, f"market_state:{mi.state.value}"))
                continue
            if status == MatchStatus.NOT_STARTED and mi.scheduled_start_ns and mi.scheduled_start_ns > horizon:
                res.rejected.append((mi.slug, "outside_lookahead"))
                continue
            if mi.tournament_name:
                c = classifier.classify(mi.tour, mi.tournament_name, mi.round_name)
                if not c.supported:
                    res.rejected.append((mi.slug, f"tournament:{c.reason}"))
                    continue
            spread = (
                float(mi.best_ask - mi.best_bid) if mi.best_ask is not None and mi.best_bid is not None else None
            )
            if spread is not None and spread > cfg.discovery.max_spread_for_tracking:
                res.rejected.append((mi.slug, f"quoted_spread:{spread:.2f}"))
                continue
            res.candidates.append(mi)
            res.events[mi.event_slug] = ev
            res.statuses[mi.event_slug] = status.value
    ordered = prioritize(res.candidates, res.statuses, now_ns, cfg)
    res.selected = ordered[: cfg.discovery.max_tracked_matches]
    return res


async def discover(
    client: PolymarketUSPublicClient, cfg: AppConfig, classifier: TournamentClassifier, now_ns: int
) -> DiscoveryResult:
    events: list[dict[str, Any]] = []
    for league in cfg.exchange.leagues:
        events.extend(await client.list_events(league))
    return select_from_events(events, cfg, classifier, now_ns)
