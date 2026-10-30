"""Paper mode: live public data, real signals, simulated (pessimistic) execution.

Never loads trading credentials and never constructs an order-capable client.
"""

from __future__ import annotations

from pathlib import Path

from topspin.config import AppConfig
from topspin.orchestration.runner import Runner, RunStats


async def run_paper(
    cfg: AppConfig, duration_s: float | None = None, root: Path | None = None
) -> RunStats:
    return await Runner(cfg, "paper", root=root).run(duration_s)
