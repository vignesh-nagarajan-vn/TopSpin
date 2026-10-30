"""Gate 2: tennis scoring and Markov pricing correctness."""

from __future__ import annotations

import itertools
import math

import pytest

from topspin.pricing.markov import (
    InvalidStateError,
    MarkovPricer,
    ScoreState,
    game_win_prob,
    tiebreak_first_server,
    tiebreak_point_server,
    validate_state,
)
from topspin.pricing.rulesets import (
    SLAM_MEN_BO5,
    SLAM_WOMEN_BO3,
    TOUR_BO3,
    SetRules,
    ruleset_from_config,
)


def mirror(s: ScoreState) -> ScoreState:
    return ScoreState(
        s.sets_b,
        s.sets_a,
        s.games_b,
        s.games_a,
        s.points_b,
        s.points_a,
        "B" if s.server == "A" else "A",
    )


# ---------------------------------------------------------------- games / deuce


def test_game_known_value_and_symmetry():
    assert game_win_prob(0.6) == pytest.approx(0.735729230769, abs=1e-10)
    assert game_win_prob(0.5) == pytest.approx(0.5, abs=1e-15)
    for p in (0.3, 0.55, 0.7):
        assert game_win_prob(p) + game_win_prob(1 - p) == pytest.approx(1.0, abs=1e-12)


def test_deuce_and_advantage_terminate_in_closed_form():
    p = 0.62
    q = 1 - p
    deuce = p * p / (p * p + q * q)
    assert game_win_prob(p, 3, 3) == pytest.approx(deuce, abs=1e-15)
    assert game_win_prob(p, 4, 3) == pytest.approx(p + q * deuce, abs=1e-15)
    assert game_win_prob(p, 3, 4) == pytest.approx(p * deuce, abs=1e-15)
    # Long deuce battles collapse to the same states.
    assert game_win_prob(p, 11, 11) == pytest.approx(deuce, abs=1e-15)
    assert game_win_prob(p, 12, 11) == pytest.approx(p + q * deuce, abs=1e-15)


def test_game_terminal_values():
    assert game_win_prob(0.6, 4, 0) == 1.0
    assert game_win_prob(0.6, 2, 4) == 0.0
    assert game_win_prob(0.6, 6, 4) == 1.0


def test_brute_force_game_matches_closed_form():
    # Enumerate point sequences up to a long cutoff; the tail mass is negligible.
    p = 0.58
    total = 0.0
    frontier = {(0, 0): 1.0}
    for _ in range(60):
        nxt: dict[tuple[int, int], float] = {}
        for (a, b), m in frontier.items():
            for da, pr in ((1, p), (0, 1 - p)):
                na, nb = a + da, b + (1 - da)
                if na >= 4 and na - nb >= 2:
                    total += m * pr
                elif nb >= 4 and nb - na >= 2:
                    pass
                else:
                    nxt[(na, nb)] = nxt.get((na, nb), 0.0) + m * pr
        frontier = nxt
    assert total == pytest.approx(game_win_prob(p), abs=1e-9)


# ---------------------------------------------------------------- tiebreaks


def test_tiebreak_serve_rotation():
    seq = [tiebreak_point_server("A", k) for k in range(9)]
    assert seq == ["A", "B", "B", "A", "A", "B", "B", "A", "A"]
    for k in range(20):
        cur = tiebreak_point_server("B", k)
        assert tiebreak_first_server(cur, k) == "B"


def _brute_tiebreak(pa: float, pb: float, target: int, first: str) -> float:
    total = 0.0
    frontier = {(0, 0): 1.0}
    for _ in range(80):
        nxt: dict[tuple[int, int], float] = {}
        for (a, b), m in frontier.items():
            srv = tiebreak_point_server(first, a + b)  # type: ignore[arg-type]
            pw = pa if srv == "A" else 1 - pb
            for win, pr in ((True, pw), (False, 1 - pw)):
                na, nb = (a + 1, b) if win else (a, b + 1)
                if na >= target and na - nb >= 2:
                    total += m * pr
                elif nb >= target and nb - na >= 2:
                    pass
                else:
                    nxt[(na, nb)] = nxt.get((na, nb), 0.0) + m * pr
        frontier = nxt
    return total


