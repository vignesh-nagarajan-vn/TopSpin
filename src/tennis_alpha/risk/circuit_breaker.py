"""Hard circuit breakers and the persistent daily-loss halt latch.

The latch is a JSON file. Once tripped it blocks new entries across restarts and does
*not* reset at midnight. Only ``tennis-alpha reset-halt`` (an interactive, human-operated
command requiring operator name, reason and confirmation of a fresh reconciliation) can
clear it. Automated code paths never call ``HaltLatch.reset``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from tennis_alpha.timeutil import ns_to_iso, utc_day


class HaltLatch:
    def __init__(self, path: str | Path | None) -> None:
        """``path=None`` keeps the latch in memory only (replay / tests); it must never be
        used by a paper or live process."""
        self.path = Path(path) if path is not None else None
        self._state: dict[str, Any] = {"halted": False, "history": []}
        if self.path is not None and self.path.exists():
            self._state = json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def halted(self) -> bool:
        return bool(self._state.get("halted"))

    @property
    def reason(self) -> str | None:
        return self._state.get("reason")

    @property
    def state(self) -> dict[str, Any]:
        return dict(self._state)

    def trip(self, reason: str, ts_ns: int, detail: dict[str, Any] | None = None) -> None:
        if self.halted:
            return
        entry = {"event": "trip", "reason": reason, "at": ns_to_iso(ts_ns), "detail": detail or {}}
        self._state.update({"halted": True, "reason": reason, "tripped_at": ns_to_iso(ts_ns)})
        self._state.setdefault("history", []).append(entry)
        self._write()

    def reset(self, *, operator: str, reason: str, reconciled: bool, ts_ns: int) -> None:
        """Human-only. Called exclusively by the interactive reset CLI."""
        if not operator.strip() or not reason.strip():
            raise ValueError("operator and reason are required")
        if not reconciled:
            raise ValueError("a fresh account/portfolio reconciliation is required before reset")
        entry = {
            "event": "reset",
            "operator": operator,
            "reason": reason,
            "reconciled": True,
            "at": ns_to_iso(ts_ns),
            "previous_reason": self._state.get("reason"),
        }
        self._state.update({"halted": False, "reason": None, "tripped_at": None})
        self._state.setdefault("history", []).append(entry)
        self._write()


@dataclass
class DailyLossMonitor:
    """Trips the latch when conservative equity falls ``frac`` below the day's start."""

    latch: HaltLatch
    frac: float
    day: str | None = None
    start_equity: Decimal | None = None

    def update(self, equity: Decimal, ts_ns: int) -> bool:
        d = utc_day(ts_ns)
        if d != self.day or self.start_equity is None:
            self.day = d
            self.start_equity = equity
        loss = self.start_equity - equity
        if self.start_equity > 0 and loss >= Decimal(str(self.frac)) * self.start_equity:
            self.latch.trip(
                "daily_loss_limit",
                ts_ns,
                {"start_equity": str(self.start_equity), "equity": str(equity), "day": d},
            )
            return True
        return False


@dataclass
class CircuitBreakers:
    """Non-persistent breakers. Global reasons block all entries; match reasons one match."""

    emergency_stop_file: Path | None = None
    global_reasons: set[str] = field(default_factory=set)
    match_reasons: dict[str, set[str]] = field(default_factory=dict)

    def emergency_stop_active(self) -> bool:
        return self.emergency_stop_file is not None and self.emergency_stop_file.exists()

    def trip_global(self, reason: str) -> None:
        self.global_reasons.add(reason)

    def clear_global(self, reason: str) -> None:
        self.global_reasons.discard(reason)

    def block_match(self, match_key: str, reason: str) -> None:
        self.match_reasons.setdefault(match_key, set()).add(reason)

    def unblock_match(self, match_key: str, reason: str) -> None:
        self.match_reasons.get(match_key, set()).discard(reason)

    def entry_block_reasons(self, match_key: str | None = None) -> list[str]:
        reasons = sorted(self.global_reasons)
        if self.emergency_stop_active():
            reasons.append("emergency_stop")
        if match_key is not None:
            reasons.extend(sorted(self.match_reasons.get(match_key, set())))
        return reasons
