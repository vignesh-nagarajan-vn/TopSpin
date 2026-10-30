# Risk controls

The risk engine (`risk/limits.py`) is independent of the strategy. A strategy proposal is only
a request.

## Pre-trade checks (every entry)

* Halt latch tripped, emergency-stop file present, global breakers (such as `engine_error`), or
  match breakers.
* Price on tick and inside (0, 1); quantity a multiple of the market minimum.
* **Contradictory exposure:** an open position or working order in the same match that favours a
  different player is rejected. A second working order in the same market is rejected too.
* **Per-match budget:** the maximum settlement loss of positions plus working orders on the match,
  plus the new order, must be ≤ 2% of bankroll. Match-winner and set-winner exposure share this
  budget and are never netted.
* **Portfolio budget:** ≤ 10% of bankroll across all matches.
* Working (unmatched) opening orders in total ≤ 5% of bankroll; sufficient cash after working
  orders.
* Live mode only: the per-order value cap.

Sizing (`risk/sizing.py`) is quarter Kelly on the fee-inclusive binary payoff
(`f* = (q − c)/(1 − c)`, `c` = price + fee per contract), using the conservative probability
minus haircuts. It is capped by every budget and rounded **down** to the quantity step. An order
smaller than the market minimum is rejected, never rounded up.

Exits (`authorize_exit`) are allowed while entries are halted because they reduce risk, but they
can never exceed or flip a position.

## Circuit breakers (`risk/circuit_breaker.py`, engine)

New entries stop when any of these is true: daily loss limit; stale market data; stale score
data; mapping rejected; market suspended, closed or not open; model or calibration failure;
non-finite probabilities; unknown or unverified server; unsupported tournament; score
correction or unreliable score; book unsynchronised, crossed or invalid; engine exception; manual
halt.

On suspension, retirement, walkover, cancellation or match completion, the engine cancels the
match's working orders and blocks entries. It **never** assumes a refund. Settlement comes from
the exchange record. In paper mode, a provisional settlement at the score-derived winner is
applied for completed matches only.

No control relies on a stop-loss being executable: nothing is sold into a suspended or
untradeable book.

The stale-data breakers measure **data age**, including time in the CDN cache on public market
data, so a book received just now but cached for 29 s is stale. Older data from another cache node
is dropped rather than treated as a score correction or a price change. On the public route most
responses are too old to act on, so entries are rare by design.

## Persistent daily-loss latch

* The trigger is conservative equity (cash plus liquidation value of positions at executable
  bids) falling 5% below the UTC day's starting equity.
* The latch is written to `state/halt_latch.json` with an append-only history. It survives
  restarts and does not reset at midnight.
* Only `topspin reset-halt --operator NAME --reason TEXT` can clear it. The command refuses
  to run without an interactive terminal, requires typing `RECONCILED`, and records the operator,
  reason and time. No automated code path calls the reset.

## Emergency controls

* `topspin halt --reason ...` trips the latch and creates the emergency-stop file.
* Live kill switch (`orchestration/live_runner.kill_switch`): trips the latch and stop file,
  cancels **bot-managed** orders only (identified by exchange ids recorded locally), reports
  unrelated (human) orders and positions untouched, and never submits liquidation orders.
* A cancel is counted only when the order reads back as canceled or terminal. An unconfirmed
  cancel leaves reconciliation dirty and trips the latch.