@pytest.mark.parametrize("target", [7, 10])
def test_tiebreak_first_to_n_matches_brute_force(target):
    m = MarkovPricer(0.68, 0.61, TOUR_BO3)
    for first in ("A", "B"):
        exact = m.tiebreak_prob_a(0, 0, first, target)
        assert exact == pytest.approx(_brute_tiebreak(0.68, 0.61, target, first), abs=1e-9)


def test_longer_tiebreak_favours_stronger_player():
    m = MarkovPricer(0.70, 0.60, TOUR_BO3)
    assert m.tiebreak_prob_a(0, 0, "A", 10) > m.tiebreak_prob_a(0, 0, "A", 7) > 0.5
    even = MarkovPricer(0.64, 0.64, TOUR_BO3)
    assert even.tiebreak_prob_a(0, 0, "A", 7) == pytest.approx(0.5, abs=1e-12)
    assert even.tiebreak_prob_a(0, 0, "B", 10) == pytest.approx(0.5, abs=1e-12)


def test_tiebreak_terminal_values():
    m = MarkovPricer(0.6, 0.6, TOUR_BO3)
    assert m.tiebreak_prob_a(7, 5, "A", 7) == 1.0
    assert m.tiebreak_prob_a(8, 10, "A", 10) == 0.0


# ---------------------------------------------------------------- sets and match


def test_set_rules_winner():
    r = SetRules()
    assert r.winner(6, 4) == "A" and r.winner(7, 5) == "A" and r.winner(7, 6) == "A"
    assert r.winner(4, 6) == "B" and r.winner(6, 7) == "B"
    assert r.winner(6, 5) is None and r.winner(6, 6) is None and r.winner(5, 5) is None
    adv = SetRules(mode="advantage")
    assert adv.winner(7, 6) is None and adv.winner(12, 10) == "A"


def test_equal_players_are_coin_flips():
    for rs in (TOUR_BO3, SLAM_MEN_BO5, SLAM_WOMEN_BO3):
        m = MarkovPricer(0.63, 0.63, rs)
        assert m.prematch_prob() == pytest.approx(0.5, abs=1e-12)


def test_set_lead_increases_win_probability():
    m = MarkovPricer(0.65, 0.63, TOUR_BO3)
    for server in ("A", "B"):
        base = m.match_prob(ScoreState(server=server))
        lead = m.match_prob(ScoreState(sets_a=1, server=server))
        trail = m.match_prob(ScoreState(sets_b=1, server=server))
        assert lead > base > trail


def test_converted_break_effect():
    m = MarkovPricer(0.64, 0.64, TOUR_BO3)
    # Set started with A serving. Game 2 (B serving) is either held (1-1) or broken (2-0).
    held = m.match_prob(ScoreState(games_a=1, games_b=1, server="A"))
    broke = m.match_prob(ScoreState(games_a=2, games_b=0, server="A"))
    start = m.match_prob(ScoreState(server="A"))
    assert broke > held
    # A break moves the match far more than a hold of serve.
    after_hold = m.match_prob(ScoreState(games_a=1, games_b=0, server="B"))
    assert (broke - held) > abs(after_hold - start)
    # Being broken is the mirror image.
    broken = m.match_prob(ScoreState(games_a=0, games_b=2, server="B"))
    assert broken == pytest.approx(1 - broke, abs=1e-12)


def test_best_of_five_amplifies_favourite():
    bo3 = ruleset_from_config("tour_bo3_tb7")
    for pa, pb in ((0.68, 0.62), (0.62, 0.66)):
        p3 = MarkovPricer(pa, pb, bo3).prematch_prob()
        p5 = MarkovPricer(pa, pb, SLAM_MEN_BO5).prematch_prob()
        if p3 > 0.5:
            assert p5 > p3
        else:
            assert p5 < p3


