"""Async runner for ``record`` and ``paper`` modes.

Pollers fetch public Polymarket US data (no credentials), stamp local receipt times and
push records onto one queue. A single consumer journals every input record and, in paper
mode, feeds it to the deterministic ``TradingEngine`` - so the live paper run and a later
replay of its journal see exactly the same ordered inputs.

This module never imports ``topspin.live`` or the trading SDK.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from topspin.config import AppConfig
from topspin.discovery.markets import discover
from topspin.discovery.tournaments import TournamentClassifier
from topspin.feeds.polymarket_us import (
    ParseError,
    PolymarketUSPublicClient,
    parse_book,
    parse_event_score,
)
from topspin.models.market import MarketInfo
from topspin.models.match import MatchStatus
from topspin.orchestration.engine import TradingEngine
from topspin.risk.circuit_breaker import CircuitBreakers, HaltLatch
from topspin.storage.journal import JournalWriter
from topspin.timeutil import NS_PER_S, mono_ns, now_ns

log = logging.getLogger("topspin.runner")


@dataclass
class RunStats:
    inputs: int = 0
    books: int = 0
    scores: int = 0
    gaps: int = 0
    errors: int = 0
    tracked: set[str] = field(default_factory=set)


class Runner:
    def __init__(
        self, cfg: AppConfig, mode: str, *, run_id: str | None = None, root: Path | None = None
    ) -> None:
        if mode not in ("record", "paper"):
            raise ValueError("runner supports record and paper only")
        if cfg.live.enabled:
            raise RuntimeError("refusing to start: live.enabled is set in a non-live process")
        self.cfg = cfg
        self.mode = mode
        self.root = root or Path.cwd()
        self.run_id = run_id or f"{mode}-{uuid.uuid4().hex[:8]}"
        self.journal = JournalWriter(self.root / cfg.operations.journal_dir, self.run_id)
        self.classifier = TournamentClassifier.from_file(self.root / cfg.discovery.tournaments_file)
        self.client = PolymarketUSPublicClient(
            cfg.exchange.public_base_url,
            timeout_s=cfg.exchange.request_timeout_s,
            per_second=cfg.exchange.max_requests_per_second,
            max_attempts=cfg.exchange.retry_max_attempts,
            backoff_s=cfg.exchange.retry_backoff_s,
        )
        self.queue: asyncio.Queue[tuple[str, dict[str, Any], int, int | None]] = asyncio.Queue()
        self.tracked: dict[str, MarketInfo] = {}  # market slug -> info
        self.ended: dict[str, int] = {}  # market slug -> first time seen ended
        self.stats = RunStats()
        self._last_ts = 0
        self.engine: TradingEngine | None = None
        if mode == "paper":
            latch = HaltLatch(self.root / cfg.risk.halt_latch_path)
            breakers = CircuitBreakers(
                emergency_stop_file=self.root / cfg.operations.emergency_stop_file
            )
            self.engine = TradingEngine(
                cfg, self._sink, self.classifier, mode="paper", latch=latch, breakers=breakers
            )

    def _sink(
        self, kind: str, payload: dict[str, Any], local_ts_ns: int, *, key: str | None = None
    ) -> None:
        self.journal.write(kind, payload, local_ts_ns, key=key)

    async def _put(
        self, kind: str, payload: dict[str, Any], ts: int, source_ts: int | None = None
    ) -> None:
        await self.queue.put((kind, payload, ts, source_ts))

    # ---------------------------------------------------------------- pollers

    async def discovery_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                res = await discover(self.client, self.cfg, self.classifier, now_ns())
                ts = now_ns()
                await self._put(
                    "discovery_summary",
                    {
                        "selected": [m.slug for m in res.selected],
                        "candidates": len(res.candidates),
                        "rejected": res.rejected[:200],
                    },
                    ts,
                )
                cap = self.cfg.discovery.max_tracked_matches
                for mi in res.selected:
                    prev = self.tracked.get(mi.slug)
                    active = [s for s in self.tracked if s not in self.ended]
                    if prev is None and len(active) >= cap:
                        continue  # keep following matches already tracked until they end
                    if prev is None or prev.model_dump(
                        exclude={"discovered_local_ts_ns", "best_bid", "best_ask", "volume"}
                    ) != mi.model_dump(
                        exclude={"discovered_local_ts_ns", "best_bid", "best_ask", "volume"}
                    ):
                        await self._put("market_info", mi.model_dump(mode="json"), now_ns())
                    self.tracked[mi.slug] = mi
            except Exception as exc:  # network / parse errors must not kill the run
                self.stats.errors += 1
                await self._put(
                    "health", {"component": "discovery", "error": repr(exc)[:300]}, now_ns()
                )
            await _sleep(stop, self.cfg.discovery.refresh_interval_s)

    async def score_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            for slug, mi in list(self.tracked.items()):
                if slug in self.ended and now_ns() - self.ended[slug] > 600 * NS_PER_S:
                    continue
                try:
                    ev, cache = await self.client.fetch_event(mi.event_slug)
                    ts = now_ns()
                    snap = parse_event_score(ev, ts, cache)
                    await self._put("score", snap.model_dump(mode="json"), ts, snap.source_ts_ns)
                    if snap.status.is_terminal or snap.status == MatchStatus.RETIRED:
                        self.ended.setdefault(slug, ts)
                except (ParseError, LookupError) as exc:
                    await self._put(
                        "health",
                        {
                            "component": "score",
                            "match_key": mi.event_slug,
                            "error": repr(exc)[:300],
                        },
                        now_ns(),
                    )
                except Exception as exc:
                    self.stats.errors += 1
                    await self._put(
                        "health",
                        {
                            "component": "score",
                            "match_key": mi.event_slug,
                            "error": repr(exc)[:300],
                        },
                        now_ns(),
                    )
            await _sleep(stop, self.cfg.feeds.score_poll_interval_s)

    async def book_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            for slug in list(self.tracked):
                if slug in self.ended:
                    continue
                t0 = mono_ns()
                try:
                    raw, cache = await self.client.fetch_book(slug)
                    ts = now_ns()
                    snap = parse_book(raw, ts, cache=cache)
                    payload = snap.model_dump(mode="json")
                    payload["request_latency_ms"] = (mono_ns() - t0) / 1e6
                    await self._put("book", payload, ts, snap.transact_time_ns)
                except Exception as exc:
                    self.stats.gaps += 1
                    await self._put(
                        "feed_gap", {"market_slug": slug, "reason": repr(exc)[:200]}, now_ns()
                    )
            await _sleep(stop, self.cfg.feeds.book_poll_interval_s)

    async def settlement_loop(self, stop: asyncio.Event) -> None:
        done: set[str] = set()
        while not stop.is_set():
            for slug in list(self.ended):
                if slug in done:
                    continue
                with contextlib.suppress(Exception):
                    px = await self.client.get_settlement(slug)
                    if px is not None:
                        await self._put(
                            "settlement",
                            {
                                "market_slug": slug,
                                "yes_price": str(px),
                                "source": "exchange_settlement_endpoint",
                            },
                            now_ns(),
                        )
                        done.add(slug)
            await _sleep(stop, 60)

    async def tick_loop(self, stop: asyncio.Event) -> None:
        last = now_ns()
        while not stop.is_set():
            ts = now_ns()
            for kind, payload in suspension_records(last, ts):
                await self._put(kind, payload, ts)
            last = ts
            await self._put("tick", {}, ts)
            await _sleep(stop, 1.0)

    # ---------------------------------------------------------------- consumer

    async def consumer(self, stop: asyncio.Event) -> None:
        while not (stop.is_set() and self.queue.empty()):
            try:
                kind, payload, ts, source_ts = await asyncio.wait_for(self.queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            if ts <= self._last_ts:
                payload = {**payload, "receipt_ts_ns": ts}
                ts = self._last_ts + 1  # keep the journal strictly ordered by local time
            self._last_ts = ts
            self.stats.inputs += 1
            if kind == "book":
                self.stats.books += 1
            elif kind == "score":
                self.stats.scores += 1
            self.journal.write(kind, payload, ts, source_ts_ns=source_ts)
            if self.engine is not None and kind in (
                "market_info",
                "book",
                "score",
                "feed_gap",
                "settlement",
                "tick",
            ):
                try:
                    self.engine.handle(kind, payload, ts)
                except Exception as exc:
                    # Fail closed: an engine error halts new entries for the rest of the run.
                    self.engine.breakers.trip_global("engine_error")
                    self.journal.write(
                        "health", {"component": "engine", "error": repr(exc)[:500]}, ts
                    )
                    log.exception("engine error")
            if self.stats.inputs % 200 == 0:
                self.journal.flush()

    async def run(self, duration_s: float | None = None) -> RunStats:
        stop = asyncio.Event()
        self.journal.write(
            "run_start",
            {"run_id": self.run_id, "mode": self.mode, "config": self.cfg.model_dump(mode="json")},
            now_ns(),
        )
        tasks = [
            asyncio.create_task(self.discovery_loop(stop)),
            asyncio.create_task(self.score_loop(stop)),
            asyncio.create_task(self.book_loop(stop)),
            asyncio.create_task(self.settlement_loop(stop)),
            asyncio.create_task(self.tick_loop(stop)),
        ]
        consumer = asyncio.create_task(self.consumer(stop))
        try:
            if duration_s is not None:
                await asyncio.sleep(duration_s)
            else:
                await asyncio.Event().wait()
        finally:
            stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await consumer
            summary: dict[str, Any] = {
                "run_id": self.run_id,
                "inputs": self.stats.inputs,
                "books": self.stats.books,
                "scores": self.stats.scores,
                "gaps": self.stats.gaps,
                "errors": self.stats.errors,
                "tracked": sorted(self.tracked),
            }
            if self.engine is not None:
                summary["ledger"] = self.engine.ledger_summary()
            self.journal.write("run_end", summary, max(now_ns(), self._last_ts + 1))
            self.journal.close()
            await self.client.aclose()
        self.stats.tracked = set(self.tracked)
        return self.stats


SUSPENSION_GAP_S = 5.0


def suspension_records(prev_tick_ns: int, now_tick_ns: int) -> list[tuple[str, dict[str, Any]]]:
    """Records to journal when the 1 s tick loop was silent for too long, which means the
    process or machine was suspended (e.g. laptop sleep). Every book is then distrusted
    until a fresh snapshot arrives, and the gap is visible in the data-quality report."""
    gap_s = (now_tick_ns - prev_tick_ns) / NS_PER_S
    if gap_s <= SUSPENSION_GAP_S:
        return []
    return [
        (
            "health",
            {"component": "runner", "event": "suspension_detected", "gap_s": round(gap_s, 1)},
        ),
        ("feed_gap", {"market_slug": "", "reason": f"process_suspended_{gap_s:.0f}s"}),
    ]


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
