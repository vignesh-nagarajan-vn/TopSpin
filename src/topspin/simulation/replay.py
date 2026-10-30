"""Deterministic historical replay.

Replays recorded *input* records (market metadata, books, trades, scores, feed gaps,
settlements, ticks) through the same ``TradingEngine`` used for paper trading, in local
receipt order. Recorded decisions are ignored; they are regenerated. The halt latch is
in-memory so replays can never touch the operator's real latch.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from topspin.config import AppConfig
from topspin.discovery.tournaments import TournamentClassifier
from topspin.orchestration.engine import TradingEngine
from topspin.risk.circuit_breaker import CircuitBreakers, HaltLatch
from topspin.storage.journal import INPUT_KINDS, MemoryJournal, read_journal


@dataclass
class ReplayResult:
    engine: TradingEngine
    journal: MemoryJournal
    inputs: int

    def decisions(self) -> list[dict[str, Any]]:
        return [r["payload"] for r in self.journal.of_kind("decision")]

    def fingerprint(self) -> str:
        """Hash of every regenerated decision, order and fill (determinism check)."""
        h = hashlib.sha256()
        for r in self.journal.records:
            if r["kind"] in ("decision", "order_submitted", "fill", "order_update", "settlement"):
                h.update(
                    json.dumps([r["kind"], r["local_ts_ns"], r["payload"]], sort_keys=True).encode()
                )
        return h.hexdigest()


def replay_records(
    cfg: AppConfig,
    records: Iterable[dict[str, Any]],
    classifier: TournamentClassifier,
) -> ReplayResult:
    out = MemoryJournal()
    engine = TradingEngine(
        cfg, out.write, classifier, mode="replay", latch=HaltLatch(None), breakers=CircuitBreakers()
    )
    n = 0
    for rec in records:
        if rec["kind"] not in INPUT_KINDS:
            continue
        n += 1
        out.write(
            rec["kind"], rec["payload"], rec["local_ts_ns"], source_ts_ns=rec.get("source_ts_ns")
        )
        engine.handle(rec["kind"], rec["payload"], rec["local_ts_ns"])
    return ReplayResult(engine, out, n)


def replay_files(
    cfg: AppConfig, paths: Iterable[str | Path], classifier: TournamentClassifier
) -> ReplayResult:
    return replay_records(cfg, read_journal(paths, kinds=INPUT_KINDS), classifier)
