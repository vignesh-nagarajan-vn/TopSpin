"""Realized-P&L analytics from journal records. Only simulated or real *fills* create P&L."""

from __future__ import annotations

import math
import random
import statistics
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from tennis_alpha.models.market import ONE
from tennis_alpha.timeutil import NS_PER_S, utc_day


@dataclass
class RoundTrip:
    market_slug: str
    match_key: str
    side: str
    entry_ts_ns: int
    exit_ts_ns: int | None = None
    qty_opened: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)  # held-side price * qty, entries
    proceeds: Decimal = Decimal(0)  # exits + settlement payouts
    fees: Decimal = Decimal(0)
    open_qty: Decimal = Decimal(0)
    signal: str | None = None
    entry_model_prob: float | None = None
    entry_best_price: float | None = None
    settled: bool = False
    fill_ids: list[str] = field(default_factory=list)

    @property
    def gross(self) -> Decimal:
        return self.proceeds - self.cost

    @property
    def net(self) -> Decimal:
        return self.gross - self.fees

    @property
    def holding_s(self) -> float | None:
        return None if self.exit_ts_ns is None else (self.exit_ts_ns - self.entry_ts_ns) / NS_PER_S


def build_round_trips(records: list[dict[str, Any]]) -> list[RoundTrip]:
    decisions = {r["payload"]["decision_id"]: r["payload"] for r in records if r["kind"] == "decision"}
    order_decision: dict[str, str | None] = {}
    for r in records:
        if r["kind"] == "order_submitted":
            o = r["payload"]["order"]
            order_decision[o["client_order_id"]] = o.get("decision_id")
    open_trips: dict[str, RoundTrip] = {}
    done: list[RoundTrip] = []
    for r in records:
        if r["kind"] == "fill":
            f = r["payload"]["fill"]
            slug = f["market_slug"]
            intent = f["intent"]
            holding = "YES" if intent.endswith("_LONG") else "NO"
            opens = intent.startswith("ORDER_INTENT_BUY")
            px = Decimal(f["price"])
            side_px = px if holding == "YES" else ONE - px
            qty = Decimal(f["qty"])
            fee = Decimal(f["fee"])
            trip = open_trips.get(slug)
            if opens:
                if trip is None:
                    d = decisions.get(order_decision.get(f["client_order_id"]) or "", {})
                    nums = d.get("numbers") or {}
                    trip = RoundTrip(slug, f["match_key"], holding, int(f["local_ts_ns"]),
                                     signal=nums.get("event_kind"), entry_model_prob=nums.get("q_fair"),
                                     entry_best_price=nums.get("best_side_price"))
                    open_trips[slug] = trip
                trip.qty_opened += qty
                trip.open_qty += qty
                trip.cost += side_px * qty
            else:
                if trip is None:
                    continue
                trip.open_qty -= qty
                trip.proceeds += side_px * qty
            trip.fees += fee
            trip.fill_ids.append(f["fill_id"])
            if trip.open_qty <= 0:
                trip.exit_ts_ns = int(f["local_ts_ns"])
                done.append(open_trips.pop(slug))
        elif r["kind"] == "settlement" and r["payload"].get("market_slug") in open_trips:
            slug = r["payload"]["market_slug"]
            trip = open_trips.pop(slug)
            y = Decimal(r["payload"]["yes_price"])
            payout = y if trip.side == "YES" else ONE - y
            trip.proceeds += payout * trip.open_qty
            trip.open_qty = Decimal(0)
            trip.exit_ts_ns = int(r["local_ts_ns"])
            trip.settled = True
            done.append(trip)
    return done + list(open_trips.values())


def max_drawdown(series: list[float]) -> float:
    peak, mdd, cum = 0.0, 0.0, 0.0
    for x in series:
        cum += x
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return mdd


def cluster_bootstrap_mean(values_by_cluster: dict[str, list[float]], n_boot: int = 2000, seed: int = 7) -> dict[str, float | int | None]:
    """Mean P&L per *match* with a match-clustered bootstrap CI (trades within a match are
    not independent observations)."""
    clusters = [sum(v) for v in values_by_cluster.values()]
    n = len(clusters)
    if n == 0:
        return {"clusters": 0, "mean": None, "ci_low": None, "ci_high": None}
    mean = statistics.fmean(clusters)
    if n < 2:
        return {"clusters": n, "mean": mean, "ci_low": None, "ci_high": None}
    rng = random.Random(seed)
    boots = sorted(statistics.fmean(rng.choices(clusters, k=n)) for _ in range(n_boot))
    return {"clusters": n, "mean": mean, "ci_low": boots[int(0.025 * n_boot)], "ci_high": boots[int(0.975 * n_boot) - 1]}


