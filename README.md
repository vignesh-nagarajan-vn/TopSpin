# TopSpin

A research-first, risk-gated engine for trading ATP and WTA match-winner markets on
**Polymarket US**. Its first job is to find out honestly whether a tradeable edge exists after
fees, spreads, latency, slippage, model error and missed fills. A correct answer of "no edge" is
an acceptable outcome.

> **Status (2026-10-30):** record, paper and replay modes are built, tested (185 tests) and run
> against the live public API. **No edge has been demonstrated.** The first real session found
> the venue's score feed about 15 s behind the provider, with the market repricing first. Live
> order routing is intentionally not enabled. See [research findings](docs/research-findings.md).

## What it does

* **Discovers** live and upcoming ATP/WTA `tennis_match_winner` markets from public reference
  data. Tour-level events only: Challenger, ITF, WTA 125, doubles and qualifying are excluded.
* **Records** order books and the venue's embedded live tennis state (sets, games, points,
  server, status) into an append-only journal. Every record carries a local receipt time.
* **Prices** every validated score state with an exact Markov model (point, game, tiebreak, set,
  match), calibrated once to a frozen prematch market anchor, with a conservative uncertainty
  band.
* **Detects** converted breaks and completed sets with a deterministic state machine. It checks
  serve rotation, corrections, gaps and suspensions.
* **Evaluates** entries on executable prices only, after fees, the model band and haircuts. It
  sizes at quarter Kelly under per-match and portfolio budgets that never net positions.
* **Simulates** execution pessimistically: delayed arrival, liquidity present both before and
  after arrival, a partial-fill fraction, rounded per-fill fees, and back-of-queue maker fills.
* **Manages** positions by expected log wealth (hold, reduce or sell), never by midpoint.
* **Reports** P&L, executable markouts, match-clustered bootstrap intervals, skip reasons, model
  calibration, and the Gate 1 **latency study**: does the market reprice before our score feed
  delivers an event?

## Quick start

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv -e ".[dev]"
python -m topspin.cli discover                          # eligible markets now (no credentials)
python -m topspin.cli record --duration 3600
python -m topspin.cli replay data/journal               # deterministic replay + report
python -m topspin.cli report data/journal --source paper
python -m topspin.cli price 0.62 --games 2-0 --server A # price a score state
pytest                                                  # 185 tests, offline
```

All commands: `python -m topspin.cli --help`. Operations, the promotion gates, the halt
reset and live preconditions are in the [operator runbook](docs/operator-runbook.md).

## Safety properties

* Paper is the default mode. Record and paper never load credentials or import the live adapter
  (a test enforces this).
* `live.enabled: false` and `live.max_order_value: 0` by default. Live startup also needs
  environment credentials, a dated operator authorization file, and computed gate evidence.
* The daily-loss halt is persistent across restarts, and only an interactive human command
  resets it.
* Unknown market types, tournaments, servers, scores or rulesets cause a skip, never a guess.

## Documentation

* [Architecture](docs/architecture.md)
* [Venue verification](docs/venue-verification.md): what Polymarket US actually offers, as verified
* [Model methodology](docs/model-methodology.md): including the market-anchored limitation
* [Assumptions and defaults](docs/assumptions.md)
* [Risk controls](docs/risk-controls.md)
* [Operator runbook](docs/operator-runbook.md)
* [Research findings](docs/research-findings.md)
