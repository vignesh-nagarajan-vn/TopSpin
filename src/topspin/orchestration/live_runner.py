"""Live runner: startup safety sequence and the human kill switch.

v1 status: the startup checks, reconciliation, scoped cancellation and kill switch are
implemented and tested against a fake exchange client. Routing engine decisions to the
exchange is intentionally *not* enabled: no research gate has passed, so there is no
evidence-backed strategy to run with real money. ``run_live`` therefore always stops after
the startup sequence unless the preconditions pass, and even then refuses to trade until
a live order router is reviewed and enabled in a later, separately approved change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from topspin.config import AppConfig
from topspin.live.execution import LiveExecutionAdapter, OrderStore, SdkOrderClient
from topspin.live.reconciliation import ReconciliationReport, reconcile_and_cancel_bot_orders
from topspin.live.safety import check_live_preconditions
from topspin.risk.circuit_breaker import HaltLatch
from topspin.timeutil import now_ns


@dataclass
class StartupResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    reconciliation: ReconciliationReport | None = None


async def live_startup(
    cfg: AppConfig, root: Path, adapter: LiveExecutionAdapter | None = None
) -> StartupResult:
    failures = check_live_preconditions(cfg, root)
    if failures:
        return StartupResult(False, failures)
    if adapter is None:
        client = SdkOrderClient(
            os.environ["POLYMARKET_KEY_ID"], os.environ["POLYMARKET_SECRET_KEY"]
        )
        adapter = LiveExecutionAdapter(
            client, OrderStore(root / "state" / "live_orders.jsonl"), cfg.live.max_order_value
        )
    try:
        await adapter.client.balances()  # authentication + account access
    except Exception as exc:
        return StartupResult(
            False, [f"authentication/account check failed: {exc.__class__.__name__}"]
        )
    rep = await reconcile_and_cancel_bot_orders(adapter)
    if not rep.clean:
        latch = HaltLatch(root / cfg.risk.halt_latch_path)
        latch.trip(
            "live_startup_reconciliation_failed",
            now_ns(),
            {
                "cancel_failed": rep.cancel_failed,
                "unresolved": rep.unresolved_intents,
                "errors": rep.errors,
            },
        )
        return StartupResult(False, ["reconciliation not clean; halt latch tripped"], rep)
    return StartupResult(True, [], rep)


async def kill_switch(
    cfg: AppConfig, root: Path, adapter: LiveExecutionAdapter, reason: str
) -> ReconciliationReport:
    """Human-operated: block entries (latch + stop file), cancel bot orders, report the
    rest. Never submits liquidation orders."""
    HaltLatch(root / cfg.risk.halt_latch_path).trip(f"kill_switch:{reason}", now_ns())
    stop = root / cfg.operations.emergency_stop_file
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text(reason, encoding="utf-8")
    return await reconcile_and_cancel_bot_orders(adapter)


async def run_live(cfg: AppConfig, root: Path) -> StartupResult:
    res = await live_startup(cfg, root)
    if res.ok:
        res.failures.append(
            "startup checks passed, but live order routing is not enabled in v1 "
            "(no research gate has produced evidence of an edge)"
        )
        res.ok = False
    return res