def test_grand_slam_deciding_set_uses_ten_point_tiebreak():
    assert SLAM_MEN_BO5.set_rules(4).tiebreak_points == 10
    assert all(SLAM_MEN_BO5.set_rules(i).tiebreak_points == 7 for i in range(4))
    assert SLAM_WOMEN_BO3.set_rules(2).tiebreak_points == 10
    assert TOUR_BO3.set_rules(2).tiebreak_points == 7
    # 7-5 is a finished first-to-7 tiebreak but in progress in a first-to-10 tiebreak.
    deciding = ScoreState(
        sets_a=2, sets_b=2, games_a=6, games_b=6, points_a=7, points_b=5, server="A"
    )
    validate_state(deciding, SLAM_MEN_BO5)
    with pytest.raises(InvalidStateError):
        validate_state(
            ScoreState(sets_a=1, sets_b=1, games_a=6, games_b=6, points_a=7, points_b=5), TOUR_BO3
        )
    # Non-deciding set at 6-6 in a slam is still first-to-7.
    with pytest.raises(InvalidStateError):
        validate_state(
            ScoreState(sets_a=1, sets_b=1, games_a=6, games_b=6, points_a=7, points_b=5),
            SLAM_MEN_BO5,
        )


def test_complementary_probabilities_sum_to_one():
    pa, pb = 0.67, 0.6
    ma = MarkovPricer(pa, pb, SLAM_MEN_BO5)
    mb = MarkovPricer(pb, pa, SLAM_MEN_BO5)
    states = [
        ScoreState(),
        ScoreState(sets_a=1, games_a=3, games_b=2, points_a=2, points_b=3, server="B"),
        ScoreState(sets_a=2, sets_b=2, games_a=6, games_b=6, points_a=8, points_b=8, server="A"),
        ScoreState(sets_b=2, games_a=5, games_b=4, points_a=4, points_b=3, server="A"),
    ]
    for s in states:
        assert ma.match_prob(s) + mb.match_prob(mirror(s)) == pytest.approx(1.0, abs=1e-12)


def test_terminal_match_states():
    m = MarkovPricer(0.6, 0.6, TOUR_BO3)
    assert m.match_prob_from_set_start(2, 0, "A") == 1.0
    assert m.match_prob_from_set_start(1, 2, "B") == 0.0
    with pytest.raises(InvalidStateError):
        m.match_prob(ScoreState(sets_a=2))


@pytest.mark.parametrize(
    "state",
    [
        ScoreState(games_a=7, games_b=3),
        ScoreState(games_a=6, games_b=4),
        ScoreState(points_a=5, points_b=2),
        ScoreState(points_a=-1),
        ScoreState(sets_a=2, sets_b=0),
        ScoreState(sets_a=3),
        ScoreState(games_a=8, games_b=6),
        ScoreState(server="C"),  # type: ignore[arg-type]
    ],
)
def test_invalid_states_rejected(state):
    with pytest.raises(InvalidStateError):
        validate_state(state, TOUR_BO3)


def test_invalid_probabilities_rejected():
    for bad in (0.0, 1.0, -0.1, math.nan, math.inf):
        with pytest.raises(InvalidStateError):
            MarkovPricer(bad, 0.6, TOUR_BO3)


def test_set_winner_probabilities():
    m = MarkovPricer(0.66, 0.62, TOUR_BO3)
    s = ScoreState(games_a=3, games_b=1, server="B")
    pa, pb, np_ = m.set_outcome_probs(s, 1)
    assert pa + pb == pytest.approx(1.0) and np_ == 0.0 and pa > 0.5
    pa3, pb3, np3 = m.set_outcome_probs(ScoreState(), 3)
    assert pa3 + pb3 + np3 == pytest.approx(1.0, abs=1e-12)
    with pytest.raises(InvalidStateError):
        m.set_outcome_probs(ScoreState(sets_a=1), 1)  # set 1 already concluded


def test_all_reachable_states_are_finite_and_bounded():
    m = MarkovPricer(0.7, 0.55, SLAM_MEN_BO5)
    for sa, sb, ga, gb in itertools.product(range(3), range(3), range(7), range(7)):
        rules = SLAM_MEN_BO5.set_rules(sa + sb)
        if not rules.is_valid_in_progress(ga, gb):
            continue
        pts = (
            [(0, 0), (3, 3), (2, 1)] if not rules.is_tiebreak(ga, gb) else [(0, 0), (6, 6), (9, 9)]
        )
        for (pa, pb), srv in itertools.product(pts, ("A", "B")):
            st = ScoreState(sa, sb, ga, gb, pa, pb, srv)  # type: ignore[arg-type]
            try:
                validate_state(st, SLAM_MEN_BO5)
            except InvalidStateError:
                continue
            v = m.match_prob(st)
            assert math.isfinite(v) and 0.0 <= v <= 1.0
