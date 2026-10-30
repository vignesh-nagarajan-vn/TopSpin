"""Command-line interface: ``topspin --help``."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Annotated

import typer

from topspin.config import AppConfig, default_config_paths, load_config

app = typer.Typer(add_completion=False, no_args_is_help=True, help="TopSpin (Polymarket US).")

ConfigOpt = Annotated[
    list[Path] | None, typer.Option("--config", "-c", help="Config files, merged in order.")
]


def _cfg(mode: str, config: list[Path] | None) -> AppConfig:
    paths = config or default_config_paths(mode)
    return load_config(*paths)


def _records(paths: list[Path]) -> list[dict]:
    from topspin.storage.journal import read_journal

    files: list[Path] = []
    for p in paths:
        files.extend(sorted(p.glob("*.jsonl")) if p.is_dir() else [p])
    if not files:
        raise typer.BadParameter("no journal files found")
    return list(read_journal(files))


@app.command("validate-config")
def validate_config(mode: str = "paper", config: ConfigOpt = None) -> None:
    """Load and validate configuration; print the effective values."""
    cfg = _cfg(mode, config)
    typer.echo(json.dumps(cfg.model_dump(mode="json"), indent=2))


@app.command()
def discover(config: ConfigOpt = None, show_rejected: bool = False) -> None:
    """List eligible Polymarket US tennis markets right now (public data, no credentials)."""
    from topspin.discovery.markets import discover as run_discover
    from topspin.discovery.tournaments import TournamentClassifier
    from topspin.feeds.polymarket_us import PolymarketUSPublicClient
    from topspin.timeutil import now_ns

    cfg = _cfg("record", config)

    async def go() -> None:
        client = PolymarketUSPublicClient(
            cfg.exchange.public_base_url, per_second=cfg.exchange.max_requests_per_second
        )
        try:
            res = await run_discover(
                client,
                cfg,
                TournamentClassifier.from_file(cfg.discovery.tournaments_file),
                now_ns(),
            )
        finally:
            await client.aclose()
        typer.echo(
            f"candidates: {len(res.candidates)}  selected: {len(res.selected)}  rejected: {len(res.rejected)}"
        )
        for m in res.selected:
            typer.echo(
                f"  {m.slug:45s} {m.tour} {res.statuses.get(m.event_slug):12s} "
                f"{m.long.name} vs {m.short.name}  bid={m.best_bid} ask={m.best_ask} "
                f"tournament={m.tournament_name or m.tournament_hint or '?'}"
            )
        if show_rejected:
            for slug, why in res.rejected:
                typer.echo(f"  REJECTED {slug}: {why}")

    asyncio.run(go())


@app.command()
def record(
    duration: float | None = typer.Option(None, help="Seconds; default runs until Ctrl-C."),
    config: ConfigOpt = None,
) -> None:
    """Record mode: collect markets, books and scores to the journal. No signals, no orders."""
    from topspin.orchestration.recorder import run_recorder

    logging.basicConfig(level=logging.WARNING)
    stats = asyncio.run(run_recorder(_cfg("record", config), duration))
    typer.echo(
        f"recorded {stats.inputs} inputs ({stats.books} books, {stats.scores} scores, {stats.gaps} gaps)"
    )


@app.command()
def paper(
    duration: float | None = typer.Option(None, help="Seconds; default runs until Ctrl-C."),
    config: ConfigOpt = None,
) -> None:
    """Paper mode: live public data, real signals, simulated pessimistic execution."""
    from topspin.orchestration.paper import run_paper

    logging.basicConfig(level=logging.WARNING)
    stats = asyncio.run(run_paper(_cfg("paper", config), duration))
    typer.echo(f"paper run: {stats.inputs} inputs ({stats.books} books, {stats.scores} scores)")


@app.command()
def replay(
    journal: list[Path],
    config: ConfigOpt = None,
    out: Path | None = None,
    taker_delay_ms: int | None = None,
    fill_fraction: float | None = None,
    slippage_ticks: int | None = None,
) -> None:
    """Deterministically replay recorded inputs through the engine (with optional stress)."""
    from topspin.analytics.reports import build_report, write_report
    from topspin.discovery.tournaments import TournamentClassifier
    from topspin.simulation.replay import replay_records

    overrides: dict = {"simulation": {}}
    if taker_delay_ms is not None:
        overrides["simulation"]["taker_delay_ms"] = taker_delay_ms
    if fill_fraction is not None:
        overrides["simulation"]["paper_fill_fraction"] = fill_fraction
    if slippage_ticks is not None:
        overrides["simulation"]["slippage_stress_ticks"] = slippage_ticks
    paths = config or default_config_paths("paper")
    cfg = load_config(*paths, overrides=overrides)
    recs = _records(journal)
    res = replay_records(cfg, recs, TournamentClassifier.from_file(cfg.discovery.tournaments_file))
    rep = build_report(res.journal.records, float(cfg.risk.bankroll), source="replay")
    _, mp = write_report(rep, out or Path(cfg.operations.report_dir), "replay_report")
    typer.echo(f"replayed {res.inputs} inputs; fingerprint {res.fingerprint()[:16]}; report {mp}")


@app.command()
def sensitivity(journal: list[Path], config: ConfigOpt = None, out: Path | None = None) -> None:
    """Replay journals under a grid of harsher latency / fill / slippage assumptions."""
    from topspin.analytics.sensitivity import sensitivity_grid
    from topspin.discovery.tournaments import TournamentClassifier

    cfg = _cfg("paper", config)
    rows = sensitivity_grid(
        cfg, _records(journal), TournamentClassifier.from_file(cfg.discovery.tournaments_file)
    )
    out_dir = out or Path(cfg.operations.report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sensitivity.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    for r in rows:
        typer.echo(json.dumps(r))


@app.command()
def report(
    journal: list[Path],
    source: str = "paper",
    config: ConfigOpt = None,
    out: Path | None = None,
    write_gate_evidence: bool = False,
) -> None:
    """Build the research report (P&L, execution, latency study, gates) from journals."""
    from topspin.analytics.reports import build_report, write_report

    cfg = _cfg("paper", config)
    rep = build_report(_records(journal), float(cfg.risk.bankroll), source=source)
    out_dir = out or Path(cfg.operations.report_dir)
    jp, mp = write_report(rep, out_dir, f"{source}_report")
    if write_gate_evidence:
        (Path(cfg.live.gate_evidence_file)).parent.mkdir(parents=True, exist_ok=True)
        Path(cfg.live.gate_evidence_file).write_text(
            json.dumps(rep["gates"], indent=2, default=str), encoding="utf-8"
        )
    typer.echo(f"wrote {jp} and {mp}")
    typer.echo(f"latency verdict: {rep['latency_study']['verdict']}")


@app.command()
def price(
    anchor: float,
    tour: str = "ATP",
    ruleset: str = "tour_bo3_tb7",
    sets: str = "0-0",
    games: str = "0-0",
    points: str = "0-0",
    server: str = "A",
    half_spread: float = 0.01,
) -> None:
    """Price a score state with the frozen-anchor Markov model (A = YES player)."""
    from topspin.pricing.markov import ScoreState
    from topspin.pricing.rulesets import ruleset_from_config
    from topspin.pricing.uncertainty import AnchoredMatchModel

    cfg = _cfg("paper", None)
    rs = ruleset_from_config(ruleset)
    model = AnchoredMatchModel(
        anchor,
        half_spread,
        rs,
        cfg.model.serve_baseline[tour.upper()],
        base_delta=cfg.model.serve_baseline_delta,
        floor=cfg.model.uncertainty_floor,
    )
    sa, sb = map(int, sets.split("-"))
    ga, gb = map(int, games.split("-"))
    pa, pb = map(int, points.split("-"))
    est = model.match_estimate(ScoreState(sa, sb, ga, gb, pa, pb, server.upper()))  # type: ignore[arg-type]
    typer.echo(
        json.dumps(
            {
                "p_serve_a": model.central.p_a,
                "p_serve_b": model.central.p_b,
                "fair": est.fair,
                "low": est.low,
                "high": est.high,
            },
            indent=2,
        )
    )


@app.command()
def halt(
    reason: str = typer.Option(..., help="Why entries are being halted."), config: ConfigOpt = None
) -> None:
    """Emergency halt: trip the persistent latch and create the emergency-stop file."""
    from topspin.risk.circuit_breaker import HaltLatch
    from topspin.timeutil import now_ns

    cfg = _cfg("paper", config)
    HaltLatch(cfg.risk.halt_latch_path).trip(f"manual:{reason}", now_ns())
    stop = Path(cfg.operations.emergency_stop_file)
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text(reason, encoding="utf-8")
    typer.echo("halted: new entries blocked until a human runs reset-halt")


@app.command("reset-halt")
def reset_halt(
    operator: str = typer.Option(...), reason: str = typer.Option(...), config: ConfigOpt = None
) -> None:
    """Human-only reset of the halt latch. Requires an interactive terminal and explicit
    confirmation that a fresh reconciliation was performed."""
    from topspin.risk.circuit_breaker import HaltLatch
    from topspin.timeutil import now_ns

    if not sys.stdin.isatty():
        typer.echo("refusing: reset-halt must be run interactively by a human operator", err=True)
        raise typer.Exit(2)
    cfg = _cfg("paper", config)
    latch = HaltLatch(cfg.risk.halt_latch_path)
    if not latch.halted:
        typer.echo("latch is not tripped")
        return
    typer.echo(f"latch tripped: {latch.reason}")
    phrase = typer.prompt(
        "Type RECONCILED to confirm positions, orders and balances were freshly reconciled"
    )
    if phrase.strip() != "RECONCILED":
        typer.echo("not confirmed; latch unchanged", err=True)
        raise typer.Exit(1)
    latch.reset(operator=operator, reason=reason, reconciled=True, ts_ns=now_ns())
    stop = Path(cfg.operations.emergency_stop_file)
    if stop.exists():
        stop.unlink()
    typer.echo("latch reset and recorded")


@app.command("gate-status")
def gate_status(config: ConfigOpt = None) -> None:
    """Show the computed promotion-gate evidence (Gate 5 is always human)."""
    cfg = _cfg("paper", config)
    p = Path(cfg.live.gate_evidence_file)
    if not p.exists():
        typer.echo("no gate evidence yet: run `topspin report <journal> --write-gate-evidence`")
        raise typer.Exit(1)
    typer.echo(p.read_text(encoding="utf-8"))


@app.command("live-check")
def live_check(config: ConfigOpt = None) -> None:
    """Show which live-mode preconditions fail (never starts trading)."""
    from topspin.live.safety import check_live_preconditions

    cfg = _cfg("live", config) if config else load_config("config/default.yaml")
    failures = check_live_preconditions(cfg, Path.cwd())
    if failures:
        typer.echo("live trading would be REFUSED:")
        for f in failures:
            typer.echo(f"  - {f}")
        raise typer.Exit(1)
    typer.echo("all live preconditions pass (order routing remains disabled in v1)")


@app.command("index-db")
def index_db(journal: list[Path], config: ConfigOpt = None) -> None:
    """Index journal files into the SQLite inspection database."""
    from topspin.storage.database import Database
    from topspin.storage.journal import read_journal

    cfg = _cfg("paper", config)
    db = Database(cfg.operations.db_path)
    total = 0
    for p in journal:
        for f in sorted(p.glob("*.jsonl")) if p.is_dir() else [p]:
            total += db.index_records(f.name, read_journal([f]))
    db.close()
    typer.echo(f"indexed {total} new records into {cfg.operations.db_path}")


if __name__ == "__main__":
    app()
