"""Polymarket US public (unauthenticated) reference-data, book and event-state adapter.

This module never sends credentials and never imports the trading SDK. It is safe to use
from the recorder and the paper engine.

Verified against https://docs.polymarket.us (see docs/venue-verification.md):

* ``GET {gateway}/v1/sports``                 - sport/league slugs and their series ids
* ``GET {gateway}/v1/events?seriesId=..``     - events with embedded markets + event state
* ``GET {gateway}/v1/events/slug/{slug}``     - one event (``{"event": {...}}``)
* ``GET {gateway}/v1/markets/{slug}/book``    - full book snapshot (``{"marketData": ...}``)

Tennis event payloads embed a Sportradar-derived live state: a score string such as
``"6-3, 1-2:15-0"``, per-set ``periodScores`` keyed by competitor id, a ``period``
(``NS``/``S1``..``S5``/``SUS``/``FT``) and ``eventState.tennisState.servingTeamId``.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from tennis_alpha.models.market import (
    BookSnapshot,
    MarketInfo,
    MarketKind,
    MarketState,
    Participant,
)
from tennis_alpha.models.match import FeedCapabilities, MatchStatus, ScoreSnapshot
from tennis_alpha.timeutil import mono_ns, now_ns, parse_ts_ns

PROVIDER = "polymarket_us_event_state"

_PERIOD_STATUS: dict[str, MatchStatus] = {
    "NS": MatchStatus.NOT_STARTED,
    "S1": MatchStatus.LIVE,
    "S2": MatchStatus.LIVE,
    "S3": MatchStatus.LIVE,
    "S4": MatchStatus.LIVE,
    "S5": MatchStatus.LIVE,
    "SUS": MatchStatus.SUSPENDED,
    "INT": MatchStatus.SUSPENDED,
    "DEL": MatchStatus.SUSPENDED,
    "FT": MatchStatus.FINISHED,
    "RET": MatchStatus.RETIRED,
    "WO": MatchStatus.WALKOVER,
    "CAN": MatchStatus.CANCELLED,
    "CANC": MatchStatus.CANCELLED,
    "ABD": MatchStatus.CANCELLED,
    "POST": MatchStatus.CANCELLED,
}

_SET_TOKEN = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*(?:\((\d+)\s*-\s*(\d+)\))?\s*$")
_POINT_TOKENS = {"0", "15", "30", "40", "A", "AD"}


class ParseError(ValueError):
    pass


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _amount(obj: Any) -> Decimal | None:
    if isinstance(obj, Mapping):
        return _dec(obj.get("value"))
    return _dec(obj)


# --------------------------------------------------------------------------- score parsing


def parse_score_string(score: str) -> tuple[list[tuple[int, int, tuple[int, int] | None]], tuple[str, str] | None]:
    """Split ``"7-6(7-3), 2-1:15-0"`` into per-set games (+ tiebreak) and point tokens.

    Values are in the string's own column order, which must be mapped to competitor ids
    separately (see ``_string_order``).
    """
    sets_part, _, points_part = score.partition(":")
    sets: list[tuple[int, int, tuple[int, int] | None]] = []
    if sets_part.strip():
        for tok in sets_part.split(","):
            m = _SET_TOKEN.match(tok)
            if not m:
                raise ParseError(f"bad set token {tok!r} in {score!r}")
            tb = (int(m.group(3)), int(m.group(4))) if m.group(3) is not None else None
            sets.append((int(m.group(1)), int(m.group(2)), tb))
    points: tuple[str, str] | None = None
    if points_part.strip():
        left, sep, right = points_part.strip().partition("-")
        if not sep:
            raise ParseError(f"bad point token {points_part!r}")
        points = (left.strip().upper(), right.strip().upper())
    return sets, points


def _string_order(
    string_sets: list[tuple[int, int, tuple[int, int] | None]],
    period_sets: list[dict[str, int]],
    ids: tuple[str, str],
) -> tuple[str, str] | None:
    """Which competitor is the score string's first column? None if ambiguous."""
    fwd = rev = 0
    for (g0, g1, _), ps in zip(string_sets, period_sets, strict=False):
        a, b = ps.get(ids[0]), ps.get(ids[1])
        if a is None or b is None:
            return None
        if (g0, g1) == (a, b) and (g0, g1) != (b, a):
            fwd += 1
        elif (g0, g1) == (b, a) and (g0, g1) != (a, b):
            rev += 1
        elif (g0, g1) not in ((a, b), (b, a)):
            return None  # string and periodScores disagree
    if fwd and not rev:
        return ids
    if rev and not fwd:
        return (ids[1], ids[0])
    return None


