"""The deterministic trading engine shared by paper trading and historical replay.

It consumes input records strictly in local-receipt order and uses *only* their
timestamps as the clock, so the same inputs, code and configuration always produce the
same decisions. It never touches the network and never imports the live adapter.

Input record kinds: ``market_info``, ``book``, ``trade``, ``score``, ``feed_gap``,
``settlement``, ``tick``. Everything it decides is written to the journal sink.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from typing import Any

from topspin.config import AppConfig
from topspin.discovery.matching import MappingRejected, MatchLink, link_same_payload
from topspin.discovery.tournaments import Classification, TournamentClassifier
from topspin.feeds.market_feed import MarketBookTracker
from topspin.models.market import (
    BookSnapshot,
    MarketInfo,
    MarketState,
    OrderBookView,
    TradePrint,
)
from topspin.models.match import MatchStatus, ScoreSnapshot
from topspin.models.orders import Order, OrderType, TimeInForce
from topspin.models.positions import Ledger
from topspin.pricing.calibration import CalibrationError
from topspin.pricing.markov import InvalidStateError
from topspin.pricing.rulesets import Ruleset
from topspin.pricing.uncertainty import AnchoredMatchModel, ProbEstimate
from topspin.risk.circuit_breaker import CircuitBreakers, DailyLossMonitor, HaltLatch
from topspin.risk.limits import RiskEngine
from topspin.simulation.broker import PaperBroker
from topspin.simulation.fees import FeeModel
from topspin.strategy.edge import (
    SizingInputs,
    best_entry,
    evaluate_side,
    expected_remaining_games,
)
from topspin.strategy.events import (
    MatchEventDetector,
    OrientationError,
    OrientedScore,
    TennisEvent,
    TennisEventKind,
    orient,
)
from topspin.strategy.exit_policy import evaluate_exit, liquidation_value
from topspin.strategy.signals import Action, Decision, Reason
from topspin.timeutil import NS_PER_S

Sink = Callable[..., Any]

_ENTRY_EVENT = {"break": TennisEventKind.BREAK, "set_complete": TennisEventKind.SET_COMPLETE}
_STOP_EVENTS = {
    TennisEventKind.SUSPENSION: "match_suspended",
    TennisEventKind.RETIREMENT: "match_retired",
    TennisEventKind.WALKOVER: "match_walkover",
    TennisEventKind.CANCELLED: "match_cancelled",
    TennisEventKind.MATCH_COMPLETE: "match_over",
}


@dataclass
class Anchor:
    prob: float
    half_spread: float
    depth: float
    book_ts_ns: int
    frozen_ts_ns: int | None = None


@dataclass
class MatchContext:
    market: MarketInfo
    tracker: MarketBookTracker
    fee_model: FeeModel
    classification: Classification | None = None
    classified_name: str | None = None
    detector: MatchEventDetector | None = None
    link: MatchLink | None = None
    status: MatchStatus = MatchStatus.UNKNOWN
    seen_not_started: bool = False
    first_seen_in_play: bool = False
    candidates: list[Anchor] = field(default_factory=list)
    confirmed_until_ns: int | None = None  # receipt time of the latest "not started" snapshot
    anchor: Anchor | None = None
    anchor_error: str | None = None
    model: AnchoredMatchModel | None = None
    model_error: str | None = None
    oriented: OrientedScore | None = None
    estimate: ProbEstimate | None = None
    last_score_ts: int | None = None  # when the newest score data was current, not its receipt
    last_source_ts: int | None = None
    pending_as_of: int | None = None
    pending_event: TennisEvent | None = None
    pending_deadline: int | None = None
    pending_attempts: int = 0
    acted_events: set[str] = field(default_factory=set)
    last_exit_ts: int | None = None
    settled: bool = False

    @property
    def key(self) -> str:
        return self.market.match_key

    @property
    def ruleset(self) -> Ruleset | None:
        return (
            self.classification.ruleset
            if self.classification and self.classification.supported
            else None
        )


class TradingEngine:
    def __init__(
        self,
        cfg: AppConfig,
        sink: Sink,
        classifier: TournamentClassifier,
        *,
        mode: str = "paper",
        latch: HaltLatch | None = None,
        breakers: CircuitBreakers | None = None,
    ) -> None:
        if mode not in ("record", "paper", "replay"):
            raise ValueError(f"engine mode {mode!r} (live uses a separate runner)")
        self.cfg = cfg
        self.sink = sink
        self.classifier = classifier
        self.mode = mode
        self.now_ns = 0
        self.contexts: dict[str, MatchContext] = {}
        self.markets: dict[str, MarketInfo] = {}
        self.ledger = Ledger(starting_cash=cfg.risk.bankroll, cash=cfg.risk.bankroll)
        self.latch = latch or HaltLatch(cfg.risk.halt_latch_path)
        self.breakers = breakers or CircuitBreakers()
        self.risk = RiskEngine(cfg.risk, self.latch, self.breakers)
        self.daily = DailyLossMonitor(self.latch, cfg.risk.daily_loss_halt_frac)
        self.base_fee = FeeModel(taker_theta=cfg.fees.taker_theta)
        self.broker: PaperBroker | None = None
        if mode != "record":
            self.broker = PaperBroker(
                cfg.simulation,
                self.ledger,
                fee_model_for=self._fee_model_for,
                market_for=self.markets.get,
                max_book_staleness_s=cfg.feeds.max_book_staleness_s,
                unknown_cache_age_s=cfg.feeds.unknown_cache_age_s,
                sink=self._broker_event,
            )
        self._decision_seq = 0
        self._order_seq = 0

    # ------------------------------------------------------------------ plumbing

    def _emit(self, kind: str, payload: dict[str, Any], key: str | None = None) -> None:
        self.sink(kind, payload, self.now_ns, key=key)

    def _broker_event(self, kind: str, payload: dict[str, object]) -> None:
        self._emit(kind, dict(payload))
        if kind == "fill":
            self._emit("ledger", self.ledger_summary())

    def _fee_model_for(self, slug: str) -> FeeModel:
        mi = self.markets.get(slug)
        return self.base_fee.for_market(
            mi.fee_coefficient if mi else None, self.cfg.fees.use_market_fee_coefficient
        )

    def _decision(self, ctx: MatchContext, action: Action, reason: Reason, **kw: Any) -> Decision:
        self._decision_seq += 1
        raw = f"{ctx.key}|{self.now_ns}|{self._decision_seq}"
        d = Decision(
            decision_id="d-" + hashlib.sha1(raw.encode()).hexdigest()[:16],
            local_ts_ns=self.now_ns,
            match_key=ctx.key,
            market_slug=ctx.market.slug,
            action=action,
            reason=reason,
            **kw,
        )
        self._emit("decision", d.model_dump(mode="json"))
        return d

    def ledger_summary(self) -> dict[str, Any]:
        return {
            "cash": str(self.ledger.cash),
            "realized_pnl": str(self.ledger.realized_pnl),
            "settled_pnl": str(self.ledger.settled_pnl),
            "total_fees": str(self.ledger.total_fees),
            "open_positions": [p.model_dump(mode="json") for p in self.ledger.open_positions()],
            "equity_conservative": str(self.equity()),
        }

    def equity(self) -> Decimal:
        total = self.ledger.cash
        for pos in self.ledger.open_positions():
            ctx = self.contexts.get(pos.match_key)
            view = ctx.tracker.view if ctx else None
            total += liquidation_value(pos, view, self._fee_model_for(pos.market_slug))
        return total

    def open_orders(self) -> list[Order]:
        return self.broker.open_orders() if self.broker else []

    # ------------------------------------------------------------------ dispatch

    def handle(self, kind: str, payload: dict[str, Any], local_ts_ns: int) -> None:
        if local_ts_ns < self.now_ns:
            raise ValueError("records must be processed in local-receipt order")
        self.now_ns = local_ts_ns
        if self.broker:
            self.broker.advance(local_ts_ns)
        if kind == "market_info":
            self.on_market_info(MarketInfo.model_validate(payload))
        elif kind == "book":
            self.on_book(BookSnapshot.model_validate(payload))
        elif kind == "trade":
            if self.broker:
                self.broker.on_trade(TradePrint.model_validate(payload))
        elif kind == "score":
            self.on_score(ScoreSnapshot.model_validate(payload))
        elif kind == "feed_gap":
            self.on_feed_gap(
                str(payload.get("market_slug") or ""), str(payload.get("reason", "gap"))
            )
        elif kind == "settlement":
            self.on_settlement(
                str(payload["market_slug"]),
                Decimal(str(payload["yes_price"])),
                str(payload.get("source", "")),
            )
        elif kind == "tick":
            self.on_tick()

    # ------------------------------------------------------------------ handlers

    def on_market_info(self, mi: MarketInfo) -> None:
        self.markets[mi.slug] = mi
        ctx = self.contexts.get(mi.match_key)
        if ctx is None:
            if mi.kind.set_number is not None:
                return  # set-winner trading is disabled in v1 (no verified mapping yet)
            ctx = MatchContext(
                market=mi,
                tracker=MarketBookTracker(
                    mi.slug, self.cfg.feeds.max_book_staleness_s, self.cfg.feeds.unknown_cache_age_s
                ),
                fee_model=self._fee_model_for(mi.slug),
            )
            self.contexts[mi.match_key] = ctx
            if mi.fee_coefficient is not None and mi.fee_coefficient != self.cfg.fees.taker_theta:
                self._emit(
                    "fee_notice",
                    {
                        "market_slug": mi.slug,
                        "market_theta": str(mi.fee_coefficient),
                        "default_theta": str(self.cfg.fees.taker_theta),
                    },
                )
        else:
            if ctx.market.slug == mi.slug:
                if (mi.tick_size, mi.min_qty) != (ctx.market.tick_size, ctx.market.min_qty):
                    self._emit(
                        "market_increment_change",
                        {
                            "market_slug": mi.slug,
                            "tick": str(mi.tick_size),
                            "min_qty": str(mi.min_qty),
                        },
                    )
                ctx.market = mi
                ctx.fee_model = self._fee_model_for(mi.slug)
        if (
            mi.state not in (MarketState.OPEN, MarketState.PREOPEN, MarketState.UNKNOWN)
            or mi.closed
        ):
            self.breakers.block_match(mi.match_key, "market_not_open")
        else:
            self.breakers.unblock_match(mi.match_key, "market_not_open")

    def on_feed_gap(self, slug: str, reason: str) -> None:
        for ctx in self.contexts.values():
            if not slug or ctx.market.slug == slug:
                ctx.tracker.mark_gap(reason)

    def on_book(self, snap: BookSnapshot) -> None:
        mi = self.markets.get(snap.market_slug)
        if mi is None:
            return
        ctx = self.contexts.get(mi.match_key)
        if self.broker:
            self.broker.on_book(snap)
        if ctx is None or ctx.market.slug != snap.market_slug:
            return
        res = ctx.tracker.apply(snap)
        if not res.applied and res.reason.startswith("invalid"):
            self._emit("book_invalid", {"market_slug": snap.market_slug, "reason": res.reason})
        if ctx.tracker.market_state != MarketState.OPEN:
            self.breakers.block_match(ctx.key, "book_not_open")
        else:
            self.breakers.unblock_match(ctx.key, "book_not_open")
        if res.applied and ctx.status == MatchStatus.NOT_STARTED:
            self._consider_anchor_candidate(ctx, snap)
        if self.mode == "record":
            return
        self._check_daily_loss()
        if ctx.pending_event is not None:
            self._try_entry(ctx)
        self._manage_positions(ctx, on_score=False)

    def on_tick(self) -> None:
        for ctx in self.contexts.values():
            if ctx.status == MatchStatus.LIVE and ctx.last_score_ts is not None:
                stale = (
                    self.now_ns - ctx.last_score_ts
                    > self.cfg.feeds.max_score_staleness_s * NS_PER_S
                )
                if stale:
                    self.breakers.block_match(ctx.key, "score_stale")
            if (
                ctx.pending_event is not None
                and ctx.pending_deadline is not None
                and self.now_ns > ctx.pending_deadline
            ):
                self._expire_pending(ctx)
        if self.mode != "record":
            self._check_daily_loss()

    def on_settlement(self, slug: str, yes_price: Decimal, source: str) -> None:
        mi = self.markets.get(slug)
        if mi is None:
            return
        ctx = self.contexts.get(mi.match_key)
        if ctx is not None:
            ctx.settled = True
            self.breakers.block_match(ctx.key, "settled")
        if self.broker:
            self.broker.settle(slug, yes_price, self.now_ns)
            self._emit("ledger", self.ledger_summary())
        self._emit(
            "settlement_applied",
            {"market_slug": slug, "yes_price": str(yes_price), "source": source},
        )

    # ------------------------------------------------------------------ scores

    def on_score(self, snap: ScoreSnapshot) -> None:
        ctx = self.contexts.get(snap.match_key)
        if ctx is None:
            return
        if (
            ctx.last_source_ts is not None
            and snap.source_ts_ns is not None
            and snap.source_ts_ns < ctx.last_source_ts
        ):
            # Different cache nodes can answer out of order. An older provider state is not
            # a score correction; a real correction carries a newer timestamp.
            self._emit("score_out_of_order", {"match_key": ctx.key, "raw": snap.raw_score})
            return
        if snap.source_ts_ns is not None:
            ctx.last_source_ts = snap.source_ts_ns
        as_of = snap.as_of_ns(self.cfg.feeds.unknown_cache_age_s)
        ctx.last_score_ts = max(as_of, ctx.last_score_ts or as_of)
        if self.now_ns - ctx.last_score_ts > self.cfg.feeds.max_score_staleness_s * NS_PER_S:
            self.breakers.block_match(ctx.key, "score_stale")
        else:
            self.breakers.unblock_match(ctx.key, "score_stale")
        prev_status = ctx.status

        if ctx.link is None:
            try:
                ctx.link = link_same_payload(ctx.market, snap)
                self._emit("mapping", {"link": asdict(ctx.link)})
            except MappingRejected as exc:
                self.breakers.block_match(ctx.key, "mapping_rejected")
                self._emit("mapping_rejected", {"match_key": ctx.key, "reason": str(exc)})
                return

        if snap.tournament_name and snap.tournament_name != ctx.classified_name:
            if ctx.classified_name is not None:
                # The competition must not change mid-match; treat it as a data anomaly.
                self.breakers.block_match(ctx.key, "tournament_changed")
            else:
                ctx.classified_name = snap.tournament_name
                ctx.classification = self.classifier.classify(
                    ctx.market.tour, snap.tournament_name, snap.round_name
                )
                self._emit(
                    "classification",
                    {
                        "match_key": ctx.key,
                        "tournament": snap.tournament_name,
                        "round": snap.round_name,
                        "supported": ctx.classification.supported,
                        "reason": ctx.classification.reason,
                        "ruleset": ctx.ruleset.key if ctx.ruleset else None,
                    },
                )
                if not ctx.classification.supported:
                    self.breakers.block_match(ctx.key, "tournament_unsupported")

        # Status bookkeeping for the anchor (works before a ruleset is known).
        if snap.status == MatchStatus.NOT_STARTED:
            if prev_status in (MatchStatus.UNKNOWN, MatchStatus.NOT_STARTED):
                ctx.seen_not_started = True
                ctx.confirmed_until_ns = max(as_of, ctx.confirmed_until_ns or as_of)
        elif prev_status == MatchStatus.UNKNOWN and not ctx.seen_not_started:
            ctx.first_seen_in_play = True
        if (
            snap.status != MatchStatus.NOT_STARTED
            and ctx.anchor is None
            and ctx.anchor_error is None
        ):
            self._freeze_anchor(ctx)
        ctx.status = snap.status

        rs = ctx.ruleset
        if rs is None:
            return
        if ctx.detector is None:
            ctx.detector = MatchEventDetector(ctx.key, rs)
        try:
            oriented = orient(snap, ctx.market.long.team_id, ctx.market.short.team_id, rs)
        except OrientationError as exc:
            self.breakers.block_match(ctx.key, "score_invalid")
            self._emit(
                "score_invalid", {"match_key": ctx.key, "reason": str(exc), "raw": snap.raw_score}
            )
            return
        self.breakers.unblock_match(ctx.key, "score_invalid")
        changed = (
            ctx.oriented is None
            or not ctx.oriented.same_score(oriented)
            or ctx.oriented.server != oriented.server
        )
        events = ctx.detector.update(oriented)
        ctx.oriented = oriented
        for ev in events:
            self._emit("tennis_event", ev.model_dump(mode="json"), key=ev.dedupe_key)
        self._apply_events(ctx, events)

        if ctx.detector.reliable:
            self.breakers.unblock_match(ctx.key, "score_unreliable")
        else:
            self.breakers.block_match(ctx.key, "score_unreliable")

        if self.mode == "record":
            return
        self._ensure_model(ctx)
        if changed:
            self._update_estimate(ctx)
        for ev in events:
            enabled = {_ENTRY_EVENT[e] for e in self.cfg.strategy.enabled_entry_events}
            if ev.kind in enabled:
                if ev.actionable:
                    ctx.pending_event = ev
                    # The window runs from when the data was current, not when it reached
                    # us: an event that sat in a cache may already be too old to act on.
                    ctx.pending_as_of = as_of
                    ctx.pending_deadline = as_of + int(self.cfg.strategy.entry_window_s * NS_PER_S)
                    ctx.pending_attempts = 0
                else:
                    if oriented.match_over or oriented.status != MatchStatus.LIVE:
                        why = Reason.MATCH_NOT_LIVE
                    elif ev.detail.get("gap") == "true":
                        why = Reason.SCORE_GAP
                    elif not ctx.detector.server_reliable:
                        why = Reason.SERVER_UNKNOWN
                    else:
                        why = Reason.SCORE_UNRELIABLE
                    self._decision(
                        ctx,
                        Action.SKIP,
                        why,
                        trigger=ev.dedupe_key,
                        detail=f"non-actionable {ev.kind.value}",
                    )
        if ctx.pending_event is not None:
            self._try_entry(ctx)
        self._manage_positions(ctx, on_score=changed)

    def _apply_events(self, ctx: MatchContext, events: list[TennisEvent]) -> None:
        for ev in events:
            if ev.kind == TennisEventKind.CORRECTION:
                self._cancel_match_orders(ctx, "score_correction")
                ctx.pending_event = None
                self._emit(
                    "decisions_invalidated",
                    {
                        "match_key": ctx.key,
                        "after_ts_ns": ev.local_ts_ns,
                        "reason": "score_correction",
                    },
                )
            elif ev.kind in _STOP_EVENTS:
                self.breakers.block_match(ctx.key, _STOP_EVENTS[ev.kind])
                self._cancel_match_orders(ctx, _STOP_EVENTS[ev.kind])
                ctx.pending_event = None
                if (
                    ev.kind == TennisEventKind.MATCH_COMPLETE
                    and ev.player in ("A", "B")
                    and not ctx.settled
                ):
                    # Provisional score-derived settlement for paper accounting; an official
                    # settlement record, when one arrives, is applied idempotently later.
                    self.on_settlement(
                        ctx.market.slug,
                        Decimal(1) if ev.player == "A" else Decimal(0),
                        "score_derived",
                    )
            elif ev.kind == TennisEventKind.RESUMPTION:
                self.breakers.unblock_match(ctx.key, "match_suspended")

    # ------------------------------------------------------------------ anchor and model

    def _consider_anchor_candidate(self, ctx: MatchContext, snap: BookSnapshot) -> None:
        view = ctx.tracker.view
        mc = self.cfg.model
        if view is None or not view.two_sided or snap.state != MarketState.OPEN:
            return
        spread = float(view.spread)  # type: ignore[arg-type]
        mid = float(view.mid)  # type: ignore[arg-type]
        within = ctx.market.tick_size * 2
        depth = float(min(view.depth("bid", within), view.depth("ask", within)))
        if (
            spread > mc.anchor_max_spread
            or depth < mc.anchor_min_depth
            or not (mc.anchor_min_prob <= mid <= mc.anchor_max_prob)
        ):
            return
        ctx.candidates.append(
            Anchor(
                prob=mid,
                half_spread=spread / 2,
                depth=depth,
                book_ts_ns=snap.as_of_ns(self.cfg.feeds.unknown_cache_age_s),
            )
        )
        ctx.candidates = ctx.candidates[-50:]

    def _freeze_anchor(self, ctx: MatchContext) -> None:
        mc = self.cfg.model
        latest_ok = self.now_ns - int(mc.anchor_min_lead_s * NS_PER_S)
        eligible = [
            c
            for c in ctx.candidates
            if ctx.confirmed_until_ns is not None
            and c.book_ts_ns <= ctx.confirmed_until_ns
            and c.book_ts_ns <= latest_ok
        ]
        if ctx.first_seen_in_play or not ctx.seen_not_started:
            ctx.anchor_error = "first_observed_after_start"
        elif not eligible:
            ctx.anchor_error = "no_valid_prematch_book"
        elif self.now_ns - eligible[-1].book_ts_ns > mc.anchor_max_age_s * NS_PER_S:
            ctx.anchor_error = "prematch_book_too_old"
        else:
            ctx.anchor = eligible[-1]
            ctx.anchor.frozen_ts_ns = self.now_ns
        payload: dict[str, Any] = {
            "match_key": ctx.key,
            "market_slug": ctx.market.slug,
            "error": ctx.anchor_error,
        }
        if ctx.anchor:
            payload.update(
                prob=ctx.anchor.prob,
                half_spread=ctx.anchor.half_spread,
                depth=ctx.anchor.depth,
                book_ts_ns=ctx.anchor.book_ts_ns,
            )
        self._emit("anchor", payload, key=f"anchor:{ctx.key}")
        if ctx.anchor_error:
            self.breakers.block_match(ctx.key, "no_anchor")

    def _ensure_model(self, ctx: MatchContext) -> None:
        if (
            ctx.model is not None
            or ctx.model_error is not None
            or ctx.anchor is None
            or ctx.ruleset is None
        ):
            return
        mc = self.cfg.model
        base = mc.serve_baseline.get(ctx.market.tour)
        if base is None:
            ctx.model_error = f"no serve baseline for {ctx.market.tour}"
        else:
            try:
                ctx.model = AnchoredMatchModel(
                    ctx.anchor.prob,
                    ctx.anchor.half_spread,
                    ctx.ruleset,
                    base,
                    base_delta=mc.serve_baseline_delta,
                    floor=mc.uncertainty_floor,
                    p_min=mc.serve_p_min,
                    p_max=mc.serve_p_max,
                    tolerance=mc.calibration_tolerance,
                )
            except (CalibrationError, InvalidStateError) as exc:
                ctx.model_error = str(exc)
        payload: dict[str, Any] = {
            "match_key": ctx.key,
            "error": ctx.model_error,
            "model_version": mc.model_version,
            "ruleset": ctx.ruleset.key,
        }
        if ctx.model:
            c = ctx.model.central
            payload.update(
                anchor=c.anchor,
                base=c.base,
                theta=c.theta,
                p_a=c.p_a,
                p_b=c.p_b,
                reproduced=c.reproduced,
                abs_error=c.abs_error,
                scenarios=[[s.anchor, s.base, s.p_a, s.p_b] for s in ctx.model.scenarios],
            )
        self._emit("calibration", payload, key=f"calibration:{ctx.key}")
        if ctx.model_error:
            self.breakers.block_match(ctx.key, "calibration_failed")

    def _update_estimate(self, ctx: MatchContext) -> None:
        ctx.estimate = None
        o = ctx.oriented
        if ctx.model is None or o is None or o.match_over or o.status != MatchStatus.LIVE:
            return
        server = ctx.detector.current_server() if ctx.detector else None
        flags = list(o.flags)
        if o.points_a is None:
            flags.append("points_unknown_priced_at_game_start")
        payload: dict[str, Any] = {
            "match_key": ctx.key,
            "market_slug": ctx.market.slug,
            "model_version": self.cfg.model.model_version,
            "ruleset": ctx.model.ruleset.key,
            "score": o.describe(),
            "anchor": ctx.model.anchor,
            "serve_probs": list(ctx.model.serve_probs),
            "source_ts_ns": o.source_ts_ns,
            "local_ts_ns": o.local_ts_ns,
            "flags": flags,
        }
        if server is None:
            payload.update(valid=False, invalid_reason="server_unknown_or_unverified")
            self._emit("model", payload)
            return
        try:
            state = replace(o.to_state(server), server=server)
            est = ctx.model.match_estimate(state)
        except InvalidStateError as exc:
            payload.update(valid=False, invalid_reason=str(exc))
            self._emit("model", payload)
            return
        if not (0.0 <= est.low <= est.fair <= est.high <= 1.0):
            payload.update(valid=False, invalid_reason="band_inconsistent")
            self._emit("model", payload)
            return
        ctx.estimate = est
        payload.update(valid=True, fair=est.fair, low=est.low, high=est.high)
        self._emit("model", payload)

    # ------------------------------------------------------------------ entries

    def _entry_blockers(self, ctx: MatchContext) -> tuple[Reason, str] | None:
        if self.mode == "record":
            return Reason.RECORD_MODE, ""
        if ctx.anchor is None:
            return Reason.NO_ANCHOR, ctx.anchor_error or ""
        if ctx.ruleset is None:
            return (
                Reason.TOURNAMENT_UNSUPPORTED,
                ctx.classification.reason if ctx.classification else "",
            )
        if ctx.model is None:
            return Reason.CALIBRATION_FAILED, ctx.model_error or ""
        if ctx.detector is None or not ctx.detector.reliable:
            return Reason.SCORE_UNRELIABLE, ""
        if ctx.detector.current_server() is None:
            return Reason.SERVER_UNKNOWN, ""
        if (
            ctx.last_score_ts is None
            or self.now_ns - ctx.last_score_ts > self.cfg.feeds.max_score_staleness_s * NS_PER_S
        ):
            return Reason.SCORE_STALE, ""
        if ctx.estimate is None:
            return Reason.MODEL_INVALID, ""
        if ctx.estimate.width > self.cfg.model.max_band_width:
            return Reason.BAND_TOO_WIDE, f"{ctx.estimate.width:.3f}"
        if not (
            self.cfg.strategy.no_entry_below_prob
            <= ctx.estimate.fair
            <= self.cfg.strategy.no_entry_above_prob
        ):
            return Reason.NEAR_CERTAIN, f"{ctx.estimate.fair:.3f}"
        blocks = self.risk.entry_blocks(ctx.key)
        if blocks:
            return Reason.HALTED, ",".join(blocks)
        ok, why = ctx.tracker.tradeable(self.now_ns)
        if not ok:
            return Reason.BOOK_UNTRADEABLE, why
        return None

    def _expire_pending(self, ctx: MatchContext) -> None:
        ev = ctx.pending_event
        ctx.pending_event = None
        if ev is not None and ev.dedupe_key not in ctx.acted_events:
            self._decision(
                ctx,
                Action.SKIP,
                Reason.EVENT_WINDOW_EXPIRED,
                trigger=ev.dedupe_key,
                numbers={
                    "attempts": ctx.pending_attempts,
                    "event_data_age_s": (self.now_ns - (ctx.pending_as_of or self.now_ns))
                    / NS_PER_S,
                },
            )

    def _try_entry(self, ctx: MatchContext) -> None:
        ev = ctx.pending_event
        if ev is None:
            return
        if ctx.pending_deadline is not None and self.now_ns > ctx.pending_deadline:
            self._expire_pending(ctx)
            return
        if ev.dedupe_key in ctx.acted_events:
            ctx.pending_event = None
            return
        ctx.pending_attempts += 1
        first = ctx.pending_attempts == 1
        blocked = self._entry_blockers(ctx)
        if blocked is not None:
            reason, detail = blocked
            if first or reason not in (Reason.BOOK_UNTRADEABLE,):
                self._decision(
                    ctx,
                    Action.SKIP,
                    reason,
                    trigger=ev.dedupe_key,
                    detail=detail,
                    numbers={"attempt": ctx.pending_attempts},
                )
            if reason not in (Reason.BOOK_UNTRADEABLE,):
                ctx.pending_event = None
            return
        if (
            ctx.last_exit_ts is not None
            and self.now_ns - ctx.last_exit_ts < self.cfg.strategy.reentry_cooldown_s * NS_PER_S
        ):
            self._decision(ctx, Action.SKIP, Reason.COOLDOWN, trigger=ev.dedupe_key)
            ctx.pending_event = None
            return

        assert ctx.estimate is not None and ctx.oriented is not None
        assert ctx.tracker.view is not None and ctx.model is not None
        book: OrderBookView = ctx.tracker.view
        state = ctx.oriented.to_state()
        hold_min = (
            expected_remaining_games(state, ctx.model.ruleset) * self.cfg.strategy.minutes_per_game
        )
        budget = self.risk.remaining_budget(self.ledger, self.open_orders(), ctx.key)
        sizing = SizingInputs(
            bankroll=self.cfg.risk.bankroll,
            kelly_multiplier=self.cfg.risk.kelly_multiplier,
            max_loss_budget=budget,
            cash_available=self.ledger.cash,
            min_qty=self.cfg.risk.min_order_qty,
            max_qty=self.cfg.risk.max_order_qty,
        )
        cands = [
            evaluate_side(
                side=s,
                market=ctx.market,
                book=book,
                estimate=ctx.estimate,
                fee_model=ctx.fee_model,
                cfg=self.cfg.strategy,
                sizing=sizing,
                hold_minutes=hold_min,
            )
            for s in ("YES", "NO")
        ]
        cand = best_entry(cands)
        numbers = cand.numbers()
        numbers.update(
            attempt=ctx.pending_attempts,
            fair_yes=ctx.estimate.fair,
            low_yes=ctx.estimate.low,
            high_yes=ctx.estimate.high,
            best_bid=_f(book.best_bid),
            best_ask=_f(book.best_ask),
            event_kind=ev.kind.value,
            event_latency_ms=(self.now_ns - ev.local_ts_ns) / 1e6,
        )
        if ctx.last_exit_ts is not None and cand.ok:
            need = self.cfg.strategy.min_net_edge + self.cfg.strategy.reentry_min_edge_increment
            if cand.net_edge_per_contract < need:
                cand.ok = False
                cand.reason = Reason.EDGE_BELOW_THRESHOLD
                numbers["reentry_required_edge"] = need
        if not cand.ok:
            retryable = cand.reason in (
                Reason.SPREAD_TOO_WIDE,
                Reason.INSUFFICIENT_DEPTH,
                Reason.MARKET_ALREADY_REPRICED,
                Reason.EDGE_CONSUMED_BY_FEES,
                Reason.EDGE_BELOW_THRESHOLD,
                Reason.EDGE_WITHIN_MODEL_UNCERTAINTY,
            )
            if first or not retryable:
                self._decision(
                    ctx, Action.SKIP, cand.reason, trigger=ev.dedupe_key, numbers=numbers
                )
            if not retryable:
                ctx.pending_event = None
            return
        assert cand.limit_price is not None
        rd = self.risk.authorize_entry(
            market=ctx.market,
            intent=cand.intent,
            qty=cand.qty,
            limit_price=cand.limit_price,
            max_loss=cand.max_loss,
            ledger=self.ledger,
            open_orders=self.open_orders(),
            markets=self.markets,
            active_matches=set(self.contexts),
        )
        self._emit(
            "risk",
            {
                "match_key": ctx.key,
                "approved": rd.approved,
                "reasons": rd.reasons,
                "budgets": rd.budgets,
                "trigger": ev.dedupe_key,
            },
        )
        if not rd.approved:
            self._decision(
                ctx,
                Action.SKIP,
                Reason.RISK_REJECTED,
                trigger=ev.dedupe_key,
                numbers=numbers,
                detail=",".join(rd.reasons),
            )
            ctx.pending_event = None
            return
        d = self._decision(
            ctx,
            Action.BUY,
            Reason.OK,
            intent=cand.intent,
            qty=cand.qty,
            limit_price=cand.limit_price,
            trigger=ev.dedupe_key,
            numbers=numbers,
        )
        ctx.acted_events.add(ev.dedupe_key)
        ctx.pending_event = None
        self._submit(ctx, d, TimeInForce.IOC)

    def _submit(self, ctx: MatchContext, d: Decision, tif: TimeInForce) -> None:
        assert self.broker is not None and d.intent and d.qty and d.limit_price
        self._order_seq += 1
        cid = f"ts-{hashlib.sha1(f'{d.decision_id}|{self._order_seq}'.encode()).hexdigest()[:20]}"
        order = Order(
            client_order_id=cid,
            market_slug=ctx.market.slug,
            match_key=ctx.key,
            intent=d.intent,
            order_type=OrderType.LIMIT,
            tif=tif,
            limit_price=d.limit_price,
            qty=d.qty,
            created_local_ts_ns=self.now_ns,
            decision_id=d.decision_id,
        )
        self.broker.submit(order, self.now_ns)

    def _cancel_match_orders(self, ctx: MatchContext, reason: str) -> None:
        if not self.broker:
            return
        cancelled = False
        for o in self.broker.open_orders():
            if o.match_key == ctx.key:
                self.broker.cancel(o.client_order_id, self.now_ns, reason)
                cancelled = True
        if cancelled:
            self._decision(ctx, Action.CANCEL_ORDERS, Reason.MATCH_INTERRUPTED, detail=reason)

    # ------------------------------------------------------------------ positions

    def _manage_positions(self, ctx: MatchContext, on_score: bool) -> None:
        if self.broker is None:
            return
        positions = [p for p in self.ledger.open_positions() if p.match_key == ctx.key]
        if not positions:
            return
        if (
            ctx.status != MatchStatus.LIVE
            or ctx.estimate is None
            or ctx.detector is None
            or not ctx.detector.reliable
        ):
            return  # cannot trade or cannot value reliably: hold, never dump blindly
        ok, _ = ctx.tracker.tradeable(self.now_ns)
        if not ok or ctx.tracker.view is None:
            return
        for pos in positions:
            if any(o.market_slug == pos.market_slug for o in self.broker.open_orders()):
                continue
            base = self.equity() - liquidation_value(pos, ctx.tracker.view, ctx.fee_model)
            ex = evaluate_exit(
                position=pos,
                estimate=ctx.estimate,
                book=ctx.tracker.view,
                fee_model=ctx.fee_model,
                base_wealth=base,
                qty_step=ctx.market.min_qty,
                min_improvement=self.cfg.strategy.exit_min_improvement,
            )
            if ex.action == Action.HOLD:
                if on_score:
                    self._decision(ctx, Action.HOLD, ex.reason, numbers=ex.numbers)
                continue
            assert ex.intent is not None
            rd = self.risk.authorize_exit(
                market=ctx.market, intent=ex.intent, qty=ex.qty, ledger=self.ledger
            )
            if not rd.approved:
                self._emit(
                    "risk",
                    {"match_key": ctx.key, "approved": False, "reasons": rd.reasons, "exit": True},
                )
                continue
            d = self._decision(
                ctx,
                ex.action,
                ex.reason,
                intent=ex.intent,
                qty=ex.qty,
                limit_price=ex.limit_price,
                numbers=ex.numbers,
            )
            ctx.last_exit_ts = self.now_ns
            self._submit(ctx, d, TimeInForce.IOC)

    def _check_daily_loss(self) -> None:
        if self.daily.update(self.equity(), self.now_ns):
            self._emit("halt", {"reason": "daily_loss_limit", "latch": self.latch.state})


def _f(x: Decimal | None) -> float | None:
    return None if x is None else float(x)
