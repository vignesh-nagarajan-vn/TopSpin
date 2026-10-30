"""Event-state machine: holds vs converted breaks, sets, corrections, gaps, server checks."""

from __future__ import annotations

import pytest

from tests.synthetic import A_ID, B_ID, T0, S, score
from topspin.models.match import MatchStatus
from topspin.pricing.rulesets import TOUR_BO3
from topspin.strategy.events import (
    MatchEventDetector,
    OrientationError,
    TennisEventKind,
    orient,
)


def feed(det, snaps):
    out = []
    for sn in snaps:
        out.extend(det.update(orient(sn, A_ID, B_ID, TOUR_BO3)))
    return out


def kinds(evs):
    return [e.kind for e in evs]


def test_hold_and_break_distinguished():
    det = MatchEventDetector("m", TOUR_BO3)
    evs = feed(
        det,
        [
            score(T0, MatchStatus.NOT_STARTED, None, None, None),
            score(T0 + S, MatchStatus.LIVE, [(0, 0)], (0, 0), "A"),
            score(T0 + 2 * S, MatchStatus.LIVE, [(1, 0)], (0, 0), "B"),  # A held
            score(T0 + 3 * S, MatchStatus.LIVE, [(2, 0)], (0, 0), "A"),  # A broke B
        ],
    )
    k = kinds(evs)
    assert TennisEventKind.MATCH_START in k and TennisEventKind.HOLD in k
    brk = [e for e in evs if e.kind == TennisEventKind.BREAK]
    assert len(brk) == 1 and brk[0].player == "A" and brk[0].actionable
    assert brk[0].detail["broken"] == "B"


def test_break_point_alone_is_not_a_break():
    det = MatchEventDetector("m", TOUR_BO3)
    evs = feed(
        det,
        [
            score(T0, MatchStatus.LIVE, [(0, 0)], (0, 0), "A"),
            score(T0 + S, MatchStatus.LIVE, [(0, 0)], (0, 3), "A"),  # 0-40: triple break point
            score(T0 + 2 * S, MatchStatus.LIVE, [(0, 0)], (3, 3), "A"),  # saved to deuce
        ],
    )
    assert TennisEventKind.BREAK not in kinds(evs)


def test_set_completion_and_next_server():
    det = MatchEventDetector("m", TOUR_BO3)
    evs = feed(
        det,
        [
            score(T0, MatchStatus.LIVE, [(5, 4)], (0, 0), "A"),
            score(T0 + S, MatchStatus.LIVE, [(6, 4), (0, 0)], (0, 0), "B"),
        ],
    )
    sc = [e for e in evs if e.kind == TennisEventKind.SET_COMPLETE]
    assert sc and sc[0].player == "A" and sc[0].actionable
    assert TennisEventKind.SERVER_INCONSISTENT not in kinds(evs)


def test_tiebreak_set_next_server_is_tiebreak_receiver():
    det = MatchEventDetector("m", TOUR_BO3)
    # 6-6; A served point 1 of the tiebreak, so after 6 points (3-3) B is serving point 7.
    # The tiebreak receiver (B) serves the first game of the next set.
    evs = feed(
        det,
        [
            score(T0, MatchStatus.LIVE, [(6, 6)], (3, 3), "B", tb_points=True),
            score(T0 + S, MatchStatus.LIVE, [(7, 6), (0, 0)], (0, 0), "B"),
        ],
    )
    k = kinds(evs)
    assert TennisEventKind.TIEBREAK_COMPLETE in k and TennisEventKind.SET_COMPLETE in k
    assert TennisEventKind.SERVER_INCONSISTENT not in k
    # Had A started the next set, rotation would be contradicted.
    det2 = MatchEventDetector("m", TOUR_BO3)
    evs2 = feed(
        det2,
        [
            score(T0, MatchStatus.LIVE, [(6, 6)], (3, 3), "B", tb_points=True),
            score(T0 + S, MatchStatus.LIVE, [(7, 6), (0, 0)], (0, 0), "A"),
        ],
    )
    assert TennisEventKind.SERVER_INCONSISTENT in kinds(evs2)


def test_server_contradicting_rotation_blocks_trading():
    det = MatchEventDetector("m", TOUR_BO3)
    evs = feed(
        det,
        [
            score(T0, MatchStatus.LIVE, [(1, 0)], (0, 0), "B"),
            score(T0 + S, MatchStatus.LIVE, [(2, 0)], (0, 0), "B"),  # B should not serve twice
        ],
    )
    assert TennisEventKind.SERVER_INCONSISTENT in kinds(evs)
    assert det.current_server() is None


def test_unknown_server_prevents_attribution():
    det = MatchEventDetector("m", TOUR_BO3)
    evs = feed(
        det,
        [
            score(T0, MatchStatus.LIVE, [(1, 0)], (0, 0), None),
            score(T0 + S, MatchStatus.LIVE, [(2, 0)], (0, 0), None),
        ],
    )
    assert TennisEventKind.GAME_UNATTRIBUTED in kinds(evs)
    assert TennisEventKind.BREAK not in kinds(evs)
    assert det.current_server() is None