def parse_event_score(event: Mapping[str, Any], local_ts_ns: int | None = None) -> ScoreSnapshot:
    """Normalize a Polymarket US tennis event payload into a ``ScoreSnapshot``."""
    local = local_ts_ns if local_ts_ns is not None else now_ns()
    teams = event.get("teams") or []
    if len(teams) != 2:
        raise ParseError(f"event {event.get('slug')} has {len(teams)} teams, expected 2")
    ids = (str(teams[0]["id"]), str(teams[1]["id"]))
    state = event.get("eventState") or {}
    tennis = state.get("tennisState") or {}
    flags: list[str] = []

    period = event.get("period") or state.get("period")
    ended = bool(event.get("ended") or state.get("ended"))
    status = _PERIOD_STATUS.get(str(period), MatchStatus.UNKNOWN) if period else MatchStatus.UNKNOWN
    if period and str(period) not in _PERIOD_STATUS:
        flags.append(f"unknown_period:{period}")
    if ended and status in (MatchStatus.LIVE, MatchStatus.SUSPENDED, MatchStatus.UNKNOWN):
        status = MatchStatus.FINISHED
        flags.append("ended_flag_overrides_period")

    period_sets: list[dict[str, int]] = []
    for ps in sorted(state.get("periodScores") or [], key=lambda x: int(x.get("number", 0))):
        if ps.get("type") not in (None, "PERIOD_SCORE_TYPE_REGULATION"):
            flags.append(f"period_type:{ps.get('type')}")
            continue
        row = {str(s["competitorId"]): int(s["score"]) for s in ps.get("scores") or []}
        if set(row) != set(ids):
            raise ParseError(f"periodScores competitor ids {sorted(row)} != teams {sorted(ids)}")
        period_sets.append(row)

    raw_score = event.get("score") or state.get("score")
    tiebreaks: list[dict[str, int] | None] = [None] * len(period_sets)
    points: dict[str, str] | None = None
    if raw_score:
        string_sets, pts = parse_score_string(str(raw_score))
        if period_sets and len(string_sets) != len(period_sets):
            flags.append("score_string_period_count_mismatch")
        order = _string_order(string_sets, period_sets, ids) if period_sets else None
        if order is None:
            if pts is not None or any(tb for *_, tb in string_sets):
                flags.append("score_string_order_ambiguous")
        else:
            for i, (_, _, tb) in enumerate(string_sets):
                if tb is not None and i < len(tiebreaks):
                    tiebreaks[i] = {order[0]: tb[0], order[1]: tb[1]}
            if pts is not None:
                points = {order[0]: pts[0], order[1]: pts[1]}
    if status == MatchStatus.LIVE and points is None:
        flags.append("points_unavailable")

    server_raw = tennis.get("servingTeamId")
    server_id = str(server_raw) if server_raw is not None else None
    if server_id is not None and server_id not in ids:
        flags.append("server_not_a_competitor")
        server_id = None

    caps = FeedCapabilities(
        sets=bool(period_sets),
        games=bool(period_sets),
        points=points is not None,
        server=server_id is not None,
        status=period is not None,
        source_timestamp=bool(state.get("updatedAt")),
        corrections=False,
    )
    return ScoreSnapshot(
        match_key=str(event.get("slug")),
        provider=PROVIDER,
        provider_event_id=str(event.get("sportradarGameId") or event.get("id") or ""),
        status=status,
        period=str(period) if period else None,
        competitor_ids=ids,
        sets=period_sets,
        tiebreaks=tiebreaks,
        points=points,
        server_id=server_id,
        ended=ended,
        source_ts_ns=parse_ts_ns(state.get("updatedAt")),
        local_ts_ns=local,
        mono_ns=mono_ns(),
        raw_score=str(raw_score) if raw_score else None,
        tournament_name=tennis.get("tournamentName"),
        round_name=tennis.get("round"),
        capabilities=caps,
        quality_flags=flags,
    )


