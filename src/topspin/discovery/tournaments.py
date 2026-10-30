"""Classify a match's competition into a supported scoring ruleset, or reject it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from topspin.discovery.matching import normalize_text
from topspin.pricing.rulesets import Ruleset, ruleset_from_config


@dataclass(frozen=True, slots=True)
class Classification:
    supported: bool
    reason: str
    ruleset: Ruleset | None = None
    grand_slam: bool = False


class TournamentClassifier:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.version = str(spec.get("version", "unversioned"))
        self.exclude = [re.compile(p) for p in spec.get("exclude_patterns", [])]
        self.exclude_rounds = [re.compile(p) for p in spec.get("exclude_round_patterns", [])]
        self.slams = [re.compile(p) for p in spec.get("grand_slam_patterns", [])]
        self.tour = {k.upper(): re.compile(v) for k, v in (spec.get("tour_patterns") or {}).items()}
        self.rulesets: dict[str, str] = dict(spec.get("rulesets") or {})
        self.custom_rulesets: dict[str, dict[str, Any]] = dict(spec.get("custom_rulesets") or {})
        self.allowed = [normalize_text(t) for t in spec.get("allowed_tournaments") or []]

    @classmethod
    def from_file(cls, path: str | Path) -> TournamentClassifier:
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})

    def _ruleset(self, key: str) -> Ruleset | None:
        name = self.rulesets.get(key)
        if name is None:
            return None
        return ruleset_from_config(name, self.custom_rulesets.get(name))

    def hint_excluded(self, hint: str) -> bool:
        name = normalize_text(hint)
        return any(p.search(name) for p in self.exclude)

    def classify(
        self, tour: str, tournament_name: str | None, round_name: str | None = None
    ) -> Classification:
        tour = tour.upper()
        if tour not in ("ATP", "WTA"):
            return Classification(False, f"tour_not_supported:{tour}")
        if not tournament_name:
            return Classification(False, "tournament_unknown")
        name = normalize_text(tournament_name)
        for pat in self.exclude:
            if pat.search(name):
                return Classification(False, f"excluded:{pat.pattern}")
        if round_name:
            rnd = normalize_text(round_name)
            for pat in self.exclude_rounds:
                if pat.search(rnd):
                    return Classification(False, f"excluded_round:{pat.pattern}")
        if self.allowed and not any(a in name for a in self.allowed):
            return Classification(False, "not_in_allow_list")
        if "singles" not in name:
            return Classification(False, "not_identified_as_singles")
        if any(p.search(name) for p in self.slams):
            # Gender must agree with the tour (men's slam singles on ATP markets, etc.).
            if tour == "ATP" and re.search(r"\bwomen\b", name):
                return Classification(False, "tour_gender_mismatch")
            if tour == "WTA" and re.search(r"\bmen\b", name) and not re.search(r"\bwomen\b", name):
                return Classification(False, "tour_gender_mismatch")
            rs = self._ruleset(f"{tour}_SLAM")
            if rs is None:
                return Classification(False, "no_slam_ruleset")
            return Classification(True, "grand_slam", rs, grand_slam=True)
        tour_pat = self.tour.get(tour)
        if tour_pat is None or not tour_pat.search(name):
            return Classification(False, "not_tour_level_singles")
        rs = self._ruleset(tour)
        if rs is None:
            return Classification(False, "no_tour_ruleset")
        return Classification(True, "tour_level", rs)
