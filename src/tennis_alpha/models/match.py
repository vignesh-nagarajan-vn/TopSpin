"""Normalized, provider-independent live tennis score schema."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class MatchStatus(StrEnum):
    NOT_STARTED = "not_started"
    LIVE = "live"
    SUSPENDED = "suspended"  # weather / interruption / medical where reported
    FINISHED = "finished"
    RETIRED = "retired"  # finished early: retirement, default, disqualification
    WALKOVER = "walkover"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self in (
            MatchStatus.FINISHED,
            MatchStatus.RETIRED,
            MatchStatus.WALKOVER,
            MatchStatus.CANCELLED,
        )


class FeedCapabilities(BaseModel):
    """What a payload *actually* contained. Never inferred beyond the payload."""

    sets: bool = False
    games: bool = False
    points: bool = False
    server: bool = False
    status: bool = False
    source_timestamp: bool = False
    corrections: bool = False


class ScoreSnapshot(BaseModel):
    """One observation of a match's full score, keyed by provider competitor ID.

    ``sets`` holds games per set, in order; the last entry may be the set in progress.
    ``points`` holds the raw point tokens for the current game ("0", "15", "30", "40",
    "A"/"AD") or tiebreak point counts, keyed by competitor ID.
    """

    match_key: str
    provider: str
    provider_event_id: str | None = None
    status: MatchStatus
    period: str | None = None
    competitor_ids: tuple[str, str]
    sets: list[dict[str, int]] = Field(default_factory=list)
    tiebreaks: list[dict[str, int] | None] = Field(default_factory=list)
    points: dict[str, str] | None = None
    server_id: str | None = None
    ended: bool = False
    source_ts_ns: int | None = None
    local_ts_ns: int
    mono_ns: int = 0
    raw_score: str | None = None
    tournament_name: str | None = None
    round_name: str | None = None
    capabilities: FeedCapabilities = Field(default_factory=FeedCapabilities)
    quality_flags: list[str] = Field(default_factory=list)
