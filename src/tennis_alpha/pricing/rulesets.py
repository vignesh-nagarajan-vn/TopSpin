"""Tennis singles scoring formats.

Every format the engine may price is an explicit, versioned ``Ruleset``. A match whose
format cannot be mapped to a known ruleset is never traded.

Sources (verified when this module was written; re-verify before each season):
* ATP / WTA tour-level singles: best of three tiebreak sets; 7-point tiebreak at 6-6
  in every set, including the deciding set.
* Grand Slam singles (since 2022, all four Slams): a 10-point tiebreak at 6-6 in the
  deciding set. Men's main draw is best of five, women's is best of three. Every
  non-deciding set uses the 7-point tiebreak at 6-6.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RULESET_VERSION = "2026.1"

FinalSetMode = Literal["tiebreak", "advantage"]


@dataclass(frozen=True, slots=True)
class SetRules:
    """Rules for a single set."""

    games_to_win: int = 6
    tiebreak_at: int = 6
    tiebreak_points: int = 7
    # "advantage" means no tiebreak; the set is won by two clear games.
    mode: FinalSetMode = "tiebreak"

    def winner(self, ga: int, gb: int) -> str | None:
        """Return 'A' or 'B' if the game score is a completed set, else None."""
        g = self.games_to_win
        if self.mode == "tiebreak":
            t = self.tiebreak_at
            if (ga >= g and ga - gb >= 2) or (ga == t + 1 and gb == t):
                return "A"
            if (gb >= g and gb - ga >= 2) or (gb == t + 1 and ga == t):
                return "B"
            return None
        if ga >= g and ga - gb >= 2:
            return "A"
        if gb >= g and gb - ga >= 2:
            return "B"
        return None

    def is_valid_in_progress(self, ga: int, gb: int) -> bool:
        if ga < 0 or gb < 0:
            return False
        if self.winner(ga, gb) is not None:
            return False
        if self.mode == "tiebreak":
            t = self.tiebreak_at
            return ga <= t and gb <= t
        # Advantage sets: any non-terminal score within one game of each other past g-1.
        return abs(ga - gb) <= 1 or max(ga, gb) < self.games_to_win

    def is_tiebreak(self, ga: int, gb: int) -> bool:
        return self.mode == "tiebreak" and ga == self.tiebreak_at and gb == self.tiebreak_at


@dataclass(frozen=True, slots=True)
class Ruleset:
    name: str
    best_of: int
    regular_set: SetRules
    final_set: SetRules
    version: str = RULESET_VERSION

    def __post_init__(self) -> None:
        if self.best_of not in (3, 5):
            raise ValueError(f"unsupported best_of={self.best_of}")
        if self.regular_set.mode != "tiebreak":
            raise ValueError("only the deciding set may be an advantage set")

    @property
    def sets_to_win(self) -> int:
        return (self.best_of + 1) // 2

    def set_rules(self, set_index: int) -> SetRules:
        """Rules for the set with 0-based ``set_index``."""
        if set_index < 0 or set_index >= self.best_of:
            raise ValueError(f"set index {set_index} outside best-of-{self.best_of}")
        return self.final_set if set_index == self.best_of - 1 else self.regular_set

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"


_STD = SetRules(tiebreak_points=7)
_SLAM_DECIDER = SetRules(tiebreak_points=10)

TOUR_BO3 = Ruleset("tour_bo3_tb7", best_of=3, regular_set=_STD, final_set=_STD)
SLAM_MEN_BO5 = Ruleset("slam_men_bo5_final_tb10", best_of=5, regular_set=_STD, final_set=_SLAM_DECIDER)
SLAM_WOMEN_BO3 = Ruleset(
    "slam_women_bo3_final_tb10", best_of=3, regular_set=_STD, final_set=_SLAM_DECIDER
)

BUILTIN_RULESETS: dict[str, Ruleset] = {
    r.name: r for r in (TOUR_BO3, SLAM_MEN_BO5, SLAM_WOMEN_BO3)
}


def ruleset_from_config(name: str, spec: dict[str, object] | None = None) -> Ruleset:
    """Return a built-in ruleset, or build a custom one from a config mapping."""
    if spec is None:
        if name not in BUILTIN_RULESETS:
            raise KeyError(f"unknown ruleset {name!r}")
        return BUILTIN_RULESETS[name]
    best_of = int(spec["best_of"])  # type: ignore[call-overload]
    tb = int(spec.get("tiebreak_points", 7))  # type: ignore[call-overload]
    final_mode = str(spec.get("final_set_mode", "tiebreak"))
    final_tb = int(spec.get("final_set_tiebreak_points", tb))  # type: ignore[call-overload]
    if final_mode not in ("tiebreak", "advantage"):
        raise ValueError(f"bad final_set_mode {final_mode!r}")
    regular = SetRules(tiebreak_points=tb)
    final = SetRules(tiebreak_points=final_tb, mode=final_mode)  # type: ignore[arg-type]
    return Ruleset(name, best_of=best_of, regular_set=regular, final_set=final)
