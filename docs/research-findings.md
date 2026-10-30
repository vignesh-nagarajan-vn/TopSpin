# Research findings

**Bottom line (2026-10-30): no trading edge has been demonstrated.** The first real-data session
captured too little live play for a verdict. What it did capture points the wrong way for the v1
hypothesis: the venue's public score feed reaches us about **15 s** after the provider's own
update, and in the one break with a material price move, the market had fully repriced before we
received the score. Until Gates 1, 3 and 4 pass on real data, nothing should be traded with real
money.

## Gate status

| Gate | Status | Notes |
| - | - | - |
| 1. Data quality | **In progress** | Discovery, mapping, books, timestamps and event detection all work on real data. The latency study has 1 of the 30 break/set events it needs. |
| 2. Mathematical correctness | **Passed (tests)** | 185 automated tests: scoring, best of 3 and 5, tiebreak rotation, first-to-7/10, slam deciders, calibration, fees (documented examples), sizing, risk, broker, replay determinism. |
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

## First real-data session (2026-10-30)

Record mode ran from 06:08 to 17:48 UTC over 22 ATP/WTA markets (Basel, Vienna, Guangzhou,
Tokyo, and one Challenger that was correctly excluded). Replaying the 49,593 recorded inputs
through the engine is deterministic (same fingerprint on every run).

**Coverage was poor because the laptop slept.** Windows Modern Standby suspended the recorder
from 07:20 to 08:04, 08:31 to 16:47 and 16:47 to 17:37 UTC. The longest silence was 8.25 hours.
Most matches went from "not started" straight to "finished" in the data. The recorder now
journals a `suspension_detected` record and distrusts every book after such a gap, but the
machine itself must stay awake (see the runbook).

| Measurement | Result |
| - | - |
| Live score snapshots with sets, games, points, server, status and timestamp | 100% of captured live snapshots |
| Score feed delay: provider `updatedAt` to our first receipt (45 live updates) | median **15.2 s**, p90 29.9 s, max 134 s |
| Order book request round trip (26,963 requests) | median 93 ms, p90 171 ms |
| Events detected | 2 converted breaks, 3 set completions, 5 holds, 6 recording gaps |
| Prematch anchors | 0 of 11 frozen: every first serve happened while the machine slept (`prematch_book_too_old`) |
| Orders | 0 (every opportunity lacked an anchor) |
| Latency study verdict | `insufficient_data` (1 break/set event with a material move; 30 needed) |

The individual observations, from the latency study, give the YES mid at receipt offsets:

* **Eala vs Shnaider, break (08:31:00):** 0.595 at −60 s, 0.685 by −10 s, unchanged afterwards.
  The whole move happened **before** our feed reported the break (pre-receipt share 1.0).
* **Kym vs Lehecka, hold for B (17:44:52):** 0.185 to 0.165 before receipt, flat after (share
  1.0).
* **Kym vs Lehecka, hold for A (17:41:20):** a 1-tick move after receipt (share 0.0). This is a
  hold, which is not an entry signal.

Three observations prove nothing statistically. Combined with a feed that is typically 15 s
behind the provider, they make it unlikely that this score feed can support a reaction strategy.
A faster licensed point-by-point feed would be required to test the hypothesis properly.

The replay also exposed two analytics bugs, now fixed and tested:

* Feed delay was measured from the last state *change*, which counted repeated polls of an
  unchanged state (a median of "31 minutes"). It now counts only the first receipt of each
  provider update.
* The latency study could bridge a recording gap with a book 50 minutes old. Books older than
  10 s now count as missing.

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

1. **Feed speed.** The implemented score feed is the venue's own Sportradar-derived state. It
   arrives about 15 s after the provider's update even though we poll every 2-3 s. Professional
   market makers have faster point-by-point data, and the first real observation shows the market
   moving first. If more data confirms this, the break/set strategy is rejected. That would be a
   valid outcome.
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
