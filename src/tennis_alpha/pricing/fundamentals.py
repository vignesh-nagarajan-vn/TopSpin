"""Interface for an experimental, market-independent prematch prior.

v1 ships no fundamentals model: no licensed historical player dataset is available, and
fabricating player statistics is prohibited. A future implementation (surface-adjusted
Elo, opponent-adjusted serve/return rates with shrinkage, ...) must implement
``FundamentalsModel`` and is reported *separately* from the market-anchored model. It
must never be blended with live market prices.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

FUNDAMENTALS_MODEL_FAMILY = "fundamentals"


@dataclass(frozen=True, slots=True)
class FundamentalsPrior:
    model_version: str
    p_serve_a: float
    p_serve_b: float
    data_sources: tuple[str, ...]
    as_of: str


class FundamentalsUnavailable(RuntimeError):
    pass


class FundamentalsModel(Protocol):
    model_version: str

    def prior(
        self, player_a_id: str, player_b_id: str, surface: str | None, best_of: int
    ) -> FundamentalsPrior: ...


class NoFundamentalsModel:
    """Placeholder that always declines, so callers must handle absence explicitly."""

    model_version = "none"

    def prior(
        self, player_a_id: str, player_b_id: str, surface: str | None, best_of: int
    ) -> FundamentalsPrior:
        raise FundamentalsUnavailable(
            "no licensed historical tennis dataset configured; fundamentals model disabled"
        )
