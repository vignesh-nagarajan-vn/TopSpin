"""Fit serve-point probabilities to a frozen prematch market anchor.

A single prematch win probability cannot identify two serve-point probabilities. We fix
a configurable tour baseline serve-point win rate ``base`` and fit a single relative
strength parameter ``theta`` on the logit scale:

    logit(p_a) = logit(base) + theta / 2
    logit(p_b) = logit(base) - theta / 2

``theta`` is found by bounded Brent root finding so that the Markov prematch probability
(averaged over the unknown coin-toss first server) reproduces the anchor. The two serve
probabilities are therefore *assumption-dependent*, not independently identified; the
uncertainty module re-fits under alternative baselines to expose that sensitivity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.optimize import brentq

from tennis_alpha.pricing.markov import MarkovPricer
from tennis_alpha.pricing.rulesets import Ruleset


class CalibrationError(RuntimeError):
    """The anchor cannot be reproduced within feasible, realistic parameters."""


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    anchor: float
    base: float
    theta: float
    p_a: float
    p_b: float
    reproduced: float
    abs_error: float
    ruleset_key: str
    iterations: int

    def pricer(self, ruleset: Ruleset) -> MarkovPricer:
        return MarkovPricer(self.p_a, self.p_b, ruleset)


def logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def expit(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def serve_probs(base: float, theta: float) -> tuple[float, float]:
    lb = logit(base)
    return expit(lb + theta / 2.0), expit(lb - theta / 2.0)


def theta_bounds(base: float, p_min: float, p_max: float) -> tuple[float, float]:
    """Largest symmetric theta interval keeping both serve probabilities in range."""
    lb = logit(base)
    hi = 2.0 * min(logit(p_max) - lb, lb - logit(p_min))
    if hi <= 0:
        raise CalibrationError(f"baseline {base} outside serve bounds [{p_min}, {p_max}]")
    return -hi, hi


def calibrate_to_anchor(
    anchor: float,
    ruleset: Ruleset,
    base: float,
    *,
    p_min: float = 0.40,
    p_max: float = 0.85,
    tolerance: float = 1e-6,
    max_iter: int = 200,
) -> CalibrationResult:
    if not (math.isfinite(anchor) and 0.0 < anchor < 1.0):
        raise CalibrationError(f"anchor {anchor!r} is not a probability in (0, 1)")
    if not (0.0 < p_min < base < p_max < 1.0):
        raise CalibrationError(f"invalid serve bounds/base: {p_min} < {base} < {p_max}")
    lo, hi = theta_bounds(base, p_min, p_max)

    def f(theta: float) -> float:
        pa, pb = serve_probs(base, theta)
        return MarkovPricer(pa, pb, ruleset).prematch_prob() - anchor

    f_lo, f_hi = f(lo), f(hi)
    if not (f_lo < 0.0 < f_hi):
        raise CalibrationError(
            f"anchor {anchor:.4f} not reachable with serve probabilities in "
            f"[{p_min}, {p_max}] (range {f_lo + anchor:.4f}..{f_hi + anchor:.4f})"
        )
    theta, info = brentq(f, lo, hi, xtol=1e-12, rtol=1e-12, maxiter=max_iter, full_output=True)
    if not info.converged:
        raise CalibrationError(f"root finding did not converge: {info.flag}")
    pa, pb = serve_probs(base, theta)
    reproduced = MarkovPricer(pa, pb, ruleset).prematch_prob()
    err = abs(reproduced - anchor)
    if err > tolerance:
        raise CalibrationError(f"anchor reproduced to {err:.2e}, tolerance {tolerance:.0e}")
    # Monotonicity spot check: a stronger A must never lower A's win probability.
    eps = 1e-3
    if f(min(hi, theta + eps)) < f(max(lo, theta - eps)):
        raise CalibrationError("non-monotone calibration surface")
    return CalibrationResult(
        anchor=anchor,
        base=base,
        theta=float(theta),
        p_a=pa,
        p_b=pb,
        reproduced=reproduced,
        abs_error=err,
        ruleset_key=ruleset.key,
        iterations=int(info.iterations),
    )