# --------------------------------------------------------------------------- market parsing


def _league(event: Mapping[str, Any]) -> str | None:
    leagues = {str(t.get("league") or "").lower() for t in event.get("teams") or []}
    leagues.discard("")
    if len(leagues) == 1:
        return leagues.pop()
    for tag in event.get("tags") or []:
        lg = tag.get("league") or {}
        if lg.get("slug"):
            return str(lg["slug"]).lower()
    return None


def parse_market(
    event: Mapping[str, Any],
    market: Mapping[str, Any],
    supported: tuple[MarketKind, ...] = (MarketKind.MATCH_WINNER,),
    local_ts_ns: int = 0,
) -> MarketInfo:
    """Validate and normalize one tennis instrument. Raises ``ParseError`` to reject."""
    smt = market.get("sportsMarketType") or (market.get("metadata") or {}).get("market_sport_type")
    if not smt:
        raise ParseError("missing sportsMarketType")
    if str(smt).startswith("table_tennis"):
        raise ParseError("table tennis is not tennis")
    try:
        kind = MarketKind(str(smt))
    except ValueError as exc:
        raise ParseError(f"unsupported market type {smt!r}") from exc
    if kind not in supported:
        raise ParseError(f"market type {kind.value} not enabled")

    league = _league(event)
    if league not in ("atp", "wta"):
        raise ParseError(f"league {league!r} is not ATP/WTA")

    sides = [s for s in market.get("marketSides") or [] if s.get("teamId") is not None]
    if len(sides) != 2:
        raise ParseError(f"expected 2 participant sides, got {len(sides)}")
    longs = [s for s in sides if s.get("long") is True]
    shorts = [s for s in sides if s.get("long") is False]
    if len(longs) != 1 or len(shorts) != 1:
        raise ParseError("market sides must have exactly one long and one short participant")

    def participant(side: Mapping[str, Any]) -> Participant:
        team = side.get("team") or {}
        name = str(team.get("name") or side.get("description") or "")
        if "/" in name or " & " in name:
            raise ParseError(f"doubles-looking participant {name!r}")
        prov = {
            str(p.get("provider")): str(p.get("providerId"))
            for p in team.get("providerIds") or []
            if p.get("provider") and p.get("providerId")
        }
        return Participant(
            team_id=str(side["teamId"]), name=name, provider_ids=prov, league=team.get("league")
        )

    long_p, short_p = participant(longs[0]), participant(shorts[0])
    event_team_ids = {str(t["id"]) for t in event.get("teams") or []}
    if {long_p.team_id, short_p.team_id} != event_team_ids:
        raise ParseError("market participants do not match the event's teams")

    tick = _dec(market.get("orderPriceMinTickSize"))
    min_qty = _dec(market.get("minimumTradeQty"))
    if tick is None or tick <= 0 or min_qty is None or min_qty <= 0:
        raise ParseError("missing tick size or minimum trade quantity")

    tennis = (event.get("eventState") or {}).get("tennisState") or {}
    status = market.get("status") or market.get("ep3Status")
    return MarketInfo(
        slug=str(market["slug"]),
        market_id=str(market.get("id")),
        event_id=str(event.get("id")),
        event_slug=str(event.get("slug")),
        kind=kind,
        tour=league.upper(),
        long=long_p,
        short=short_p,
        scheduled_start_ns=parse_ts_ns(event.get("startTime") or market.get("gameStartTime")),
        state=MarketState.parse(str(status) if status else None),
        active=bool(market.get("active")),
        closed=bool(market.get("closed")),
        tick_size=tick,
        min_qty=min_qty,
        fee_coefficient=_dec(market.get("feeCoefficient")),
        best_bid=_amount(market.get("bestBidQuote")),
        best_ask=_amount(market.get("bestAskQuote")),
        volume=_dec(market.get("volume")),
        rules_text=market.get("description"),
        sportradar_event_id=event.get("sportradarGameId"),
        tournament_name=tennis.get("tournamentName"),
        round_name=tennis.get("round"),
        discovered_local_ts_ns=local_ts_ns,
    )


