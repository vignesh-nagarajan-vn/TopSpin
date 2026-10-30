# Operator runbook

## Install

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv -e ".[dev]"
```

Record and paper modes need **no credentials**. Never put keys in files under version control.

## Daily research workflow

```bash
python -m topspin.cli discover --show-rejected     # what is tradeable right now
python -m topspin.cli record --duration 28800      # Gate 1 data collection (8 h)
python -m topspin.cli paper                        # forward paper trading until Ctrl-C
python -m topspin.cli report data/journal --source paper
python -m topspin.cli replay data/journal          # regenerate decisions from inputs
python -m topspin.cli replay data/journal --taker-delay-ms 1500 --fill-fraction 0.25 --slippage-ticks 1
```

**Keep the recording machine awake.** On 2026-10-30 a laptop entering Modern Standby suspended
the recorder for most of a day, and every prematch anchor was lost. Run long sessions on an
always-on machine, or plugged in with sleep disabled for the session. The recorder journals a
`suspension_detected` health record after any silence longer than 5 s. The report lists the
largest recording gaps.

Journals are in `data/journal/*.jsonl` (append-only); reports are in `reports/`. Re-index into
SQLite for ad-hoc queries with `index-db data/journal`.

Choosing thresholds on a sample and then reporting on the same sample is not validation. Keep a
chronological split: design on early journals, report on later ones (Gate 3).

## Promotion gates

1. **Data quality:** run `report --write-gate-evidence` on record journals. It needs real
   markets, valid mappings, books, timestamps, measured latency, detected breaks and sets, a
   latency verdict that is not negative, and **uncached market data**. Recordings over the public
   REST route cannot pass, because that data is cached for up to 30 s.
2. **Mathematical correctness:** `pytest` (CI).
3. **Replay profitability:** at least 50 matches in a *later* sample, with the match-clustered
   bootstrap lower bound of mean P&L above 0. It must also survive the stressed replays above.
4. **Forward paper:** at least 30 matches across at least 3 tournaments, with the clustered lower
   bound above 0.
5. **Human approval:** never set by software.

`gate-status` prints the computed evidence. A failed gate is an acceptable outcome. Do not
loosen thresholds to pass one.

## Emergency halt and reset

```bash
python -m topspin.cli halt --reason "feed looks wrong"
# investigate, reconcile positions/orders/balances against the exchange, then (interactive only):
python -m topspin.cli reset-halt --operator "Your Name" --reason "reconciled; feed restored"
```

## Live mode (Gate 5), only after Gates 1, 3 and 4 pass

v1 implements the live adapter, startup checks, scoped cancellation and the kill switch, but
**order routing from the engine to the exchange is deliberately not enabled.** No evidence of an
edge exists yet. Enabling it is a separate, reviewed change.

Preconditions, all checked by `live-check`:

1. Confirm your Polymarket US account is verified and that you are eligible to trade from your
   jurisdiction. Do not use VPNs or proxies.
2. Provide credentials through the environment or a secret manager only:
   `POLYMARKET_KEY_ID`, `POLYMARKET_SECRET_KEY`. Never write them to files in the repository,
   logs or tickets.
3. Copy `config/live.example.yaml` to `config/live.yaml` (git-ignored). Set by hand:
   `live.enabled: true`, a positive `live.max_order_value`, and the real `risk.bankroll`.
4. Write `config/live_authorization.yaml` (git-ignored) by hand:

   ```yaml
   operator: "Your Name"
   jurisdiction: "US-XX"
   exchange_eligibility_confirmed: true
   api_permissions_confirmed: true
   reviewed_risk_settings: true
   understands_potential_losses: true
   max_order_value: "25"        # must equal live.max_order_value
   bankroll: "500"              # must equal risk.bankroll
   authorized_on: 2026-11-01
   valid_until: 2026-11-30
   statement: "I authorize live trading with real money and accept that I may lose it."
   ```

5. `python -m topspin.cli live-check -c config/default.yaml -c config/live.yaml`

On startup, live mode verifies authorization, account access, reconciles open orders, cancels
only bot-recorded orders and confirms each cancel, then reconciles positions and balances. Any
unresolved order or failed cancel trips the latch and refuses to continue.
