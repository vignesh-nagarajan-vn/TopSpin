# Model methodology

## Markov hierarchy (`pricing/markov.py`)

Win probabilities come from exact dynamic programming over tennis scoring. No sampling or
LLM is involved.

1. **Point:** `p_a` = P(A wins a point on A's serve); `p_b` likewise for B.
2. **Game:** recursion over (server points, receiver points). Deuce is closed form:
   `p^2 / (p^2 + q^2)`. Advantage states are `p + q*D` or `p*D`.
3. **Tiebreak:** first to 7 or 10, win by two. Serve order is: the player due to serve serves
   point 1, then servers alternate every two points. At ties past target-1, the next two
   points are served once by each player, giving the closed form
   `pa(1-pb) / (pa(1-pb) + (1-pa)pb)`.
4. **Set:** a distribution over *(winner, server of the next set's first game)*. The next server
   is the player who did not serve the last game. After a tiebreak set it is the tiebreak's
   first receiver. Advantage final sets use a closed-form two-game cycle.
5. **Match:** composes set distributions, using the deciding-set rules for the final set.

Every result is checked to be finite and within [0, 1]. Validated properties, all tested: exact
reproduction of brute-force enumeration for games and tiebreaks, complementarity
`P_A(s) + P_B(mirror s) = 1`, terminal values exactly 0 or 1, rejection of impossible states.

## Rulesets (`pricing/rulesets.py`, `config/tournaments.yaml`)

| Ruleset | Used for | Format |
| - | - | - |
| `tour_bo3_tb7` | ATP and WTA tour-level singles | Best of 3; 7-point tiebreak at 6-6 in every set |
| `slam_men_bo5_final_tb10` | Men's Grand Slam main draw | Best of 5; 10-point tiebreak at 6-6 in set 5 |
| `slam_women_bo3_final_tb10` | Women's Grand Slam main draw | Best of 3; 10-point tiebreak at 6-6 in set 3 |

Tournaments are classified from the provider's `tournamentName`. Challenger, ITF, WTA 125,
doubles, qualifying, team events and exhibitions are excluded. An unclassifiable match is never
traded. Re-verify formats against official sources each season.

## Frozen prematch anchor (v1 baseline)

1. While the match is `NS`, every valid book becomes a candidate if it is two-sided, spread
   ≤ 0.04, depth ≥ 50 contracts within two ticks on each side, mid in [0.05, 0.95], and market
   open.
2. A candidate is *confirmed prematch* only if a later score snapshot still reports `NS`.
3. At the first in-play snapshot, the last confirmed candidate that is at least 30 s older than
   that snapshot (a margin for a provider whose "not started" lags the first serve) is frozen,
   provided it is ≤ 300 s old.
4. If the match was first observed already in play, or no valid candidate exists, the match is
   skipped. The anchor is never back-solved from in-play prices and never refit.

## Calibration (`pricing/calibration.py`)

One win probability cannot identify two serve probabilities. With a tour baseline `b`
(ATP 0.645, WTA 0.565; these are assumptions):

```
logit(p_a) = logit(b) + θ/2      logit(p_b) = logit(b) − θ/2
```

`θ` is found by Brent's method so that the prematch probability, averaged over the unknown
coin-toss first server, reproduces the anchor to 1e-6. Bounds keep both probabilities in
[0.40, 0.85]. An unreachable anchor, non-convergence or a non-monotone surface raises
`CalibrationError`, and the match is skipped.

## Uncertainty band (`pricing/uncertainty.py`)

The model is refit for every combination of anchor ± half the prematch spread and baseline
± 0.03. Each state is priced under all nine fits; the band is [min − 0.01, max + 0.01]. Entries
use the band edge least favourable to the trade. Matches with band width > 0.15 are skipped.

## Important limitation

**The anchored model is market-informed.** It removes in-play market sentiment from the live
recalculation, but prematch consensus remains in the strength estimate. It is not a
sentiment-free model. A market-independent model (surface Elo, opponent-adjusted serve and
return rates) has an interface (`pricing/fundamentals.py`) but no implementation: no licensed
historical dataset is available and player data is never fabricated. If one is added later, it
must be reported separately and never blended with live prices.

Other unmodelled effects: retirement hazard (the retiring player's side loses), momentum,
fatigue, injury, conditions, and point-level serve variation. These are reasons for the band
floor and the position limits, not things the model captures.
