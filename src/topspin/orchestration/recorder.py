"""Record mode: collect and validate data only. No signals, no orders, no credentials."""

from __future__ import annotations

from pathlib import Path

from topspin.config import AppConfig
from topspin.orchestration.runner import Runner, RunStats


async def run_recorder(
    cfg: AppConfig, duration_s: float | None = None, root: Path | None = None
) -> RunStats:
    return await Runner(cfg, "record", root=root).run(duration_s)