def parse_book(payload: Mapping[str, Any], local_ts_ns: int | None = None, source: str = "rest") -> BookSnapshot:
    md = payload.get("marketData") or payload
    if "marketSlug" not in md:
        raise ParseError("book payload has no marketSlug")

    def levels(rows: Any) -> list[tuple[str, str]]:
        out = []
        for r in rows or []:
            px = _amount(r.get("px"))
            qty = _dec(r.get("qty"))
            if px is None or qty is None:
                raise ParseError(f"bad book level {r!r}")
            out.append((str(px), str(qty)))
        return out

    stats = md.get("stats") or {}
    return BookSnapshot(
        market_slug=str(md["marketSlug"]),
        bids=levels(md.get("bids")),
        asks=levels(md.get("offers")),
        state=MarketState.parse(md.get("state")),
        transact_time_ns=parse_ts_ns(md.get("transactTime")),
        local_ts_ns=local_ts_ns if local_ts_ns is not None else now_ns(),
        mono_ns=mono_ns(),
        source=source,
        last_trade_px=str(_amount(stats.get("lastTradePx"))) if stats.get("lastTradePx") else None,
        shares_traded=stats.get("sharesTraded"),
    )


# --------------------------------------------------------------------------- HTTP client


class RateLimiter:
    """Minimum spacing between requests (simple and deterministic)."""

    def __init__(self, per_second: float) -> None:
        self.interval = 1.0 / per_second
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            if delay > 0:
                await asyncio.sleep(delay)
                now = time.monotonic()
            self._next = now + self.interval


class PolymarketUSPublicClient:
    """Async client for unauthenticated Polymarket US endpoints. Holds no credentials."""

    def __init__(
        self,
        base_url: str = "https://gateway.polymarket.us",
        *,
        timeout_s: float = 10.0,
        per_second: float = 8.0,
        max_attempts: int = 3,
        backoff_s: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_s,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "tennis-alpha-engine/0.1"},
        )
        self._limiter = RateLimiter(per_second)
        self._max_attempts = max_attempts
        self._backoff = backoff_s
        self._series_cache: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self._max_attempts):
            await self._limiter.wait()
            try:
                resp = await self._client.get(path, params=params)
            except httpx.TransportError as exc:
                last_exc = exc
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 404:
                    raise LookupError(f"{path} not found")
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"GET {path} -> {resp.status_code}: {resp.text[:200]}")
                last_exc = RuntimeError(f"GET {path} -> {resp.status_code}")
            await asyncio.sleep(self._backoff * (2**attempt))
        raise RuntimeError(f"GET {path} failed after {self._max_attempts} attempts") from last_exc

    async def series_id(self, league: str) -> str:
        if league not in self._series_cache:
            data = await self._get("/v1/sports")
            for s in data.get("sports", []):
                if s.get("series"):
                    self._series_cache[str(s["sport"]).lower()] = str(s["series"])
        if league not in self._series_cache:
            raise LookupError(f"no series for league {league!r}")
        return self._series_cache[league]

    async def list_events(self, league: str, page_size: int = 100, max_pages: int = 10) -> list[dict[str, Any]]:
        sid = await self.series_id(league)
        out: list[dict[str, Any]] = []
        for page in range(max_pages):
            data = await self._get(
                "/v1/events",
                {
                    "seriesId": sid,
                    "active": "true",
                    "closed": "false",
                    "limit": page_size,
                    "offset": page * page_size,
                },
            )
            events = data.get("events") or []
            out.extend(events)
            if len(events) < page_size:
                break
        return out

    async def get_event(self, slug: str) -> dict[str, Any]:
        data = await self._get(f"/v1/events/slug/{slug}")
        return data.get("event") or data

    async def get_book(self, slug: str) -> dict[str, Any]:
        return await self._get(f"/v1/markets/{slug}/book")

    async def get_settlement(self, slug: str) -> Decimal | None:
        """Official settlement price (YES), or None while unsettled."""
        try:
            data = await self._get(f"/v1/markets/{slug}/settlement")
        except LookupError:
            return None
        return _dec(data.get("settlement"))
