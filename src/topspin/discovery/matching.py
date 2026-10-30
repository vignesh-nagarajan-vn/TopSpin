"""Identity matching between score-feed matches and exchange markets.

Two paths:

1. **Same-payload identity** (implemented feed): the Polymarket US event state carries the
   same competitor ids as the market's participants, so identity is checked by exact id
   equality, never by names or slugs.
2. **External providers**: names are normalized (case, accents, punctuation, initials,
   reversed order) and *both* players, the date and the singles classification must agree.
   One-player matches, multiple plausible candidates, or any ambiguity are rejected.
   Manual overrides come from an audited YAML file.

Home/away labels carry no tennis identity meaning and are ignored.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from topspin.models.market import MarketInfo
from topspin.models.match import ScoreSnapshot


def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("-", " ").replace(".", " ").replace("'", "")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def name_tokens(name: str) -> list[str]:
    return normalize_text(name).split()


def names_match(a: str, b: str) -> bool:
    """Same player? Handles reversed order ("Sinner Jannik"), initials ("J. Sinner") and
    hyphenated/compound names. Requires surname agreement plus compatible given names."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb or sorted(ta) == sorted(tb):
        return True
    long_a = {t for t in ta if len(t) > 1}
    long_b = {t for t in tb if len(t) > 1}
    common = long_a & long_b
    if not common:
        return False
    rest_a = [t for t in ta if t not in common]
    rest_b = [t for t in tb if t not in common]
    if not rest_a or not rest_b:
        # One side is a strict subset (e.g. surname only): too weak on its own.
        return len(common) >= 2
    # Remaining tokens must be compatible as initials.
    return all(any(x[0] == y[0] for y in rest_b) for x in rest_a)


@dataclass(frozen=True, slots=True)
class MatchLink:
    market_slug: str
    match_key: str
    provider: str
    provider_match_id: str
    long_competitor_id: str
    short_competitor_id: str
    method: str  # "same_payload_ids" | "names" | "manual_override"


class MappingRejected(ValueError):
    pass


def link_same_payload(market: MarketInfo, snap: ScoreSnapshot) -> MatchLink:
    """Validate the market <-> event-state link by exact competitor-id equality."""
    if snap.match_key != market.event_slug:
        raise MappingRejected("score snapshot belongs to a different event")
    ids = set(snap.competitor_ids)
    if ids != {market.long.team_id, market.short.team_id}:
        raise MappingRejected(f"competitor ids {sorted(ids)} != market participants")
    return MatchLink(
        market_slug=market.slug,
        match_key=market.match_key,
        provider=snap.provider,
        provider_match_id=snap.provider_event_id or snap.match_key,
        long_competitor_id=market.long.team_id,
        short_competitor_id=market.short.team_id,
        method="same_payload_ids",
    )


@dataclass(frozen=True, slots=True)
class ExternalMatch:
    provider: str
    match_id: str
    player_1: str
    player_2: str
    player_1_id: str
    player_2_id: str
    start_date: str  # YYYY-MM-DD
    singles: bool


def link_by_names(
    market: MarketInfo, market_date: str, candidates: list[ExternalMatch]
) -> MatchLink:
    """Strict two-player name match for external feeds. Rejects any ambiguity."""
    hits: list[tuple[ExternalMatch, bool]] = []
    for c in candidates:
        if not c.singles or c.start_date != market_date:
            continue
        fwd = names_match(market.long.name, c.player_1) and names_match(
            market.short.name, c.player_2
        )
        rev = names_match(market.long.name, c.player_2) and names_match(
            market.short.name, c.player_1
        )
        one_only = (
            names_match(market.long.name, c.player_1)
            or names_match(market.long.name, c.player_2)
            or names_match(market.short.name, c.player_1)
            or names_match(market.short.name, c.player_2)
        )
        if fwd and rev:
            raise MappingRejected("players indistinguishable by name")
        if fwd or rev:
            hits.append((c, fwd))
        elif one_only:
            continue  # only one player matches: never a link
    if not hits:
        raise MappingRejected("no candidate with both players, same date, singles")
    if len(hits) > 1:
        raise MappingRejected(f"{len(hits)} plausible candidates; ambiguous")
    c, fwd = hits[0]
    return MatchLink(
        market_slug=market.slug,
        match_key=market.match_key,
        provider=c.provider,
        provider_match_id=c.match_id,
        long_competitor_id=c.player_1_id if fwd else c.player_2_id,
        short_competitor_id=c.player_2_id if fwd else c.player_1_id,
        method="names",
    )


class OverrideFile:
    REQUIRED = (
        "market_slug",
        "score_provider",
        "score_match_id",
        "long_player",
        "short_player",
        "reason",
        "added_by",
    )

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        for e in entries:
            missing = [k for k in self.REQUIRED if not e.get(k)]
            if missing:
                raise ValueError(f"override {e.get('market_slug')} missing {missing}")
        slugs = [e["market_slug"] for e in entries]
        if len(slugs) != len(set(slugs)):
            raise ValueError("duplicate market_slug in overrides")
        self.entries = {e["market_slug"]: e for e in entries}

    @classmethod
    def from_file(cls, path: str | Path) -> OverrideFile:
        p = Path(path)
        if not p.exists():
            return cls([])
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls(list(data.get("overrides") or []))

    def get(self, market: MarketInfo) -> dict[str, Any] | None:
        e = self.entries.get(market.slug)
        if e is None:
            return None
        if not (
            names_match(e["long_player"], market.long.name)
            and names_match(e["short_player"], market.short.name)
        ):
            raise MappingRejected("override player names do not match the market participants")
        return e