def test_multi_game_jump_is_a_gap_not_a_break():
    det = MatchEventDetector("m", TOUR_BO3)
    evs = feed(
        det,
        [
            score(T0, MatchStatus.LIVE, [(1, 0)], (0, 0), "B"),
            score(T0 + S, MatchStatus.LIVE, [(3, 0)], (0, 0), "B"),
        ],
    )
    assert TennisEventKind.DATA_GAP in kinds(evs)
    assert TennisEventKind.BREAK not in kinds(evs)


def test_score_regression_is_a_correction_and_blocks_until_stable():
    det = MatchEventDetector("m", TOUR_BO3, stable_snapshots=2)
    evs = feed(
        det,
        [
            score(T0, MatchStatus.LIVE, [(2, 1)], (0, 0), "B"),
            score(T0 + S, MatchStatus.LIVE, [(2, 0)], (0, 0), "B"),
        ],
    )
    assert TennisEventKind.CORRECTION in kinds(evs) and not det.reliable
    feed(det, [score(T0 + 2 * S, MatchStatus.LIVE, [(2, 0)], (1, 0), "B")])
    assert not det.reliable
    feed(det, [score(T0 + 3 * S, MatchStatus.LIVE, [(2, 0)], (2, 0), "B")])
    assert det.reliable


def test_duplicate_events_never_retrigger():
    det = MatchEventDetector("m", TOUR_BO3)
    snaps = [
        score(T0, MatchStatus.LIVE, [(1, 0)], (0, 0), "B"),
        score(T0 + S, MatchStatus.LIVE, [(2, 0)], (0, 0), "A"),
        score(T0 + 2 * S, MatchStatus.LIVE, [(1, 0)], (0, 0), "B"),  # correction back
        score(T0 + 3 * S, MatchStatus.LIVE, [(2, 0)], (0, 0), "A"),  # same break again
    ]
    evs = feed(det, snaps)
    assert sum(1 for e in evs if e.kind == TennisEventKind.BREAK) == 1


def test_suspension_retirement_and_walkover():
    det = MatchEventDetector("m", TOUR_BO3)
    evs = feed(
        det,
        [
            score(T0, MatchStatus.LIVE, [(6, 4), (2, 1)], (0, 0), "A"),
            score(T0 + S, MatchStatus.SUSPENDED, [(6, 4), (2, 1)], None, None),
            score(T0 + 2 * S, MatchStatus.LIVE, [(6, 4), (2, 1)], (0, 0), "A"),
            score(
                T0 + 3 * S, MatchStatus.FINISHED, [(6, 4), (2, 1)], None, None
            ),  # ended undecided
        ],
    )
    k = kinds(evs)
    assert (
        TennisEventKind.SUSPENSION in k
        and TennisEventKind.RESUMPTION in k
        and TennisEventKind.RETIREMENT in k
    )
    det2 = MatchEventDetector("m2", TOUR_BO3)
    evs2 = feed(
        det2,
        [
            score(T0, MatchStatus.NOT_STARTED, None, None, None),
            score(T0 + S, MatchStatus.WALKOVER, None, None, None),
        ],
    )
    assert TennisEventKind.WALKOVER in kinds(evs2)


def test_completed_match():
    det = MatchEventDetector("m", TOUR_BO3)
    evs = feed(
        det,
        [
            score(T0, MatchStatus.LIVE, [(6, 4), (5, 3)], (0, 0), "A"),
            score(T0 + S, MatchStatus.FINISHED, [(6, 4), (6, 3)], None, None),
        ],
    )
    mc = [e for e in evs if e.kind == TennisEventKind.MATCH_COMPLETE]
    assert mc and mc[0].player == "A"
    # A completed-set event at match end is never actionable.
    assert not any(e.actionable for e in evs if e.kind == TennisEventKind.SET_COMPLETE)


@pytest.mark.parametrize(
    "sets",
    [[(7, 2)], [(6, 4), (6, 3), (1, 0)], [(3, 2), (1, 0)]],
)
def test_impossible_scores_rejected(sets):
    with pytest.raises(OrientationError):
        orient(score(T0, MatchStatus.LIVE, sets, (0, 0), "A"), A_ID, B_ID, TOUR_BO3)


def test_orientation_requires_matching_competitors():
    with pytest.raises(OrientationError):
        orient(score(T0, MatchStatus.LIVE, [(0, 0)], (0, 0), "A"), A_ID, "999", TOUR_BO3)


def test_unknown_points_price_at_game_start():
    o = orient(score(T0, MatchStatus.LIVE, [(2, 1)], None, "B"), A_ID, B_ID, TOUR_BO3)
    assert o.points_a is None and o.to_state().points_a == 0
