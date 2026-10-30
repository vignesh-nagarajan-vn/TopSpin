# Research findings

**Bottom line (2026-10-30): no trading edge has been demonstrated.** The system can now collect
the evidence needed to decide. Until Gates 1, 3 and 4 pass on real data, nothing should be
traded with real money.

## Gate status

| Gate | Status | Notes |
| - | - | - |
| 1. Data quality | **In progress** | Real markets discovered, mapped and recorded. The latency study needs at least 30 break/set events with material price moves. |
| 2. Mathematical correctness | **Passed (tests)** | 182 automated tests: scoring, best of 3 and 5, tiebreak rotation, first-to-7/10, slam deciders, calibration, fees (documented examples), sizing, risk, broker, replay determinism. |
| 3. Replay profitability | Not started | Needs at least 50 matches in a sample *later* than the one used to set thresholds. |
| 4. Forward paper | Not started | Needs at least 30 matches across at least 3 tournaments. |
| 5. Human approval | Never automatic | |

## What has been verified with real data

* Polymarket US lists ATP and WTA match-winner markets (45 active on 2026-10-30) with
  executable books. Typical spreads on tour-level matches were 1 tick (0.01). No set-winner
  markets were listed.
* The venue's public event payload carries a live score (sets, games, points, server, status)
  that parses cleanly. On real payloads, the parser returns points as *unknown* when the score
  string's column order cannot be proven, rather than guessing.
* A 90-second paper smoke run tracked 8 real markets: 305 book snapshots, 200 score snapshots,
  0 feed gaps. Replaying its journal reproduces the same mapping decisions.
* The tracked "ATP" and "WTA" series include Challenger and WTA 125 events. Discovery excludes
  them via the tournament name, or the event tags before play.
* Fees: every tennis market observed carried `feeCoefficient = 0.0695`, matching the schedule.

## What synthetic tests show (mechanics only, not evidence of an edge)

Synthetic, clearly fake scenarios in `tests/synthetic.py`:

* If the book lags the score feed by 5 s after a converted break, the engine buys at the stale
  ask (0.60 vs model fair 0.73) within risk limits and is filled after the simulated delay.
* If the market reprices **before** the feed delivers the break, which is the realistic
  concern, the engine skips with `market_already_repriced`.
* With 8 s of execution latency, the lagging-market opportunity disappears entirely. The
  sensitivity grid makes this kind of fragility visible.

These scenarios only confirm the plumbing. Whether the real market lags our feed is exactly what
the latency study measures.

## Key risks to the v1 hypothesis

1. **Feed speed.** The implemented score feed is the venue's own Sportradar-derived state,
   polled every 2 s. Professional market makers likely have faster point-by-point data. The prior
   expectation is that the market moves first, so the break/set strategy would then be rejected.
   That would be a valid outcome.
2. **Model information.** A frozen prematch anchor plus a constant-serve Markov model ignores
   in-match information the market sees (injury, momentum, serve speed). Disagreement with the
   market is often information, not mispricing.
3. **Retirement.** A player who retires while leading loses the contract. The model does not
   price this.
4. **Fees.** At p ≈ 0.5 the taker fee is about 1.7¢ per contract per side; a round trip costs
   about 3.5¢ before spread. Only large, fast dislocations can clear that.

## How to produce the real findings

```bash
python -m topspin.cli record --duration 57600          # a full day of matches
python -m topspin.cli replay data/journal              # regenerate decisions
python -m topspin.cli report data/journal --source replay --write-gate-evidence
python -m topspin.cli sensitivity data/journal
```

The report's latency-study verdict is one of:

* `not_viable`: median pre-receipt share ≥ 0.8.
* `potentially_viable`: post-receipt drift toward the event winner is greater than 2¢.
* `inconclusive`.
* `insufficient_data`.
