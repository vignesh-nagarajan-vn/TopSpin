"""Deterministic tennis event-state machine.

Turns successive ``ScoreSnapshot`` observations (provider competitor ids) into an oriented
A/B score and discrete events: match start, holds, *converted* breaks, set completions,
tiebreak transitions, suspensions, completion, retirement/walkover, and corrections.

Rules that keep it honest:

* A break is a completed game won by the receiver. Break points are never events.
* A hold/break is only attributed when exactly one game elapsed between snapshots and the
  server of that game is known. Multi-game jumps (polling gaps) yield ``DATA_GAP``.
* The provider's server is cross-checked against serve rotation; a contradiction makes
  the server unreliable until it is consistent again.
* Any score regression is a ``CORRECTION``; the match is unreliable until
  ``stable_snapshots`` consistent observations follow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, Field

from tennis_alpha.models.match import MatchStatus, ScoreSnapshot
from tennis_alpha.pricing.markov import (
    InvalidStateError,
    Player,
    ScoreState,
    other,
    tiebreak_first_server,
    tiebreak_point_server,
    validate_state,
)
from tennis_alpha.pricing.rulesets import Ruleset

_POINT_VALUES = {"0": 0, "15": 1, "30": 2, "40": 3, "A": 4, "AD": 4}


class OrientationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OrientedScore:
    status: MatchStatus
    completed_sets: tuple[tuple[int, int], ...]
    games_a: int
    games_b: int
    points_a: int | None
    points_b: int | None
    server: Player | None
    in_tiebreak: bool
    local_ts_ns: int
    source_ts_ns: int | None
    match_over: bool
    flags: tuple[str, ...] = ()

    @property
    def sets_a(self) -> int:
        return sum(1 for a, b in self.completed_sets if a > b)

    @property
    def sets_b(self) -> int:
        return sum(1 for a, b in self.completed_sets if b > a)

    @property
    def games_played(self) -> int:
        return sum(a + b for a, b in self.completed_sets) + self.games_a + self.games_b

    @property
    def set_index(self) -> int:
        return len(self.completed_sets)

    def same_score(self, other_: OrientedScore) -> bool:
        return (
            self.completed_sets == other_.completed_sets
            and (self.games_a, self.games_b) == (other_.games_a, other_.games_b)
            and (self.points_a, self.points_b) == (other_.points_a, other_.points_b)
        )

    def to_state(self, server: Player | None = None) -> ScoreState:
        """Pricing state. Requires a known server (provider or verified rotation);
        unknown points price as the start of the current game/tiebreak."""
        server = server or self.server
        if server is None:
            raise InvalidStateError("server unknown")
        return ScoreState(
            sets_a=self.sets_a,
            sets_b=self.sets_b,
            games_a=self.games_a,
            games_b=self.games_b,
            points_a=self.points_a or 0,
            points_b=self.points_b or 0,
            server=server,
        )

    def describe(self) -> str:
        sets = " ".join(f"{a}-{b}" for a, b in self.completed_sets)
        pts = "?" if self.points_a is None else f"{self.points_a}-{self.points_b}"
        return f"[{sets}] {self.games_a}-{self.games_b} ({pts}) srv={self.server} {self.status}"


def _parse_points(tok_a: str, tok_b: str, in_tiebreak: bool) -> tuple[int, int]:
    if in_tiebreak:
        try:
            a, b = int(tok_a), int(tok_b)
        except ValueError as exc:
            raise OrientationError(f"non-numeric tiebreak points {tok_a}-{tok_b}") from exc
        return a, b
    if tok_a not in _POINT_VALUES or tok_b not in _POINT_VALUES:
        raise OrientationError(f"bad point tokens {tok_a}-{tok_b}")
    a, b = _POINT_VALUES[tok_a], _POINT_VALUES[tok_b]
    if (a == 4 and b != 3) or (b == 4 and a != 3):
        raise OrientationError(f"advantage without deuce {tok_a}-{tok_b}")
    return a, b


def orient(snap: ScoreSnapshot, a_id: str, b_id: str, ruleset: Ruleset) -> OrientedScore:
    """Map a provider snapshot to player A/B using the validated market mapping."""
    if set(snap.competitor_ids) != {a_id, b_id}:
        raise OrientationError("snapshot competitors do not match the market mapping")
    flags = list(snap.quality_flags)
    completed: list[tuple[int, int]] = []
    current: tuple[int, int] | None = None
    for i, row in enumerate(snap.sets):
        ga, gb = row[a_id], row[b_id]
        if current is not None:
            raise OrientationError("set rows after an unfinished set")
        if i >= ruleset.best_of:
            raise OrientationError(f"more than {ruleset.best_of} sets")
        rules = ruleset.set_rules(i)
        if rules.winner(ga, gb) is not None:
            completed.append((ga, gb))
        else:
            if not rules.is_valid_in_progress(ga, gb):
                raise OrientationError(f"impossible set score {ga}-{gb}")
            current = (ga, gb)
    sets_a = sum(1 for a, b in completed if a > b)
    sets_b = len(completed) - sets_a
    need = ruleset.sets_to_win
    match_over = sets_a >= need or sets_b >= need
    if match_over and current is not None:
        raise OrientationError("set in progress after the match was decided")
    if current is None:
        current = (0, 0)

    if snap.period and snap.period.startswith("S") and snap.period[1:].isdigit():
        if int(snap.period[1:]) - 1 != len(completed) and not match_over:
            flags.append("period_label_mismatch")

    in_tb = (not match_over) and ruleset.set_rules(len(completed)).is_tiebreak(*current)
    pa: int | None = None
    pb: int | None = None
    if snap.points is not None and not match_over:
        pa, pb = _parse_points(snap.points[a_id], snap.points[b_id], in_tb)

    server: Player | None = None
    if snap.server_id is not None:
        server = "A" if snap.server_id == a_id else "B"

    oriented = OrientedScore(
        status=snap.status,
        completed_sets=tuple(completed),
        games_a=current[0],
        games_b=current[1],
        points_a=pa,
        points_b=pb,
        server=server,
        in_tiebreak=in_tb,
        local_ts_ns=snap.local_ts_ns,
        source_ts_ns=snap.source_ts_ns,
        match_over=match_over,
        flags=tuple(flags),
    )
    if not match_over and server is not None:
        try:
            validate_state(oriented.to_state(), ruleset)
        except InvalidStateError as exc:
            raise OrientationError(str(exc)) from exc
    return oriented


class TennisEventKind(StrEnum):
    MATCH_START = "match_start"
    HOLD = "hold"
    BREAK = "break"
    GAME_UNATTRIBUTED = "game_unattributed"
    SET_COMPLETE = "set_complete"
    TIEBREAK_START = "tiebreak_start"
    TIEBREAK_COMPLETE = "tiebreak_complete"
    SERVER_CHANGE = "server_change"
    MATCH_COMPLETE = "match_complete"
    RETIREMENT = "retirement"
    WALKOVER = "walkover"
    CANCELLED = "cancelled"
    SUSPENSION = "suspension"
    RESUMPTION = "resumption"
    CORRECTION = "correction"
    DATA_GAP = "data_gap"
    SERVER_INCONSISTENT = "server_inconsistent"


class TennisEvent(BaseModel):
    kind: TennisEventKind
    match_key: str
    dedupe_key: str
    local_ts_ns: int
    source_ts_ns: int | None = None
    player: str | None = None  # game/set winner, breaker, or retiring player's opponent
    sets_a: int = 0
    sets_b: int = 0
    games_a: int = 0
    games_b: int = 0
    actionable: bool = False
    detail: dict[str, str] = Field(default_factory=dict)


@dataclass
class MatchEventDetector:
    match_key: str
    ruleset: Ruleset
    stable_snapshots: int = 3
    last: OrientedScore | None = None
    reliable: bool = True
    server_reliable: bool = False
    _stable_count: int = 0
    seen_keys: set[str] = field(default_factory=set)
    # Server of the game in progress as last confirmed (provider or rotation).
    _game_server: Player | None = None

    def _event(
        self, kind: TennisEventKind, cur: OrientedScore, player: str | None = None, actionable: bool = False, **detail: str
    ) -> TennisEvent:
        key = f"{self.match_key}:{kind.value}:{cur.sets_a}-{cur.sets_b}:{cur.games_a}-{cur.games_b}"
        if kind in (TennisEventKind.CORRECTION, TennisEventKind.DATA_GAP, TennisEventKind.SERVER_INCONSISTENT):
            key += f":{cur.local_ts_ns}"
        return TennisEvent(
            kind=kind,
            match_key=self.match_key,
            dedupe_key=key,
            local_ts_ns=cur.local_ts_ns,
            source_ts_ns=cur.source_ts_ns,
            player=player,
            sets_a=cur.sets_a,
            sets_b=cur.sets_b,
            games_a=cur.games_a,
            games_b=cur.games_b,
            actionable=actionable,
            detail=dict(detail),
        )

    def _emit(self, events: list[TennisEvent], ev: TennisEvent) -> None:
        if ev.dedupe_key in self.seen_keys:
            return  # duplicate provider event: never re-trigger
        self.seen_keys.add(ev.dedupe_key)
        events.append(ev)

    def _is_regression(self, prev: OrientedScore, cur: OrientedScore) -> bool:
        n = len(prev.completed_sets)
        if len(cur.completed_sets) < n:
            return True
        if cur.completed_sets[:n] != prev.completed_sets:
            return True
        if len(cur.completed_sets) == n and (cur.games_a < prev.games_a or cur.games_b < prev.games_b):
            return True
        if cur.games_played < prev.games_played:
            return True
        same_game = cur.games_played == prev.games_played and len(cur.completed_sets) == n
        if same_game and None not in (prev.points_a, cur.points_a):
            assert prev.points_a is not None and prev.points_b is not None
            assert cur.points_a is not None and cur.points_b is not None
            if not prev.in_tiebreak:
                total_prev = prev.points_a + prev.points_b
                total_cur = cur.points_a + cur.points_b
                # Deuce/advantage cycles may lower individual counts but never the total.
                if total_cur < total_prev and not (prev.points_a >= 3 and prev.points_b >= 3):
                    return True
            elif cur.points_a < prev.points_a or cur.points_b < prev.points_b:
                return True
        return False

    def _expected_server_after(self, prev: OrientedScore, game_server: Player) -> Player:
        """Server of the next game after the game served by ``game_server`` completes."""
        if prev.in_tiebreak:
            played = (prev.points_a or 0) + (prev.points_b or 0)
            first = tiebreak_first_server(game_server, played) if prev.points_a is not None else game_server
            return other(first)
        return other(game_server)

    def update(self, cur: OrientedScore) -> list[TennisEvent]:
        events: list[TennisEvent] = []
        prev = self.last

        if prev is None:
            self.last = cur
            self.server_reliable = cur.server is not None
            self._game_server = cur.server
            if cur.status == MatchStatus.LIVE and cur.games_played > 0:
                # First observation is mid-match: no attribution possible yet.
                self._emit(events, self._event(TennisEventKind.DATA_GAP, cur, reason="first_seen_in_play"))
            return events

        # ---- status transitions
        if prev.status != cur.status:
            if prev.status == MatchStatus.NOT_STARTED and cur.status == MatchStatus.LIVE:
                self._emit(events, self._event(TennisEventKind.MATCH_START, cur))
            elif cur.status == MatchStatus.SUSPENDED:
                self._emit(events, self._event(TennisEventKind.SUSPENSION, cur))
            elif prev.status == MatchStatus.SUSPENDED and cur.status == MatchStatus.LIVE:
                self._emit(events, self._event(TennisEventKind.RESUMPTION, cur))
            elif cur.status == MatchStatus.WALKOVER:
                self._emit(events, self._event(TennisEventKind.WALKOVER, cur))
            elif cur.status == MatchStatus.CANCELLED:
                self._emit(events, self._event(TennisEventKind.CANCELLED, cur))
            elif cur.status in (MatchStatus.FINISHED, MatchStatus.RETIRED):
                if cur.match_over and cur.status == MatchStatus.FINISHED:
                    winner = "A" if cur.sets_a > cur.sets_b else "B"
                    self._emit(events, self._event(TennisEventKind.MATCH_COMPLETE, cur, player=winner))
                else:
                    # Ended without a decided score: retirement / default / DQ. The
                    # official winner is unknown from the score alone.
                    self._emit(events, self._event(TennisEventKind.RETIREMENT, cur))
            elif cur.status == MatchStatus.UNKNOWN:
                self.reliable = False
                self._stable_count = 0

        # ---- regression => correction
        if self._is_regression(prev, cur):
            self._emit(
                events,
                self._event(TennisEventKind.CORRECTION, cur, before=prev.describe(), after=cur.describe()),
            )
            self.reliable = False
            self._stable_count = 0
            self.server_reliable = False
            self.last = cur
            self._game_server = cur.server
            return events

        delta = cur.games_played - prev.games_played
        if delta == 1:
            self._attribute_game(prev, cur, events)
        elif delta > 1:
            self._emit(events, self._event(TennisEventKind.DATA_GAP, cur, games_skipped=str(delta)))
            if len(cur.completed_sets) > len(prev.completed_sets):
                last_set = cur.completed_sets[-1]
                winner = "A" if last_set[0] > last_set[1] else "B"
                # Set result is certain, but the path is not: not actionable.
                self._emit(events, self._event(TennisEventKind.SET_COMPLETE, cur, player=winner, gap="true"))
            self.server_reliable = cur.server is not None and self.server_reliable
            self._game_server = cur.server
        else:
            # Same game. Outside a tiebreak the server must not change mid-game.
            if (
                cur.server is not None
                and prev.server is not None
                and cur.server != prev.server
                and not cur.in_tiebreak
            ):
                self._emit(events, self._event(TennisEventKind.SERVER_INCONSISTENT, cur, reason="changed_mid_game"))
                self.server_reliable = False
            elif cur.in_tiebreak and cur.server is not None and cur.points_a is not None:
                self._check_tiebreak_server(prev, cur, events)
            if cur.server is not None and self._game_server is None:
                self._game_server = cur.server

        if cur.in_tiebreak and not prev.in_tiebreak:
            self._emit(events, self._event(TennisEventKind.TIEBREAK_START, cur))

        # ---- reliability recovery
        if not self.reliable:
            self._stable_count += 1
            if self._stable_count >= self.stable_snapshots and cur.status != MatchStatus.UNKNOWN:
                self.reliable = True
        self.last = cur
        return events

    def _check_tiebreak_server(self, prev: OrientedScore, cur: OrientedScore, events: list[TennisEvent]) -> None:
        if prev.in_tiebreak and prev.server is not None and prev.points_a is not None:
            played_prev = (prev.points_a or 0) + (prev.points_b or 0)
            first = tiebreak_first_server(prev.server, played_prev)
            played = (cur.points_a or 0) + (cur.points_b or 0)
            if tiebreak_point_server(first, played) != cur.server:
                self._emit(events, self._event(TennisEventKind.SERVER_INCONSISTENT, cur, reason="tiebreak_rotation"))
                self.server_reliable = False

    def _attribute_game(self, prev: OrientedScore, cur: OrientedScore, events: list[TennisEvent]) -> None:
        set_done = len(cur.completed_sets) > len(prev.completed_sets)
        if set_done:
            fin_a, fin_b = cur.completed_sets[-1]
        else:
            fin_a, fin_b = cur.games_a, cur.games_b
        winner: Player = "A" if fin_a > prev.games_a else "B"
        game_server = prev.server if prev.server is not None else self._game_server
        trusted = self.reliable and self.server_reliable and game_server is not None

        if prev.in_tiebreak:
            self._emit(events, self._event(TennisEventKind.TIEBREAK_COMPLETE, cur, player=winner))
        elif game_server is None:
            self._emit(events, self._event(TennisEventKind.GAME_UNATTRIBUTED, cur, player=winner))
        elif winner == game_server:
            self._emit(events, self._event(TennisEventKind.HOLD, cur, player=winner, actionable=False))
        else:
            self._emit(
                events,
                self._event(
                    TennisEventKind.BREAK,
                    cur,
                    player=winner,
                    actionable=trusted and cur.status == MatchStatus.LIVE,
                    broken=game_server,
                ),
            )
        if set_done:
            set_winner = "A" if fin_a > fin_b else "B"
            self._emit(
                events,
                self._event(
                    TennisEventKind.SET_COMPLETE,
                    cur,
                    player=set_winner,
                    actionable=trusted and cur.status == MatchStatus.LIVE and not cur.match_over,
                    set_score=f"{fin_a}-{fin_b}",
                ),
            )

        # Serve rotation check for the new game.
        if game_server is not None and not cur.match_over:
            expected = self._expected_server_after(prev, game_server)
            if cur.server is None:
                self._game_server = expected  # derived from rotation
                self.server_reliable = self.server_reliable and prev.server is not None
            elif cur.server != expected:
                self._emit(
                    events,
                    self._event(TennisEventKind.SERVER_INCONSISTENT, cur, expected=expected, got=cur.server),
                )
                self.server_reliable = False
                self._game_server = cur.server
            else:
                self.server_reliable = True
                self._game_server = cur.server
                self._emit(events, self._event(TennisEventKind.SERVER_CHANGE, cur, player=cur.server))

    def current_server(self) -> Player | None:
        """Best verified server for the current point, or None if not trustworthy."""
        if not (self.reliable and self.server_reliable) or self.last is None:
            return None
        return self.last.server if self.last.server is not None else self._game_server
