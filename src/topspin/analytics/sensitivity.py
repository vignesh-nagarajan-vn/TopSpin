"""Execution-stress sensitivity: replay the same inputs under harsher assumptions.

A strategy that only works with optimistic latency, fills or slippage should fail
validation, so every report on a candidate edge must include this grid.
"""

from __future__ import annotations

import itertools
from typing import Any

from topspin.analytics.pnl import execution_summary, pnl_summary
from topspin.config import AppConfig
from topspin.discovery.tournaments import TournamentClassifier
from topspin.simulation.replay import replay_records

DEFAULT_GRID: dict[str, tuple[Any, ...]] = {
    "taker_delay_ms": (500, 1500, 3000),
    "paper_fill_fraction": (0.5, 0.25),
    "slippage_stress_ticks": (0, 1),
}


def sensitivity_grid(
    base: AppConfig,
    records: list[dict[str, Any]],
    classifier: TournamentClassifier,
    grid: dict[str, tuple[Any, ...]] | None = None,
) -> list[dict[str, Any]]:
    grid = grid or DEFAULT_GRID
    keys = list(grid)
    bankroll = float(base.risk.bankroll)
    rows = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        sim = dict(zip(keys, combo, strict=True))
        cfg = base.model_copy(update={"simulation": base.simulation.model_copy(update=sim)})
        res = replay_records(cfg, records, classifier)
        pnl = pnl_summary(res.journal.records, bankroll)
        ex = execution_summary(res.journal.records)
        rows.append(
            {
                **sim,
                "orders": ex["orders_submitted"],
                "fill_rate": ex["fill_rate"],
                "round_trips": pnl["round_trips_closed"],
                "net_pnl": pnl["net_realized_pnl"],
                "clustered_ci_low": pnl["match_clustered_mean"].get("ci_low"),
            }
        )
    return rows
