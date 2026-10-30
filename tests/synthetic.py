"""Synthetic (clearly fake) match data for engine and replay tests. Never used as research data."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from topspin.models.market import MarketInfo, MarketKind, MarketState, Participant
from topspin.models.match import FeedCapabilities, MatchStatus, ScoreSnapshot

S = 1_000_000_000
T0 = 1_790_000_000 * S  # 2026-09 epoch-ish, arbitrary
A_ID, B_ID = "100", "200"
SLUG = "aec-atp-synthetic-a-b"
EVENT = "atp-synthetic-a-b"
TOURNAMENT = "ATP Vienna, Austria Men Singles"
POINT = {0: "0", 1: "15", 2: "30", 3: "40", 4: "A"}


def market(min_qty: str = "1", **kw: Any) -> MarketInfo:
    base = dict(
        slug=SLUG,
        market_id="1",
        event_id="1",
        event_slug=EVENT,
        kind=MarketKind.MATCH_WINNER,
        tour="ATP",
        long=Participant(team_id=A_ID, name="Player Alpha"),
        short=Participant(team_id=B_ID, name="Player Beta"),
        scheduled_start_ns=T0 + 60 * S,
        state=MarketState.OPEN,
        active=True,
        closed=False,
        tick_size=Decimal("0.01"),
        min_qty=Decimal(min_qty),
        fee_coefficient=Decimal("0.0695"),
    )
    base.update(kw)
    return MarketInfo(**base)  # type: ignore[arg-type]


def score(
    t: int,
    status: MatchStatus,
    sets: list[tuple[int, int]] | None = None,
    points: tuple[int, int] | None = (0, 0),
    server: str | None = "A",
    tournament: str | None = TOURNAMENT,
    tb_points: bool = False,
) -> ScoreSnapshot:
    rows = [{A_ID: a, B_ID: b} for a, b in (sets or [])]
    pts = None
    if points is not None and status == MatchStatus.LIVE:
        if tb_points:
            pts = {A_ID: str(points[0]), B_ID: str(points[1])}
        else:
            pts = {A_ID: POINT[points[0]], B_ID: POINT[points[1]]}
    srv = None if server is None else (A_ID if server == "A" else B_ID)
    return ScoreSnapshot(
        match_key=EVENT,
        provider="synthetic",
        provider_event_id="sr:1",
        status=status,
        period="NS"
        if status == MatchStatus.NOT_STARTED
        else (f"S{max(1, len(rows))}" if rows else "S1"),
        competitor_ids=(A_ID, B_ID),
        sets=rows,
        points=pts,
        server_id=srv if status == MatchStatus.LIVE else None,
        source_ts_ns=t - S // 2,
        local_ts_ns=t,
        tournament_name=tournament if status != MatchStatus.NOT_STARTED else None,
        round_name="Round of 16",
        capabilities=FeedCapabilities(
            sets=bool(rows),
            games=bool(rows),
            points=pts is not None,
            server=srv is not None,
            status=True,
            source_timestamp=True,
        ),
    )


def book(
    t: int,
    bid: str,
    ask: str,
    qty: str = "500",
    depth_levels: int = 3,
    state: MarketState = MarketState.OPEN,
) -> dict[str, Any]:
    b, a = Decimal(bid), Decimal(ask)
    tick = Decimal("0.01")
    bids = [[str(b - i * tick), qty] for i in range(depth_levels)]
    asks = [[str(a + i * tick), qty] for i in range(depth_levels)]
    return {
        "market_slug": SLUG,
        "bids": bids,
        "asks": asks,
        "state": state.value,
        "transact_time_ns": t,
        "local_ts_ns": t,
        "source": "synthetic",
    }


def rec(kind: str, payload: dict[str, Any], t: int) -> dict[str, Any]:
    return {"kind": kind, "payload": payload, "local_ts_ns": t, "source_ts_ns": None}


def play(
    games: str, first_server: str = "A", best_of: int = 3
) -> list[tuple[list[tuple[int, int]], str, bool]]:
    """Expand a string of game winners ("AABAB...") into successive score states.

    Returns (sets incl. current, server of next game, match_over) after each game, using
    correct serve rotation (no tiebreaks in these scenarios)."""
    out = []
    sets: list[tuple[int, int]] = []
    ga = gb = 0
    server = first_server
    need = (best_of + 1) // 2
    for w in games:
        ga, gb = (ga + 1, gb) if w == "A" else (ga, gb + 1)
        server = "B" if server == "A" else "A"
        done = (ga >= 6 or gb >= 6) and abs(ga - gb) >= 2
        if done:
            sets.append((ga, gb))
            ga = gb = 0
        sa = sum(1 for a, b in sets if a > b)
        sb = len(sets) - sa
        over = sa >= need or sb >= need
        out.append(([*sets] if over else [*sets, (ga, gb)], server, over))
    return out


# Set 1 (A serves first): A breaks in game 2, all other games held -> 6-3.
# Set 2 (B serves first): holds to 4-4, A breaks, A serves it out -> 6-4.
SET1 = "AAABABABA"
SET2 = "BABABABAAA"


def scenario_lagging_market(
    reprice_before_score: bool = False, lag_s: int = 5, games: str = SET1 + SET2
) -> list[dict[str, Any]]:
    """A at 0.595 prematch. A breaks in game 2 of set 1 and goes on to win.

    Default: the book lags our score feed by ``lag_s`` seconds after the break. With
    ``reprice_before_score`` the market moves ``lag_s`` seconds *before* our feed reports
    the break (the realistic adverse case, where no entry should be possible)."""
    out: list[dict[str, Any]] = []
    t = T0
    out.append(rec("market_info", market().model_dump(mode="json"), t))
    t += S
    out.append(
        rec("score", score(t, MatchStatus.NOT_STARTED, None, None, None).model_dump(mode="json"), t)
    )
    for k in range(1, 4):
        out.append(rec("book", book(t + k * S, "0.59", "0.60"), t + k * S))
    t += 4 * S
    # A later "not started" observation confirms those books were prematch.
    out.append(
        rec("score", score(t, MatchStatus.NOT_STARTED, None, None, None).model_dump(mode="json"), t)
    )
    t += 40 * S  # first serve comfortably after the anchor (anchor_min_lead_s)
    out.append(
        rec("score", score(t, MatchStatus.LIVE, [(0, 0)], (0, 0), "A").model_dump(mode="json"), t)
    )
    states = play(games, "A")
    game_dt = 30
    break_t = t + 2 * game_dt * S  # receipt time of the score showing the game-2 break
    move_t = break_t - lag_s * S if reprice_before_score else break_t + lag_s * S
    end_t = t + len(states) * game_dt * S

    def mid_at(tt: int) -> tuple[str, str]:
        if tt < move_t:
            return "0.59", "0.60"
        return "0.73", "0.74"

    score_times = {t + (i + 1) * game_dt * S: st for i, st in enumerate(states)}
    tt = t + S
    while tt <= end_t:
        if tt in score_times:
            sets, server, over = score_times[tt]
            status = MatchStatus.FINISHED if over else MatchStatus.LIVE
            snap = score(tt, status, sets, None if over else (0, 0), None if over else server)
            out.append(rec("score", snap.model_dump(mode="json"), tt))
        if tt + S // 2 <= end_t:
            bid, ask = mid_at(tt + S // 2)
            out.append(rec("book", book(tt + S // 2, bid, ask), tt + S // 2))
            out.append(rec("tick", {}, tt + S // 2 + 1))
        tt += S
    return out
