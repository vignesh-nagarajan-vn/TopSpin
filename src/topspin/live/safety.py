"""Live-mode preconditions. Every check fails closed.

Live trading requires, all at once:

1. ``operations.mode == "live"`` and ``live.enabled == true`` set by hand in a live config,
2. a positive ``live.max_order_value`` set by hand,
3. API credentials present in the environment (never read from files, never logged),
4. a current, complete operator authorization file (eligibility, jurisdiction, loss
   acknowledgement, reviewed risk settings, the exact order cap, an expiry),
5. computed gate evidence showing Gates 1, 3 and 4 passed,
6. no tripped halt latch and no emergency-stop file.

Nothing in the codebase writes the authorization file or flips these settings.
"""

from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from topspin.config import AppConfig
from topspin.risk.circuit_breaker import HaltLatch

CREDENTIAL_ENV = ("POLYMARKET_KEY_ID", "POLYMARKET_SECRET_KEY")
REQUIRED_STATEMENT = "I authorize live trading with real money and accept that I may lose it."


class LiveNotAuthorized(RuntimeError):
    def __init__(self, failures: list[str]) -> None:
        super().__init__("live trading refused: " + "; ".join(failures))
        self.failures = failures


class LiveAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operator: str
    jurisdiction: str
    exchange_eligibility_confirmed: bool
    api_permissions_confirmed: bool
    reviewed_risk_settings: bool
    understands_potential_losses: bool
    max_order_value: Decimal
    bankroll: Decimal
    authorized_on: date
    valid_until: date
    statement: str


def load_authorization(path: Path) -> LiveAuthorization:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return LiveAuthorization.model_validate(data)


def check_live_preconditions(
    cfg: AppConfig,
    root: Path,
    *,
    env: dict[str, str] | None = None,
    today: date | None = None,
) -> list[str]:
    env = dict(os.environ) if env is None else env
    today = today or date.today()
    failures: list[str] = []
    if cfg.operations.mode != "live":
        failures.append("operations.mode is not 'live'")
    if not cfg.live.enabled:
        failures.append("live.enabled is false")
    if cfg.live.max_order_value <= 0:
        failures.append("live.max_order_value is not positive")
    missing = [k for k in CREDENTIAL_ENV if not env.get(k)]
    if missing:
        failures.append(f"missing credentials in environment: {', '.join(missing)}")

    auth_path = root / cfg.live.authorization_file
    if not auth_path.exists():
        failures.append(f"operator authorization file missing: {cfg.live.authorization_file}")
    else:
        try:
            auth = load_authorization(auth_path)
        except (ValidationError, yaml.YAMLError) as exc:
            failures.append(f"authorization file invalid: {exc.__class__.__name__}")
        else:
            if not all(
                [
                    auth.exchange_eligibility_confirmed,
                    auth.api_permissions_confirmed,
                    auth.reviewed_risk_settings,
                    auth.understands_potential_losses,
                ]
            ):
                failures.append("authorization is missing a required confirmation")
            if auth.statement.strip() != REQUIRED_STATEMENT:
                failures.append("authorization statement does not match the required text")
            if auth.max_order_value != cfg.live.max_order_value:
                failures.append("authorized order cap differs from live.max_order_value")
            if auth.bankroll != cfg.risk.bankroll:
                failures.append("authorized bankroll differs from risk.bankroll")
            if not (auth.authorized_on <= today <= auth.valid_until):
                failures.append("authorization is not currently valid")

    if cfg.live.require_gate_evidence:
        ev_path = root / cfg.live.gate_evidence_file
        if not ev_path.exists():
            failures.append("gate evidence missing (run research reports first)")
        else:
            ev: dict[str, Any] = json.loads(ev_path.read_text(encoding="utf-8"))
            for gate in ("gate1_data_quality", "gate3_replay_profitability", "gate4_forward_paper"):
                if not (ev.get(gate) or {}).get("passed"):
                    failures.append(f"{gate} not passed")

    latch = HaltLatch(root / cfg.risk.halt_latch_path)
    if latch.halted:
        failures.append(f"halt latch is tripped ({latch.reason}); human reset required")
    if (root / cfg.operations.emergency_stop_file).exists():
        failures.append("emergency stop file present")
    return failures


def require_live_authorization(cfg: AppConfig, root: Path) -> None:
    failures = check_live_preconditions(cfg, root)
    if failures:
        raise LiveNotAuthorized(failures)
