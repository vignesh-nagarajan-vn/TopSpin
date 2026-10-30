from __future__ import annotations

import pytest

from topspin.pricing.calibration import (
    CalibrationError,
    calibrate_to_anchor,
    serve_probs,
    theta_bounds,
)
from topspin.pricing.markov import MarkovPricer, ScoreState
from topspin.pricing.rulesets import SLAM_MEN_BO5, TOUR_BO3
from topspin.pricing.uncertainty import AnchoredMatchModel


@pytest.mark.parametrize("anchor", [0.12, 0.35, 0.5, 0.62, 0.88])
@pytest.mark.parametrize("ruleset", [TOUR_BO3, SLAM_MEN_BO5])
def test_anchor_reproduced_at_zero_zero(anchor, ruleset):
    res = calibrate_to_anchor(anchor, ruleset, 0.645)
    assert res.abs_error < 1e-6
    assert MarkovPricer(res.p_a, res.p_b, ruleset).prematch_prob() == pytest.approx(
        anchor, abs=1e-6
    )
    assert (
        0.40 <= res.p_b <= res.p_a <= 0.85 if anchor >= 0.5 else 0.40 <= res.p_a <= res.p_b <= 0.85
    )


def test_favourite_gets_higher_serve_probability():
    res = calibrate_to_anchor(0.7, TOUR_BO3, 0.645)
    assert res.p_a > 0.645 > res.p_b and res.theta > 0


def test_unreachable_anchor_fails_instead_of_fabricating():
    with pytest.raises(CalibrationError):
        calibrate_to_anchor(0.99, TOUR_BO3, 0.645, p_min=0.62, p_max=0.67)
    with pytest.raises(CalibrationError):
        calibrate_to_anchor(float("nan"), TOUR_BO3, 0.645)
    with pytest.raises(CalibrationError):
        calibrate_to_anchor(0.6, TOUR_BO3, 0.95)  # baseline outside bounds


def test_theta_bounds_keep_probabilities_in_range():
    lo, hi = theta_bounds(0.645, 0.40, 0.85)
    for th in (lo, hi):
        pa, pb = serve_probs(0.645, th)
        assert 0.40 - 1e-12 <= pa <= 0.85 + 1e-12 and 0.40 - 1e-12 <= pb <= 0.85 + 1e-12


def test_anchored_model_band_contains_fair_and_is_conservative():
    m = AnchoredMatchModel(0.6, 0.005, TOUR_BO3, 0.645, base_delta=0.03, floor=0.01)
    est = m.match_estimate(ScoreState(server="A"))
    assert est.low < est.fair < est.high
    assert est.low <= min(est.scenario_values) - 0.01 + 1e-12
    assert len(m.scenarios) == 9
    comp = est.complement()
    assert comp.fair == pytest.approx(1 - est.fair) and comp.low == pytest.approx(1 - est.high)


def test_serve_assumption_sensitivity_is_exposed():
    """Mid-match prices depend on the (unidentified) baseline; the band must show it."""
    lo_base = AnchoredMatchModel(0.6, 0.0, TOUR_BO3, 0.60, base_delta=1e-6, floor=0.0)
    hi_base = AnchoredMatchModel(0.6, 0.0, TOUR_BO3, 0.69, base_delta=1e-6, floor=0.0)
    s = ScoreState(games_a=2, games_b=0, server="A")
    assert lo_base.match_estimate(s).fair != pytest.approx(hi_base.match_estimate(s).fair, abs=1e-4)
