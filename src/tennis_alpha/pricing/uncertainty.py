"""Frozen-anchor match model with a conservative probability band.

The point estimate uses the central anchor and serve baseline. The band re-fits the
model under every combination of

* anchor +/- half the prematch bid/ask spread (the anchor is a midpoint, not a price), and
* serve baseline +/- ``base_delta`` (the single anchor cannot identify serve strength),

then widens the min/max by a fixed ``floor`` for unmodelled effects (fatigue, injury,
momentum, retirement risk). Trading decisions use the band edge that is *least*
favourable to the trade, never the midpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tennis_alpha.pricing.calibration import CalibrationResult, calibrate_to_anchor
from tennis_alpha.pricing.markov import MarkovPricer, ScoreState
from tennis_alpha.pricing.rulesets import Ruleset

MODEL_VERSION = "anchored-markov-1.0"


@dataclass(frozen=True, slots=True)
class ProbEstimate:
    fair: float
    low: float
    high: float
    scenario_values: tuple[float, ...] = field(default_factory=tuple)

    @property
    def width(self) -> float:
        return self.high - self.low

    def complement(self) -> ProbEstimate:
        return ProbEstimate(
            fair=1.0 - self.fair,
            low=1.0 - self.high,
            high=1.0 - self.low,
            scenario_values=tuple(1.0 - v for v in self.scenario_values),
        )


class AnchoredMatchModel:
    """Model for one match. Built once from the frozen anchor; never refit in play."""

    def __init__(
        self,
        anchor: float,
        anchor_half_spread: float,
        ruleset: Ruleset,
        base: float,
        *,
        base_delta: float = 0.03,
        floor: float = 0.01,
        p_min: float = 0.40,
        p_max: float = 0.85,
        tolerance: float = 1e-6,
    ) -> None:
        self.anchor = anchor
        self.anchor_half_spread = anchor_half_spread
        self.ruleset = ruleset
        self.base = base
        self.floor = floor
        self.central: CalibrationResult = calibrate_to_anchor(
            anchor, ruleset, base, p_min=p_min, p_max=p_max, tolerance=tolerance
        )
        self._central_pricer = self.central.pricer(ruleset)
        anchors = sorted({anchor - anchor_half_spread, anchor, anchor + anchor_half_spread})
        bases = sorted({base - base_delta, base, base + base_delta})
        self.scenarios: list[CalibrationResult] = []
        for a in anchors:
            for b in bases:
                if a == anchor and b == base:
                    self.scenarios.append(self.central)
                else:
                    self.scenarios.append(
                        calibrate_to_anchor(a, ruleset, b, p_min=p_min, p_max=p_max, tolerance=tolerance)
                    )
        self._pricers: list[MarkovPricer] = [
            self._central_pricer if s is self.central else s.pricer(ruleset) for s in self.scenarios
        ]

    @property
    def serve_probs(self) -> tuple[float, float]:
        return self.central.p_a, self.central.p_b

    def _band(self, fair: float, values: list[float]) -> ProbEstimate:
        lo = max(0.0, min(values) - self.floor)
        hi = min(1.0, max(values) + self.floor)
        return ProbEstimate(fair=fair, low=lo, high=hi, scenario_values=tuple(values))

    def match_estimate(self, state: ScoreState) -> ProbEstimate:
        fair = self._central_pricer.match_prob(state)
        values = [p.match_prob(state) for p in self._pricers]
        return self._band(fair, values)

    def set_estimate(self, state: ScoreState, set_number: int) -> ProbEstimate:
        """Current-set winner estimate. Future sets are rejected upstream."""
        fair = self._central_pricer.set_outcome_probs(state, set_number)[0]
        values = [p.set_outcome_probs(state, set_number)[0] for p in self._pricers]
        return self._band(fair, values)
