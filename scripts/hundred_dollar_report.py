"""Build the 2-page "$100 test" PDF: the real engine replayed on recorded matches, plus a
benchmark of blind bets on real finished matches. Paper test only; no orders are placed.

    python scripts/fetch_market_history.py   # once, caches data/benchmark_matches.json
    python scripts/hundred_dollar_report.py  # writes reports/TopSpin_100_dollar_test.pdf

Needs matplotlib (``pip install -e ".[report]"``). Random draws use a fixed seed so every number
in the PDF can be reproduced.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import FancyBboxPatch
from matplotlib.ticker import MaxNLocator, MultipleLocator

from topspin.analytics.pnl import build_round_trips
from topspin.config import load_config
from topspin.discovery.tournaments import TournamentClassifier
from topspin.simulation.fees import FeeModel
from topspin.simulation.replay import replay_records
from topspin.storage.journal import INPUT_KINDS, read_journal
from topspin.timeutil import NS_PER_S

SEED = 20261031
BANKROLL = 100.0
RISK_PER_GAME = 2.0  # the engine's 2% per-match limit on a $100 bankroll
SERIES_GAMES = 50
SERIES_RUNS = 10_000
MIN_LIVE_MINUTES_WATCHED = 15
JOURNALS = "data/record_2026-10-30/*.jsonl"
BENCHMARK = Path("data/benchmark_matches.json")
OUT = Path("reports/TopSpin_100_dollar_test.pdf")

# Reference palette (dataviz skill): categorical slots 1-3 and neutrals, light mode.
C1, C2, C3 = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, BASE, SURFACE, TILE = "#e1e0d9", "#c3c2b7", "#fcfcfb", "#f3f2ee"

COMMON = {"risk": {"bankroll": "100"}, "strategy": {"min_expected_net_pnl": 0.05}}
RESEARCH = {"model": {"anchor_max_age_s": 7200}, "feeds": {"unknown_cache_age_s": 0}}
PROFILES: dict[str, dict[str, dict[str, Any]]] = {
    "as_built": {},
    "research": RESEARCH,
    "half_margins": {
        "model": {**RESEARCH["model"], "uncertainty_floor": 0.005, "serve_baseline_delta": 0.015},
        "feeds": RESEARCH["feeds"],
        "strategy": {
            "min_net_edge": 0.01,
            "adverse_selection_allowance": 0.005,
            "slippage_allowance_ticks": 0,
        },
    },
    "no_margins": {
        "model": {**RESEARCH["model"], "uncertainty_floor": 0.0, "serve_baseline_delta": 0.0},
        "feeds": RESEARCH["feeds"],
        "strategy": {
            "min_net_edge": 0.0,
            "adverse_selection_allowance": 0.0,
            "slippage_allowance_ticks": 0,
            "min_expected_net_pnl": 0.0,
        },
    },
}
REASONS = {
    "event_window_expired": "Data too old to act on",
    "match_not_live": "Match already over",
    "score_data_gap": "Gap in the recording",
    "near_certain_outcome": "Outcome nearly decided",
    "edge_below_threshold": "Edge below the minimum",
    "edge_consumed_by_fees": "Edge smaller than fees",
    "market_already_repriced": "Market had already moved",
    "edge_within_model_uncertainty": "Edge within model doubt",
}


def merged(profile: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {k: dict(v) for k, v in COMMON.items()}
    for k, v in profile.items():
        out.setdefault(k, {}).update(v)
    return out


def surname(name: str) -> str:
    parts = name.split()
    start = len(parts) - 1
    while start > 0 and parts[start - 1].islower():
        start -= 1
    return " ".join(parts[start:])


# ------------------------------------------------------------------ engine on recorded games


def run_engine() -> dict[str, Any]:
    classifier = TournamentClassifier.from_file("config/tournaments.yaml")
    inputs = list(read_journal(sorted(glob.glob(JOURNALS)), kinds=INPUT_KINDS))

    # Tour-level matches recorded that day, with what the recorder actually saw.
    games: dict[str, dict[str, Any]] = {}
    for r in inputs:
        p = r["payload"]
        if r["kind"] == "market_info":
            games.setdefault(
                p["event_slug"],
                {
                    "long": p["long"]["name"],
                    "short": p["short"]["name"],
                    "slug": p["slug"],
                    "tour": p["tour"],
                    "first": None,
                    "last": None,
                    "final": None,
                    "tournament": None,
                },
            )
        elif r["kind"] == "score" and p["match_key"] in games:
            g = games[p["match_key"]]
            if p.get("tournament_name"):
                g["tournament"] = p["tournament_name"]
            if p["status"] == "live":
                g["first"] = g["first"] or r["local_ts_ns"]
                g["last"] = r["local_ts_ns"]
            if p["status"] == "finished":
                g["final"] = p["raw_score"]
    pool = {}
    for key, g in games.items():
        if not g["final"] or not g["tournament"]:
            continue
        if not classifier.classify(g["tour"], g["tournament"], None).supported:
            continue
        g["live_min"] = (g["last"] - g["first"]) / 60 / NS_PER_S if g["first"] else 0.0
        pool[key] = g

    runs: dict[str, Any] = {}
    for name, profile in PROFILES.items():
        cfg = load_config("config/default.yaml", overrides=merged(profile))
        res = replay_records(cfg, inputs, classifier)
        first_reason: dict[str, dict[str, Any]] = {}
        for d in res.decisions():
            if d.get("trigger") and d["trigger"] not in first_reason and d["match_key"] in pool:
                first_reason[d["trigger"]] = d
        pnl: dict[str, float] = defaultdict(float)
        fees = 0.0
        trips = [t for t in build_round_trips(res.journal.records) if t.match_key in pool]
        for t in trips:
            pnl[t.match_key] += float(t.net)
            fees += float(t.fees)
        runs[name] = {
            "signals": first_reason,
            "reasons": Counter(d["reason"] for d in first_reason.values() if d["action"] == "SKIP"),
            "buys": [d for d in res.decisions() if d["action"] == "BUY"],
            "exits": [d for d in res.decisions() if d["action"] in ("SELL", "REDUCE")],
            "pnl": dict(pnl),
            "total": sum(pnl.values()),
            "fees": fees,
            "trips": trips,
            "journal": res.journal.records,
        }

    rng = random.Random(SEED)
    order = list(pool)
    rng.shuffle(order)
    watched = sorted(
        (k for k, g in pool.items() if g["live_min"] >= MIN_LIVE_MINUTES_WATCHED),
        key=lambda k: pool[k]["first"],
    )
    return {"pool": pool, "order": order, "watched": watched, "runs": runs, "inputs": len(inputs)}


def game_series(engine: dict[str, Any], key: str) -> dict[str, Any]:
    slug = engine["pool"][key]["slug"]
    mids: list[tuple[int, float]] = []
    model: list[tuple[int, float]] = []
    events: list[int] = []
    for r in engine["runs"]["research"]["journal"]:
        p = r["payload"]
        if r["kind"] == "book" and p["market_slug"] == slug and p["bids"] and p["asks"]:
            bid = max(float(b[0]) for b in p["bids"])
            ask = min(float(a[0]) for a in p["asks"])
            mids.append((r["local_ts_ns"], (bid + ask) / 2))
        elif r["kind"] == "model" and p["match_key"] == key and p.get("valid"):
            model.append((r["local_ts_ns"], p["fair"]))
        elif (
            r["kind"] == "tennis_event"
            and p["match_key"] == key
            and p["kind"] in ("break", "set_complete")
        ):
            events.append(r["local_ts_ns"])
    return {"mids": mids, "model": model, "events": events}


# ------------------------------------------------------------------ benchmark on finished games


@dataclass(frozen=True)
class Bet:
    yes: float  # P&L of risking the budget on the YES side
    no: float
    fav: float
    dog: float
    cost_per_contract: float  # half spread plus taker fee, on the mid


def bet_pnl(side_px: float, yes_px: float, payout: float, theta: Decimal) -> float:
    fee_model = FeeModel(taker_theta=theta)
    rate = float(fee_model.per_contract_rate(Decimal(str(yes_px))))
    step = Decimal("0.01")
    qty = (Decimal(str(RISK_PER_GAME / (side_px + rate))) / step).to_integral_value(
        ROUND_DOWN
    ) * step
    fee = fee_model.taker_fee(qty, Decimal(str(yes_px)))
    return float(qty) * (payout - side_px) - float(fee)


def run_benchmark() -> dict[str, Any]:
    data = json.loads(BENCHMARK.read_text())
    bets: list[Bet] = []
    points: list[tuple[float, float]] = []
    tournaments = set()
    for m in data["matches"]:
        ask, bid, s = m["ask"], m["bid"], m["settlement"]
        if not (0 < bid < ask < 1) or ask - bid > 0.10:
            continue
        theta = Decimal(str(m.get("fee_theta") or "0.0695"))
        yes = bet_pnl(ask, ask, s, theta)
        no = bet_pnl(1 - bid, bid, 1 - s, theta)
        mid = (ask + bid) / 2
        fav, dog = (yes, no) if mid >= 0.5 else (no, yes)
        cost = (ask - bid) / 2 + float(
            FeeModel(taker_theta=theta).per_contract_rate(Decimal(str(round(mid, 4))))
        )
        bets.append(Bet(yes, no, fav, dog, cost))
        points.append((mid, s))
        tournaments.add(m["tournament"])

    rng = random.Random(SEED + 1)
    pools = {"coin": None, "fav": [b.fav for b in bets], "dog": [b.dog for b in bets]}
    ends: dict[str, list[float]] = {}
    paths: list[list[float]] = []
    all_paths: list[list[float]] = []
    for name, vals in pools.items():
        out = []
        for run in range(SERIES_RUNS):
            sample = rng.sample(bets, SERIES_GAMES)
            pnls = (
                [(b.yes if rng.random() < 0.5 else b.no) for b in sample]
                if vals is None
                else [(b.fav if name == "fav" else b.dog) for b in sample]
            )
            out.append(BANKROLL + sum(pnls))
            if name == "coin":
                bal, path = BANKROLL, [BANKROLL]
                for x in pnls:
                    bal += x
                    path.append(bal)
                all_paths.append(path)
                if run < 40:
                    paths.append(path)
        ends[name] = sorted(out)

    # Expected result per game with match-level bootstrap intervals.
    def ci(values: list[float]) -> tuple[float, float, float]:
        boots = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(2000))
        return statistics.fmean(values), boots[50], boots[1949]

    per_game = {
        "coin": ci([(b.yes + b.no) / 2 for b in bets]),
        "fav": ci([b.fav for b in bets]),
        "dog": ci([b.dog for b in bets]),
    }
    bins = []
    for lo in range(0, 100, 10):
        sel = [s for mid, s in points if lo / 100 <= mid < (lo + 10) / 100]
        if len(sel) >= 10:
            mids = [mid for mid, _ in points if lo / 100 <= mid < (lo + 10) / 100]
            freq = statistics.fmean(sel)
            se = math.sqrt(max(freq * (1 - freq), 1e-9) / len(sel))
            bins.append((statistics.fmean(mids), freq, 1.96 * se, len(sel)))
    return {
        "n": len(bets),
        "tournaments": len(tournaments),
        "start": min(m["start"] for m in data["matches"])[:10],
        "end": max(m["start"] for m in data["matches"])[:10],
        "ends": ends,
        "paths": paths,
        "median_path": [
            statistics.median(p[i] for p in all_paths) for i in range(SERIES_GAMES + 1)
        ],
        "per_game": per_game,
        "bins": bins,
        "cost": statistics.fmean(b.cost_per_contract for b in bets),
        "brier": statistics.fmean((mid - s) ** 2 for mid, s in points),
    }


# ------------------------------------------------------------------ drawing helpers


def pct(sorted_vals: list[float], f: float) -> float:
    return sorted_vals[min(len(sorted_vals) - 1, int(f * len(sorted_vals)))]


def style(ax: Any, *, ygrid: bool = True, xgrid: bool = False) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASE)
    ax.tick_params(colors=MUTED, labelsize=7.5, length=0)
    ax.grid(ygrid, axis="y", color=GRID, linewidth=0.6)
    if xgrid:
        ax.grid(True, axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def chart_axes(
    fig: Any,
    rect: tuple[float, float, float, float],
    head: str,
    sub: str = "",
    head_x: float | None = None,
) -> Any:
    x, y, w, h = rect
    hx = x if head_x is None else head_x
    fig.text(hx, y + h + 0.032, head, fontsize=9.5, fontweight="bold", color=INK, va="bottom")
    if sub:
        fig.text(hx, y + h + 0.029, sub, fontsize=7.5, color=INK2, va="top")
    ax = fig.add_axes((x, y, w, h))
    style(ax)
    return ax


def tile(fig: Any, x: float, y: float, w: float, h: float, value: str, label: str) -> None:
    fig.patches.append(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0,rounding_size=0.008",
            transform=fig.transFigure,
            facecolor=TILE,
            edgecolor="none",
            mutation_aspect=8.5 / 11,
        )
    )
    fig.text(x + 0.012, y + h * 0.56, value, fontsize=17, fontweight="bold", color=INK, va="center")
    fig.text(x + 0.012, y + h * 0.2, label, fontsize=7.5, color=INK2, va="center")


def page(fig: Any, heading: str, sub: str, number: int) -> None:
    fig.patch.set_facecolor(SURFACE)
    fig.text(0.06, 0.962, heading, fontsize=15, fontweight="bold", color=INK, va="center")
    fig.text(0.06, 0.937, sub, fontsize=8.5, color=INK2, va="center")
    fig.text(0.94, 0.022, f"Page {number} of 2", fontsize=7, color=MUTED, ha="right")
    fig.text(
        0.06,
        0.022,
        "Paper test on recorded and historical data. No real money was used. Past results do not predict future "
        "results. Not investment advice.",
        fontsize=7,
        color=MUTED,
    )


def money(x: float) -> str:
    return f"{'-' if x < 0 else ''}${abs(x):,.2f}"


def signed(x: float) -> str:
    return "$0.00" if abs(x) < 0.005 else f"{'+' if x > 0 else '-'}${abs(x):,.2f}"


# ------------------------------------------------------------------ page 1


def draw_page1(pdf: PdfPages, engine: dict[str, Any]) -> None:
    fig = plt.figure(figsize=(8.5, 11))
    pool, order, runs = engine["pool"], engine["order"], engine["runs"]
    as_built, research = runs["as_built"], runs["research"]
    page(
        fig,
        "TopSpin $100 test: it would not have traded",
        f"The real engine, replayed on all {len(pool)} tour-level matches recorded on Oct 30, 2026, in random order. "
        "$100 bankroll, $2 risk limit per match.",
        1,
    )

    n_signals = len(as_built["signals"])
    w, gap, y0, h = 0.205, 0.0183, 0.835, 0.075
    tiles = [
        (money(BANKROLL + as_built["total"]), "Ending balance, from $100.00"),
        (str(len(as_built["buys"])), "Trades placed"),
        (str(n_signals), "Break and set signals seen"),
        (money(as_built["fees"]), "Fees paid"),
    ]
    for i, (v, lab) in enumerate(tiles):
        tile(fig, 0.06 + i * (w + gap), y0, w, h, v, lab)

    # A. Balance by game
    ax = chart_axes(
        fig,
        (0.09, 0.60, 0.43, 0.165),
        "Balance across the random series",
        "What loosening the safety rules would have done",
        head_x=0.06,
    )
    series = [
        ("as_built", "As built", C1),
        ("half_margins", "Half margins", C2),
        ("no_margins", "No margins", C3),
    ]
    xs = list(range(len(order) + 1))
    for name, label, color in series[::-1]:  # as built is drawn last, on top
        bal, ys = BANKROLL, [BANKROLL]
        for key in order:
            bal += runs[name]["pnl"].get(key, 0.0)
            ys.append(bal)
        ax.step(
            xs, ys, where="post", color=color, linewidth=1.6, label=label, solid_capstyle="round"
        )
    lo = min(BANKROLL + runs[n]["total"] for n, _, _ in series)
    ax.set_ylim(min(lo - 0.6, 98.0), 101.0)
    ax.set_xlim(0, len(order))
    ax.set_xticks(range(0, len(order) + 1, 2))
    ax.set_xlabel("Games played, in random order", fontsize=7.5, color=MUTED)
    ax.yaxis.set_major_locator(MultipleLocator(1))
    ax.yaxis.set_major_formatter(lambda v, _: f"${v:,.0f}")
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(
        handles[::-1],
        labels[::-1],
        loc="lower left",
        fontsize=7,
        frameon=False,
        labelcolor=INK2,
        ncols=3,
        handlelength=1.4,
        columnspacing=1.0,
        borderaxespad=0.1,
    )
    for name, _, _ in series[1:]:
        ax.annotate(
            money(BANKROLL + runs[name]["total"]),
            (len(order), BANKROLL + runs[name]["total"]),
            xytext=(-2, 4 if name == "no_margins" else -9),
            textcoords="offset points",
            ha="right",
            fontsize=7,
            color=INK2,
        )
    ax.annotate(
        money(BANKROLL),
        (len(order), BANKROLL),
        xytext=(-2, 4),
        textcoords="offset points",
        ha="right",
        fontsize=7,
        color=INK2,
    )

    # B. What stopped the signals
    ax = chart_axes(
        fig,
        (0.72, 0.60, 0.22, 0.165),
        "What stopped each signal",
        "As built. This recording has no cache ages, so each counts as 30 s old",
        head_x=0.56,
    )
    items = as_built["reasons"].most_common()
    labels = [REASONS.get(k, k) for k, _ in items][::-1]
    vals = [v for _, v in items][::-1]
    ax.barh(labels, vals, color=C1, height=0.5)
    for i, v in enumerate(vals):
        ax.text(v + 0.2, i, str(v), va="center", fontsize=7.5, color=INK2)
    ax.grid(False)
    ax.spines["bottom"].set_visible(False)
    ax.set_xticks([])
    ax.tick_params(axis="y", colors=INK2, labelsize=7.5)
    ax.set_xlim(0, max(vals) * 1.2)

    # C. Every game the recorder actually watched
    watched = engine["watched"]
    fig.text(
        0.06,
        0.532,
        f"Inside the {len(watched)} games the recorder watched for 15+ minutes",
        fontsize=9.5,
        fontweight="bold",
        color=INK,
        va="bottom",
    )
    fig.text(
        0.06,
        0.529,
        "Chance the first-named player wins, under research settings. Vertical lines mark breaks and "
        "completed sets.",
        fontsize=7.5,
        color=INK2,
        va="top",
    )

    def to_num(ns: int) -> float:
        return mdates.date2num(dt.datetime.fromtimestamp(ns / NS_PER_S, tz=dt.UTC))

    def segments(rows: list[tuple[int, float]]) -> list[list[tuple[int, float]]]:
        segs: list[list[tuple[int, float]]] = [[]]
        for row in rows:
            if segs[-1] and row[0] - segs[-1][-1][0] > 300 * NS_PER_S:
                segs.append([])
            segs[-1].append(row)
        return segs

    pw, pgap, px = 0.2, 0.015, 0.095
    for i, key in enumerate(watched):
        g = pool[key]
        one = game_series(engine, key)
        ax = fig.add_axes((px + i * (pw + pgap), 0.345, pw, 0.125))
        style(ax)
        fig.text(
            px + i * (pw + pgap),
            0.487,
            f"{surname(g['long'])} vs {surname(g['short'])}",
            fontsize=8,
            fontweight="bold",
            color=INK,
            va="bottom",
        )
        fig.text(
            px + i * (pw + pgap), 0.484, f"Final {g['final']}", fontsize=7, color=MUTED, va="top"
        )
        t0, t1 = g["first"] - 60 * NS_PER_S, g["last"] + 60 * NS_PER_S
        for j, seg in enumerate(segments([m for m in one["mids"] if t0 <= m[0] <= t1])):
            ax.plot(
                [to_num(t) for t, _ in seg],
                [v for _, v in seg],
                color=C1,
                linewidth=1.2,
                label="Market price" if j == 0 else None,
            )
        for j, seg in enumerate(segments(one["model"])):
            ax.step(
                [to_num(t) for t, _ in seg],
                [v for _, v in seg],
                where="post",
                color=C2,
                linewidth=1.2,
                label="TopSpin model" if j == 0 else None,
            )
        for t in one["events"]:
            if t0 <= t <= t1:
                ax.axvline(to_num(t), color=BASE, linewidth=0.6, zorder=0)
        ax.set_ylim(0, 1)
        ax.set_xlim(to_num(t0), to_num(t1))
        ax.yaxis.set_major_locator(MultipleLocator(0.5))
        ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
        if i:
            ax.tick_params(axis="y", labelleft=False)
        ax.xaxis.set_major_locator(MaxNLocator(3))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.tick_params(axis="x", labelsize=7)
        if i == 0:
            fig.legend(
                *ax.get_legend_handles_labels(),
                loc="center right",
                bbox_to_anchor=(0.945, 0.5375),
                ncols=2,
                fontsize=7,
                frameon=False,
                labelcolor=INK2,
                handlelength=1.4,
            )

    # D. Edge vs hurdle
    evaluated = [d for d in research["signals"].values() if (d.get("numbers") or {}).get("side")]
    evaluated.sort(key=lambda d: d["local_ts_ns"])
    ax = chart_axes(
        fig,
        (0.135, 0.095, 0.345, 0.165),
        "The edge never cleared the hurdle",
        f"The {len(evaluated)} signals priced under research settings, cents per contract",
        head_x=0.06,
    )
    rows = []
    for d in evaluated:
        n = d["numbers"]
        raw = n["raw_edge"] * 100
        hurdle = raw - n["net_edge_best"] * 100 + 2.0  # costs, margins and the 2 cent minimum
        match = pool[d["match_key"]]
        side = match["long"] if n["side"] == "YES" else match["short"]
        kind = "set" if n.get("event_kind") == "set_complete" else "break"
        rows.append((f"{surname(side)}, {kind}", raw, hurdle))
    rows = rows[::-1]
    ys = list(range(len(rows)))
    ax.barh(
        [y + 0.19 for y in ys],
        [max(r[1], 0) for r in rows],
        height=0.34,
        color=C1,
        label="Edge found",
    )
    ax.barh(
        [y - 0.19 for y in ys],
        [r[2] for r in rows],
        height=0.34,
        color=BASE,
        label="Needed to trade",
    )
    for y, r in zip(ys, rows, strict=True):
        ax.text(max(r[1], 0) + 0.12, y + 0.19, f"{r[1]:.1f}", va="center", fontsize=7, color=INK2)
        ax.text(r[2] + 0.12, y - 0.19, f"{r[2]:.1f}", va="center", fontsize=7, color=INK2)
    ax.set_yticks(ys, [r[0] for r in rows])
    ax.tick_params(axis="y", colors=INK2, labelsize=7.5)
    ax.grid(False)
    ax.spines["bottom"].set_visible(False)
    ax.set_xticks([])
    ax.set_xlim(0, max(r[2] for r in rows) * 1.13)
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.4, 0.02),
        ncols=2,
        fontsize=7,
        frameon=False,
        labelcolor=INK2,
        handlelength=1.2,
    )

    # E. What the loosened rules actually did
    x0 = 0.56
    fig.text(
        x0,
        0.292,
        "If the safety margins were removed",
        fontsize=9.5,
        fontweight="bold",
        color=INK,
        va="bottom",
    )
    for i, line in enumerate(
        (
            "Each row loosens more rules. Margins are the minimum edge, the",
            "model-doubt band and slippage allowances. Prices could be 30 s",
            "stale, so even these fills are generous.",
        )
    ):
        fig.text(x0, 0.289 - i * 0.0125, line, fontsize=7.5, color=INK2, va="top")
    y = 0.238
    fig.text(x0, y, "Rule set", fontsize=7.5, color=MUTED)
    fig.text(0.78, y, "Round trips", fontsize=7.5, color=MUTED, ha="right")
    fig.text(0.86, y, "Fees", fontsize=7.5, color=MUTED, ha="right")
    fig.text(0.94, y, "Result", fontsize=7.5, color=MUTED, ha="right")
    fig.add_artist(plt.Line2D([x0, 0.94], [y - 0.006, y - 0.006], color=BASE, linewidth=0.6))
    rows_e = [
        ("As built", "as_built"),
        ("Stale-data checks relaxed", "research"),
        ("Half margins", "half_margins"),
        ("No margins", "no_margins"),
    ]
    for i, (label, name) in enumerate(rows_e):
        yy = y - 0.021 - i * 0.0185
        r = runs[name]
        fig.text(x0, yy, label, fontsize=8, color=INK)
        fig.text(0.78, yy, str(len(r["trips"])), fontsize=8, color=INK2, ha="right")
        fig.text(0.86, yy, money(r["fees"]), fontsize=8, color=INK2, ha="right")
        fig.text(0.94, yy, signed(r["total"]), fontsize=8, color=INK, ha="right", fontweight="bold")
    notes = []
    for name in ("half_margins", "no_margins"):
        for t in runs[name]["trips"]:
            m = pool[t.match_key]
            who = surname(m["long"] if t.side == "YES" else m["short"])
            notes.append((name, who, float(t.net), t.settled))
    fig.text(x0, 0.143, "The what-if trades, by the player backed:", fontsize=7.5, color=INK2)
    for i, (name, who, net, settled) in enumerate(notes):
        tag = "held to settlement" if settled else "sold early"
        lab = "Half margins" if name == "half_margins" else "No margins"
        fig.text(x0, 0.1265 - i * 0.0155, f"{lab}: {who}, {tag}", fontsize=7.5, color=INK2)
        fig.text(0.94, 0.1265 - i * 0.0155, signed(net), fontsize=7.5, color=INK2, ha="right")
    pdf.savefig(fig, facecolor=SURFACE)
    plt.close(fig)


# ------------------------------------------------------------------ page 2


def draw_page2(pdf: PdfPages, engine: dict[str, Any], bench: dict[str, Any]) -> None:
    fig = plt.figure(figsize=(8.5, 11))
    ends = bench["ends"]
    coin = ends["coin"]
    pool, order, runs = engine["pool"], engine["order"], engine["runs"]

    def day(iso: str) -> str:
        d = dt.date.fromisoformat(iso)
        return f"{d:%b} {d.day}"

    page(
        fig,
        "Betting blindly instead: no better, and far bumpier",
        f"Blind bets with the same $2 limit on {bench['n']} real finished tour-level matches "
        f"({day(bench['start'])} to {day(bench['end'])}, 2026), at real pre-match prices and fees.",
        2,
    )

    below = sum(1 for x in coin if x < BANKROLL) / len(coin)
    w, gap, y0, h = 0.205, 0.0183, 0.835, 0.075
    mean, lo, hi = bench["per_game"]["coin"]
    tiles = [
        (money(pct(coin, 0.5)), f"Median after {SERIES_GAMES} coin-flip bets"),
        (f"{below:.0%}", "Chance of ending below $100"),
        (signed(mean), "Average result per $2 bet"),
        (f"{bench['cost'] * 100:.1f}¢", "Half spread + fee, per contract"),
    ]
    for i, (v, lab) in enumerate(tiles):
        tile(fig, 0.06 + i * (w + gap), y0, w, h, v, lab)

    # F. Distribution of ending balances
    ax = chart_axes(
        fig,
        (0.06, 0.60, 0.42, 0.165),
        f"{SERIES_RUNS:,} random series of {SERIES_GAMES} games",
        "Ending balance, betting a random side of each game, $2 at risk each",
    )
    ax.hist(coin, bins=44, color=C1, edgecolor=SURFACE, linewidth=0.4)
    ax.set_yticks([])
    ax.grid(False)
    ax.axvline(BANKROLL, color=INK, linewidth=1.1)
    ymax = ax.get_ylim()[1]
    ax.set_ylim(0, ymax * 1.12)
    ax.text(BANKROLL, ymax * 1.13, "TopSpin: $100", fontsize=7, color=INK, va="bottom", ha="center")
    ax.xaxis.set_major_formatter(lambda v, _: f"${v:,.0f}")

    # G. Sample paths
    ax = chart_axes(
        fig,
        (0.59, 0.60, 0.35, 0.165),
        "40 of those series, game by game",
        "Each gray line is one random series",
        head_x=0.56,
    )
    xs = list(range(SERIES_GAMES + 1))
    for path in bench["paths"]:
        ax.plot(xs, path, color=BASE, linewidth=0.6, alpha=0.8)
    ax.plot(
        xs, bench["median_path"], color=C2, linewidth=1.6, label=f"Median of all {SERIES_RUNS:,}"
    )
    ax.plot(xs, [BANKROLL] * len(xs), color=C1, linewidth=1.6, label="TopSpin, no trades")
    ax.set_xlim(0, SERIES_GAMES)
    ax.yaxis.set_major_formatter(lambda v, _: f"${v:,.0f}")
    ax.set_xlabel("Games", fontsize=7.5, color=MUTED)
    ax.legend(
        loc="upper left",
        fontsize=7,
        frameon=True,
        facecolor=SURFACE,
        edgecolor="none",
        framealpha=0.92,
        labelcolor=INK2,
        handlelength=1.4,
    )

    # H. Calibration
    ax = chart_axes(
        fig,
        (0.095, 0.345, 0.33, 0.17),
        "Market prices were close to right",
        "Pre-match price vs how often that player won. Bars: 95% range",
        head_x=0.06,
    )
    ax.plot([0, 1], [0, 1], color=BASE, linewidth=1.0, zorder=1)
    for mid, freq, err, _ in bench["bins"]:
        ax.plot(
            [mid, mid], [max(0, freq - err), min(1, freq + err)], color=C1, linewidth=1.0, zorder=2
        )
    ax.scatter(
        [b[0] for b in bench["bins"]],
        [b[1] for b in bench["bins"]],
        s=26,
        color=C1,
        edgecolors=SURFACE,
        linewidths=0.8,
        zorder=3,
    )
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.grid(True, axis="both", color=GRID, linewidth=0.6)
    ax.xaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    ax.set_xlabel("Market price", fontsize=7.5, color=MUTED)
    ax.text(0.97, 0.05, "On the line = fairly priced", fontsize=7, color=MUTED, ha="right")

    # I. Strategies
    ax = chart_axes(
        fig,
        (0.655, 0.345, 0.285, 0.17),
        f"Simple rules, {SERIES_GAMES} games each",
        "Median and middle 90% of ending balances",
        head_x=0.56,
    )
    names = [("dog", "Always underdog"), ("fav", "Always favorite"), ("coin", "Coin flip")]
    for i, (k, _) in enumerate(names):
        e = ends[k]
        ax.plot(
            [pct(e, 0.05), pct(e, 0.95)],
            [i, i],
            color=C1,
            linewidth=3.0,
            solid_capstyle="round",
            alpha=0.35,
        )
        ax.scatter([pct(e, 0.5)], [i], s=34, color=C1, edgecolors=SURFACE, linewidths=0.8, zorder=3)
        left = pct(e, 0.5) < BANKROLL
        ax.text(
            pct(e, 0.5) + (-0.8 if left else 0.8),
            i + 0.13,
            money(pct(e, 0.5)),
            fontsize=7,
            color=INK2,
            ha="right" if left else "left",
            va="bottom",
        )
    ax.axvline(BANKROLL, color=INK, linewidth=1.1)
    ax.text(BANKROLL, 2.72, "TopSpin: $100", fontsize=7, color=INK, ha="center", va="bottom")
    ax.set_yticks(range(3), [n[1] for n in names])
    ax.tick_params(axis="y", colors=INK2, labelsize=7.5)
    ax.set_ylim(-0.5, 2.7)
    ax.grid(False)
    ax.grid(True, axis="x", color=GRID, linewidth=0.6)
    ax.xaxis.set_major_formatter(lambda v, _: f"${v:,.0f}")

    # J. Game table
    y = 0.289
    fig.text(
        0.06,
        y,
        "Every recorded game in the series",
        fontsize=9.5,
        fontweight="bold",
        color=INK,
        va="bottom",
    )
    cols = [
        (0.06, "left", "#"),
        (0.085, "left", "Match"),
        (0.40, "left", "Final score"),
        (0.56, "right", "Watched"),
        (0.635, "right", "Signals"),
        (0.76, "right", "As built"),
        (0.85, "right", "Half margins"),
        (0.94, "right", "No margins"),
    ]
    y -= 0.015
    for x, ha, head in cols:
        fig.text(x, y, head, fontsize=7, color=MUTED, ha=ha)
    fig.add_artist(plt.Line2D([0.06, 0.94], [y - 0.004, y - 0.004], color=BASE, linewidth=0.6))
    rh = 0.0118
    for i, key in enumerate(order):
        g = pool[key]
        yy = y - 0.0145 - i * rh
        sigs = sum(1 for d in runs["as_built"]["signals"].values() if d["match_key"] == key)
        cells = [
            str(i + 1),
            f"{g['long']} vs {g['short']}",
            g["final"],
            f"{g['live_min']:.0f} min" if g["live_min"] >= 1 else "none",
            str(sigs),
            signed(runs["as_built"]["pnl"].get(key, 0.0)),
            signed(runs["half_margins"]["pnl"].get(key, 0.0)),
            signed(runs["no_margins"]["pnl"].get(key, 0.0)),
        ]
        for (x, ha, _), cell in zip(cols, cells, strict=True):
            fig.text(x, yy, cell, fontsize=7, color=INK if x == 0.085 else INK2, ha=ha)
    # K. Limits
    y = y - 0.0145 - len(order) * rh - 0.012
    fig.text(
        0.06, y, "How far to trust this", fontsize=9.5, fontweight="bold", color=INK, va="bottom"
    )
    mean_f, lo_f, hi_f = bench["per_game"]["fav"]
    mean_d, lo_d, hi_d = bench["per_game"]["dog"]
    watched = sum(1 for g in pool.values() if g["live_min"] >= 1)
    lines = [
        f"This does not show TopSpin has an edge, or that it lacks one. Only {watched} of the {len(pool)} games had "
        "any live coverage, because the laptop slept.",
        "Public data is cached for up to 30 s, and this recording has no cache ages, so as built each response counts "
        "as 30 s old. A fair test needs uncached data.",
        "The $0.50 minimum expected profit per order cannot be reached under a $2 limit, so it was scaled to $0.05 for "
        "this test.",
        "This page tests simple pre-match bets, not TopSpin's in-play strategy. It shows the cost of trading without "
        "an edge.",
        f"Per $2 game: coin flip {signed(mean)} (95% CI {signed(lo)} to {signed(hi)}), favorite {signed(mean_f)} "
        f"({signed(lo_f)} to {signed(hi_f)}), underdog {signed(mean_d)} ({signed(lo_d)} to {signed(hi_d)}). "
        "One 6-week sample.",
    ]
    for i, line in enumerate(lines):
        fig.text(0.06, y - 0.014 - i * 0.0132, line, fontsize=7.3, color=INK2)
    pdf.savefig(fig, facecolor=SURFACE)
    plt.close(fig)


def main() -> None:
    plt.rcParams.update(
        {
            "font.family": ["Segoe UI", "DejaVu Sans"],
            "pdf.fonttype": 42,
            "axes.unicode_minus": False,
            "text.parse_math": False,
        }
    )
    engine = run_engine()
    bench = run_benchmark()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(OUT) as pdf:
        draw_page1(pdf, engine)
        draw_page2(pdf, engine, bench)
        meta = pdf.infodict()
        meta["Title"] = "TopSpin $100 test"
        meta["Subject"] = "Paper test on recorded and historical Polymarket US tennis data"
    summary = {
        "pool": len(engine["pool"]),
        "order": engine["order"],
        "watched": engine["watched"],
        "profiles": {
            k: {
                "total": v["total"],
                "fees": v["fees"],
                "trips": len(v["trips"]),
                "signals": len(v["signals"]),
                "reasons": dict(v["reasons"]),
            }
            for k, v in engine["runs"].items()
        },
        "bench": {k: bench[k] for k in ("n", "tournaments", "cost", "brier", "per_game", "bins")},
        "ends": {
            k: {
                "p05": pct(v, 0.05),
                "p50": pct(v, 0.5),
                "p95": pct(v, 0.95),
                "below_100": sum(1 for x in v if x < BANKROLL) / len(v),
            }
            for k, v in bench["ends"].items()
        },
    }
    Path("reports/TopSpin_100_dollar_test.json").write_text(
        json.dumps(summary, indent=1, default=str)
    )
    print(json.dumps(summary, indent=1, default=str))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
