"""Score-feed interface.

The strategy depends only on ``ScoreSnapshot`` objects. Providers are replaceable:

* ``polymarket_us_event_state`` (implemented) - the Sportradar-derived live state embedded in
  Polymarket US event payloads, polled over the public API. Supplies sets, games, points
  (when the string order can be resolved), current server, period/status and an
  ``updatedAt`` source timestamp. Its latency relative to market repricing is unknown and
  is exactly what the Gate 1 latency study measures.
* A licensed low-latency provider (e.g. Sportradar direct) - interface only; no credentials
  are available, so no integration is claimed.
* Replay of recorded journals.
"""

from __future__ import annotations

from typing import Protocol

from tennis_alpha.models.match import ScoreSnapshot


class ScoreFeed(Protocol):
    provider: str

    async def poll(self, match_keys: list[str]) -> list[ScoreSnapshot]:
        """Return the latest snapshot for each requested match (may be empty)."""
        ...


class UnavailableScoreFeed:
    """Placeholder for a licensed provider with no credentials configured."""

    def __init__(self, provider: str) -> None:
        self.provider = provider

    async def poll(self, match_keys: list[str]) -> list[ScoreSnapshot]:
        raise RuntimeError(
            f"score provider {self.provider!r} is not configured (no license/credentials); "
            "no live scores will be fabricated"
        )