class BookIndex:
    """Per-market time index of book records for markouts (no lookahead in decisions:
    used only for after-the-fact evaluation)."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.ts: dict[str, list[int]] = defaultdict(list)
        self.rows: dict[str, list[tuple[float | None, float | None]]] = defaultdict(list)
        for r in records:
            if r["kind"] != "book":
                continue
            p = r["payload"]
            bids = [float(b[0]) for b in p.get("bids") or []]
            asks = [float(a[0]) for a in p.get("asks") or []]
            self.ts[p["market_slug"]].append(int(r["local_ts_ns"]))
            self.rows[p["market_slug"]].append((max(bids) if bids else None, min(asks) if asks else None))

    def at(self, slug: str, t_ns: int) -> tuple[float | None, float | None]:
        ts = self.ts.get(slug)
        if not ts:
            return None, None
        i = bisect_right(ts, t_ns) - 1
        return self.rows[slug][i] if i >= 0 else (None, None)

    def mid(self, slug: str, t_ns: int) -> float | None:
        b, a = self.at(slug, t_ns)
        return None if b is None or a is None else (a + b) / 2


def markouts(records: list[dict[str, Any]], horizons_s: tuple[int, ...] = (5, 30, 60)) -> dict[str, Any]:
    """Executable markout per entry fill: the price we could exit at after h seconds minus
    the held-side entry price (positive = favourable)."""
    idx = BookIndex(records)
    out: dict[str, list[float]] = {str(h): [] for h in horizons_s}
    for r in records:
        if r["kind"] != "fill":
            continue
        f = r["payload"]["fill"]
        if not f["intent"].startswith("ORDER_INTENT_BUY"):
            continue
        px = float(f["price"])
        yes = f["intent"].endswith("_LONG")
        entry = px if yes else 1 - px
        for h in horizons_s:
            bid, ask = idx.at(f["market_slug"], int(f["local_ts_ns"]) + h * NS_PER_S)
            exit_px = bid if yes else (None if ask is None else 1 - ask)
            if exit_px is not None:
                out[str(h)].append(exit_px - entry)
    return {h: {"n": len(v), "mean": statistics.fmean(v) if v else None} for h, v in out.items()}


def pnl_summary(records: list[dict[str, Any]], bankroll: float) -> dict[str, Any]:
    trips = build_round_trips(records)
    closed = [t for t in trips if t.exit_ts_ns is not None]
    nets = [float(t.net) for t in closed]
    wins = sum(x for x in nets if x > 0)
    losses = -sum(x for x in nets if x < 0)
    by_match: dict[str, list[float]] = defaultdict(list)
    daily: dict[str, float] = defaultdict(float)
    for t in closed:
        by_match[t.match_key].append(float(t.net))
        daily[utc_day(t.exit_ts_ns or t.entry_ts_ns)] += float(t.net)
    risk_capital = sum(float(t.cost + t.fees) for t in closed)
    hold_hours = sum((t.holding_s or 0) for t in closed) / 3600
    slips = [
        (float(t.cost / t.qty_opened) - t.entry_best_price)
        for t in closed
        if t.entry_best_price is not None and t.qty_opened > 0
    ]
    by_signal: dict[str, list[float]] = defaultdict(list)
    for t in closed:
        by_signal[t.signal or "unknown"].append(float(t.net))
    return {
        "round_trips_closed": len(closed),
        "round_trips_open": len(trips) - len(closed),
        "gross_realized_pnl": sum(float(t.gross) for t in closed),
        "net_realized_pnl": sum(nets),
        "total_fees": sum(float(t.fees) for t in closed),
        "est_entry_slippage_per_contract": statistics.fmean(slips) if slips else None,
        "net_return_on_bankroll": sum(nets) / bankroll if bankroll else None,
        "return_per_dollar_risk": sum(nets) / risk_capital if risk_capital else None,
        "return_per_holding_hour": sum(nets) / hold_hours if hold_hours else None,
        "avg_net_per_round_trip": statistics.fmean(nets) if nets else None,
        "median_net_per_round_trip": statistics.median(nets) if nets else None,
        "profit_factor": (wins / losses) if losses > 0 else (math.inf if wins > 0 else None),
        "max_drawdown": max_drawdown([float(t.net) for t in sorted(closed, key=lambda t: t.exit_ts_ns or 0)]),
        "worst_trade": min(nets) if nets else None,
        "worst_match": min((sum(v) for v in by_match.values()), default=None),
        "daily_pnl": dict(sorted(daily.items())),
        "by_signal": {k: {"n": len(v), "net": sum(v)} for k, v in by_signal.items()},
        "match_clustered_mean": cluster_bootstrap_mean(by_match),
        "markouts": markouts(records),
    }


def execution_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = [r["payload"] for r in records if r["kind"] == "decision"]
    signals = [d for d in decisions if d.get("trigger")]
    skips = Counter(d["reason"] for d in decisions if d["action"] == "SKIP")
    submitted = [r for r in records if r["kind"] == "order_submitted"]
    updates: dict[str, dict[str, Any]] = {}
    for r in records:
        if r["kind"] == "order_update":
            updates[r["payload"]["client_order_id"]] = r["payload"] | {"ts": r["local_ts_ns"]}
    fills_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        if r["kind"] == "fill":
            fills_by_order[r["payload"]["fill"]["client_order_id"]].append(r)
    full = partial = rejected = none = 0
    lat_fill: list[float] = []
    est_fees = real_fees = 0.0
    for s in submitted:
        o = s["payload"]["order"]
        cid = o["client_order_id"]
        fills = fills_by_order.get(cid, [])
        filled = sum(float(f["payload"]["fill"]["qty"]) for f in fills)
        state = (updates.get(cid) or {}).get("state")
        if state == "rejected":
            rejected += 1
        elif filled >= float(o["qty"]) - 1e-12:
            full += 1
        elif filled > 0:
            partial += 1
        else:
            none += 1
        if fills:
            lat_fill.append((fills[0]["local_ts_ns"] - s["local_ts_ns"]) / 1e6)
        for f in fills:
            real_fees += float(f["payload"]["fill"]["fee"])
            est_fees += float(f["payload"]["fill"]["exact_fee"])
    n = len(submitted)
    return {
        "decisions": len(decisions),
        "signals": len(signals),
        "skipped": sum(skips.values()),
        "skip_reasons": dict(skips.most_common()),
        "orders_submitted": n,
        "fully_filled": full,
        "partially_filled": partial,
        "unfilled": none,
        "rejected": rejected,
        "fill_rate": (full + partial) / n if n else None,
        "decision_to_first_fill_ms_median": statistics.median(lat_fill) if lat_fill else None,
        "fees_exact_vs_charged": {"exact": est_fees, "charged": real_fees},
    }


def model_quality(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Brier score / log loss of valid model outputs against settled outcomes, plus a
    per-score-state error breakdown. Every observation in a match shares one outcome, so
    these are descriptive, not independent-sample statistics."""
    outcome: dict[str, float] = {}
    slug_to_match: dict[str, str] = {}
    for r in records:
        if r["kind"] == "market_info":
            slug_to_match[r["payload"]["slug"]] = r["payload"]["event_slug"]
    for r in records:
        if r["kind"] in ("settlement_applied", "settlement") and r["payload"].get("market_slug") in slug_to_match:
            outcome[slug_to_match[r["payload"]["market_slug"]]] = float(r["payload"]["yes_price"])
    pts = [(r["payload"]["match_key"], r["payload"]["fair"]) for r in records
           if r["kind"] == "model" and r["payload"].get("valid") and r["payload"]["match_key"] in outcome]
    if not pts:
        return {"observations": 0}
    brier = statistics.fmean((p - outcome[m]) ** 2 for m, p in pts)
    eps = 1e-6
    ll = statistics.fmean(
        -(outcome[m] * math.log(max(p, eps)) + (1 - outcome[m]) * math.log(max(1 - p, eps))) for m, p in pts
    )
    buckets: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for m, p in pts:
        buckets[f"{min(int(p * 10), 9) / 10:.1f}"].append((p, outcome[m]))
    calib = {k: {"n": len(v), "mean_p": statistics.fmean(a for a, _ in v), "freq": statistics.fmean(b for _, b in v)}
             for k, v in sorted(buckets.items())}
    return {"observations": len(pts), "matches": len({m for m, _ in pts}), "brier": brier, "log_loss": ll,
            "calibration": calib}
