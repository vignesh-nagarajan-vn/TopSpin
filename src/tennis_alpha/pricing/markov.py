"""Deterministic Markov-chain pricing of tennis singles.

The hierarchy is point -> game -> tiebreak -> set -> match. Each level is solved exactly
by dynamic programming over its finite state space; the unbounded deuce / tiebreak /
advantage-set "win by two" tails are solved in closed form, so every routine
terminates and is exact up to floating-point rounding.

Conventions
-----------
* Player ``"A"`` is the player whose win pays out on the YES (long) side of the market.
* ``p_a`` is the probability that A wins a point *on A's serve*; ``p_b`` likewise for B.
* Points inside a regular game are integers: 0, 1, 2, 3 = 0, 15, 30, 40. Deuce is (3, 3)
  or any equal score >= 3; advantage is a one-point lead with both on >= 3.
* ``server`` in ``ScoreState`` is the server of the *current point*. Outside a
  tiebreak that is also the server of the current game.
* Serve order: servers alternate every game. In a tiebreak the player due to serve the
  next game serves point 1, then serve alternates every two points. The player who
  received first in a tiebreak serves the first game of the next set; otherwise the
  first server of the next set is the player who did not serve the last game.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from tennis_alpha.pricing.rulesets import Ruleset, SetRules

Player = Literal["A", "B"]


class InvalidStateError(ValueError):
    """The score state is impossible or not priceable under the ruleset."""


def other(player: Player) -> Player:
    return "B" if player == "A" else "A"


@dataclass(frozen=True, slots=True)
class ScoreState:
    sets_a: int = 0
    sets_b: int = 0
    games_a: int = 0
    games_b: int = 0
    points_a: int = 0
    points_b: int = 0
    server: Player = "A"

    def describe(self) -> str:
        return (
            f"sets {self.sets_a}-{self.sets_b} games {self.games_a}-{self.games_b} "
            f"points {self.points_a}-{self.points_b} server {self.server}"
        )


def _check_prob(name: str, p: float) -> None:
    if not (isinstance(p, float | int) and math.isfinite(p) and 0.0 < p < 1.0):
        raise InvalidStateError(f"{name}={p!r} must be a finite probability in (0, 1)")


def game_win_prob(p: float, server_points: int = 0, receiver_points: int = 0) -> float:
    """Probability the server wins a standard (advantage-scoring) game from the score."""
    _check_prob("p", p)
    q = 1.0 - p
    deuce = p * p / (p * p + q * q)
    memo: dict[tuple[int, int], float] = {}

    def g(a: int, b: int) -> float:
        if a >= 4 and a - b >= 2:
            return 1.0
        if b >= 4 and b - a >= 2:
            return 0.0
        if a >= 3 and b >= 3:
            if a == b:
                return deuce
            if a == b + 1:
                return p + q * deuce
            if b == a + 1:
                return p * deuce
            raise InvalidStateError(f"impossible game score {a}-{b}")
        key = (a, b)
        if key not in memo:
            memo[key] = p * g(a + 1, b) + q * g(a, b + 1)
        return memo[key]

    return g(server_points, receiver_points)


def tiebreak_point_server(first_server: Player, points_played: int) -> Player:
    """Server of the point after ``points_played`` points of a tiebreak."""
    return first_server if ((points_played + 1) // 2) % 2 == 0 else other(first_server)


def tiebreak_first_server(current_server: Player, points_played: int) -> Player:
    """Invert ``tiebreak_point_server``: who served point 1, given the current server."""
    if ((points_played + 1) // 2) % 2 == 0:
        return current_server
    return other(current_server)


def validate_game_points(a: int, b: int) -> None:
    if a < 0 or b < 0:
        raise InvalidStateError(f"negative points {a}-{b}")
    if (a >= 4 and a - b >= 2) or (b >= 4 and b - a >= 2):
        raise InvalidStateError(f"game already complete at {a}-{b}")
    if max(a, b) >= 4 and abs(a - b) > 1:
        raise InvalidStateError(f"impossible game score {a}-{b}")


def validate_tiebreak_points(a: int, b: int, target: int) -> None:
    if a < 0 or b < 0:
        raise InvalidStateError(f"negative tiebreak points {a}-{b}")
    if (a >= target and a - b >= 2) or (b >= target and b - a >= 2):
        raise InvalidStateError(f"tiebreak already complete at {a}-{b}")
    if max(a, b) >= target and abs(a - b) > 1:
        raise InvalidStateError(f"impossible tiebreak score {a}-{b}")


def validate_state(state: ScoreState, ruleset: Ruleset) -> None:
    need = ruleset.sets_to_win
    if state.server not in ("A", "B"):
        raise InvalidStateError(f"unknown server {state.server!r}")
    if state.sets_a < 0 or state.sets_b < 0:
        raise InvalidStateError("negative set count")
    if state.sets_a >= need or state.sets_b >= need:
        raise InvalidStateError("match already complete")
    rules = ruleset.set_rules(state.sets_a + state.sets_b)
    if not rules.is_valid_in_progress(state.games_a, state.games_b):
        raise InvalidStateError(f"invalid in-progress set score {state.games_a}-{state.games_b}")
    if rules.is_tiebreak(state.games_a, state.games_b):
        validate_tiebreak_points(state.points_a, state.points_b, rules.tiebreak_points)
    else:
        validate_game_points(state.points_a, state.points_b)


SetDist = dict[tuple[Player, Player], float]
"""Map of (set winner, server of the first game of the next set) -> probability."""


class MarkovPricer:
    """Exact match / set win probabilities for fixed serve-point probabilities."""

    def __init__(self, p_a: float, p_b: float, ruleset: Ruleset) -> None:
        _check_prob("p_a", p_a)
        _check_prob("p_b", p_b)
        self.p_a = float(p_a)
        self.p_b = float(p_b)
        self.ruleset = ruleset
        self.hold_a = game_win_prob(self.p_a)
        self.hold_b = game_win_prob(self.p_b)
        self._set_memo: dict[tuple[int, int, Player, SetRules], SetDist] = {}
        self._tb_memo: dict[tuple[int, int, Player, int], float] = {}
        self._match_memo: dict[tuple[int, int, Player], float] = {}

    # -- points and games -------------------------------------------------------------

    def point_prob_a(self, server: Player) -> float:
        """P(A wins the next point) given the point's server."""
        return self.p_a if server == "A" else 1.0 - self.p_b

    def game_prob_a(self, server: Player, points_a: int = 0, points_b: int = 0) -> float:
        """P(A wins the current regular game)."""
        if server == "A":
            return game_win_prob(self.p_a, points_a, points_b)
        return 1.0 - game_win_prob(self.p_b, points_b, points_a)

    def tiebreak_prob_a(self, a: int, b: int, first_server: Player, target: int) -> float:
        """P(A wins a first-to-``target`` (win by two) tiebreak from points a-b."""
        if a >= target and a - b >= 2:
            return 1.0
        if b >= target and b - a >= 2:
            return 0.0
        if a >= target - 1 and b >= target - 1 and a == b:
            # From a tie past target-1, the next two points are served one each, so the
            # chance of winning both is order invariant: a two-point "deuce".
            x = self.p_a * (1.0 - self.p_b)
            y = (1.0 - self.p_a) * self.p_b
            return x / (x + y)
        key = (a, b, first_server, target)
        cached = self._tb_memo.get(key)
        if cached is not None:
            return cached
        server = tiebreak_point_server(first_server, a + b)
        pw = self.point_prob_a(server)
        val = pw * self.tiebreak_prob_a(a + 1, b, first_server, target) + (
            1.0 - pw
        ) * self.tiebreak_prob_a(a, b + 1, first_server, target)
        self._tb_memo[key] = val
        return val

    # -- sets ---------------------------------------------------------------------------

    def _advantage_tie_prob_a(self) -> float:
        x = self.hold_a * (1.0 - self.hold_b)
        y = (1.0 - self.hold_a) * self.hold_b
        return x / (x + y)

    def set_dist_from_game_start(self, ga: int, gb: int, server: Player, rules: SetRules) -> SetDist:
        """Set outcome distribution at the start of a game (``server`` serves it)."""
        winner = rules.winner(ga, gb)
        if winner is not None:
            raise InvalidStateError(f"set already complete at {ga}-{gb}")
        key = (ga, gb, server, rules)
        cached = self._set_memo.get(key)
        if cached is not None:
            return cached

        if rules.is_tiebreak(ga, gb):
            t = self.tiebreak_prob_a(0, 0, server, rules.tiebreak_points)
            nxt = other(server)
            dist: SetDist = {("A", nxt): t, ("B", nxt): 1.0 - t}
        elif rules.mode == "advantage" and ga == gb and ga >= rules.games_to_win - 1:
            # Only legal for the deciding set, so the next-set server is irrelevant.
            pa = self._advantage_tie_prob_a()
            dist = {("A", "A"): pa, ("B", "A"): 1.0 - pa}
        else:
            g = self.hold_a if server == "A" else 1.0 - self.hold_b
            dist = self._after_game(ga, gb, server, g, rules)
        self._set_memo[key] = dist
        return dist

    def _after_game(self, ga: int, gb: int, server: Player, g_a: float, rules: SetRules) -> SetDist:
        """Combine the outcome of the current game (A wins w.p. ``g_a``) with the rest."""
        out: SetDist = {}
        nxt = other(server)
        for prob, (na, nb) in ((g_a, (ga + 1, gb)), (1.0 - g_a, (ga, gb + 1))):
            if prob == 0.0:
                continue
            w = rules.winner(na, nb)
            if w is not None:
                k = (w, nxt)
                out[k] = out.get(k, 0.0) + prob
                continue
            for k, v in self.set_dist_from_game_start(na, nb, nxt, rules).items():
                out[k] = out.get(k, 0.0) + prob * v
        return out

    def set_dist_in_progress(self, state: ScoreState) -> SetDist:
        rules = self.ruleset.set_rules(state.sets_a + state.sets_b)
        ga, gb = state.games_a, state.games_b
        if rules.is_tiebreak(ga, gb):
            played = state.points_a + state.points_b
            first = tiebreak_first_server(state.server, played)
            t = self.tiebreak_prob_a(state.points_a, state.points_b, first, rules.tiebreak_points)
            nxt = other(first)
            return {("A", nxt): t, ("B", nxt): 1.0 - t}
        if state.points_a == 0 and state.points_b == 0:
            return self.set_dist_from_game_start(ga, gb, state.server, rules)
        # Finish the current game, then continue from the next game start (which also
        # covers the closed-form advantage-set tail).
        g = self.game_prob_a(state.server, state.points_a, state.points_b)
        return self._after_game(ga, gb, state.server, g, rules)

    # -- match --------------------------------------------------------------------------

    def match_prob_from_set_start(self, sets_a: int, sets_b: int, server: Player) -> float:
        need = self.ruleset.sets_to_win
        if sets_a >= need:
            return 1.0
        if sets_b >= need:
            return 0.0
        key = (sets_a, sets_b, server)
        cached = self._match_memo.get(key)
        if cached is not None:
            return cached
        rules = self.ruleset.set_rules(sets_a + sets_b)
        dist = self.set_dist_from_game_start(0, 0, server, rules)
        val = 0.0
        for (w, nxt), prob in dist.items():
            val += prob * self.match_prob_from_set_start(
                sets_a + (w == "A"), sets_b + (w == "B"), nxt
            )
        self._match_memo[key] = val
        return val

    def match_prob(self, state: ScoreState) -> float:
        """P(A wins the match) from an in-progress state."""
        validate_state(state, self.ruleset)
        dist = self.set_dist_in_progress(state)
        val = 0.0
        for (w, nxt), prob in dist.items():
            val += prob * self.match_prob_from_set_start(
                state.sets_a + (w == "A"), state.sets_b + (w == "B"), nxt
            )
        return _clip_unit(val)

    def prematch_prob(self, first_server: Player | None = None) -> float:
        """P(A wins) at 0-0. With an unknown first server, average both (coin toss)."""
        if first_server is not None:
            return self.match_prob(ScoreState(server=first_server))
        return 0.5 * (
            self.match_prob(ScoreState(server="A")) + self.match_prob(ScoreState(server="B"))
        )

    def set_outcome_probs(self, state: ScoreState, set_number: int) -> tuple[float, float, float]:
        """(P(A wins set n), P(B wins set n), P(set n is never played)) for 1-based n.

        Raises ``InvalidStateError`` for a set that has already concluded.
        """
        validate_state(state, self.ruleset)
        idx = set_number - 1
        cur = state.sets_a + state.sets_b
        if idx < cur:
            raise InvalidStateError(f"set {set_number} has already concluded")
        if idx >= self.ruleset.best_of:
            raise InvalidStateError(f"set {set_number} outside best-of-{self.ruleset.best_of}")
        dist = self.set_dist_in_progress(state)
        if idx == cur:
            pa = sum(v for (w, _), v in dist.items() if w == "A")
            return _clip_unit(pa), _clip_unit(1.0 - pa), 0.0
        # Forward-propagate (sets_a, sets_b, next server) through future sets.
        need = self.ruleset.sets_to_win
        frontier: dict[tuple[int, int, Player], float] = {}
        for (w, nxt), prob in dist.items():
            k = (state.sets_a + (w == "A"), state.sets_b + (w == "B"), nxt)
            frontier[k] = frontier.get(k, 0.0) + prob
        for s in range(cur + 1, idx):
            nxt_frontier: dict[tuple[int, int, Player], float] = {}
            for (sa, sb, srv), prob in frontier.items():
                if sa >= need or sb >= need:
                    nxt_frontier[(sa, sb, srv)] = nxt_frontier.get((sa, sb, srv), 0.0) + prob
                    continue
                d = self.set_dist_from_game_start(0, 0, srv, self.ruleset.set_rules(s))
                for (w, n2), p2 in d.items():
                    k = (sa + (w == "A"), sb + (w == "B"), n2)
                    nxt_frontier[k] = nxt_frontier.get(k, 0.0) + prob * p2
            frontier = nxt_frontier
        pa = pb = not_played = 0.0
        rules = self.ruleset.set_rules(idx)
        for (sa, sb, srv), prob in frontier.items():
            if sa >= need or sb >= need:
                not_played += prob
                continue
            d = self.set_dist_from_game_start(0, 0, srv, rules)
            win_a = sum(v for (w, _), v in d.items() if w == "A")
            pa += prob * win_a
            pb += prob * (1.0 - win_a)
        return _clip_unit(pa), _clip_unit(pb), _clip_unit(not_played)


def _clip_unit(x: float) -> float:
    if not math.isfinite(x):
        raise InvalidStateError(f"non-finite probability {x!r}")
    if x < -1e-9 or x > 1.0 + 1e-9:
        raise InvalidStateError(f"probability {x!r} out of bounds")
    return min(1.0, max(0.0, x))
