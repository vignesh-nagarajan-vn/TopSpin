# Assumptions and default parameters

All values live in `config/default.yaml` and are validated at startup (`config.py`). They are
**research defaults, not optimised allocations.** Changing them to make results look better
invalidates any out-of-sample claim.

## Strategy

| Parameter | Default | Rationale |
| - | - | - |
| `enabled_entry_events` | break, set_complete | v1 hypothesis: major state changes may be priced in late. |
| `min_net_edge` | 0.02 | $ per $1 contract *after* fees, band and haircuts. About 1.2× the maximum taker fee per contract (0.0174). |
| `min_expected_net_pnl` | $0.50 | Avoids trades whose expected value is rounding noise. |
| `max_spread` | 0.04 | Wider books imply thin liquidity or stale quotes. |
| `min_executable_depth` | 25 contracts | Depth counted only up to the worst acceptable price. |
| `adverse_selection_allowance` | 0.01 | Haircut for trading against better-informed flow. |
| `slippage_allowance_ticks` | 1 | Extra tick of price risk between decision and arrival. |
| `entry_window_s` | 15 | After this, an event is stale and its opportunity is gone. |
| `reentry_cooldown_s` / `reentry_min_edge_increment` | 120 s / +0.01 | Prevents churn from quote flicker. |
| `no_entry_above/below_prob` | 0.95 / 0.05 | Near-certain outcomes carry settlement and timing risk for little edge. |
| `maker_policy` | never | Queue position cannot be reconstructed from public data. |
| `allow_swing_ev_entry` | false (enforced) | No fitted market-response model exists. Entries use hold-to-settlement EV only. |
| `point_scalping`, `market_making`, `set_winner_trading` | false | Out of scope for v1. |

## Model

| Parameter | Default | Rationale |
| - | - | - |
| `serve_baseline` | ATP 0.645, WTA 0.565 | Approximate tour-average serve points won. **Assumption**; the band tests ± 0.03. |
| `uncertainty_floor` | 0.01 | Unmodelled effects (retirement, momentum, fatigue). |
| `max_band_width` | 0.15 | Too uncertain to trade. |
| `anchor_max_spread` / `anchor_min_depth` / `anchor_max_age_s` | 0.04 / 50 / 300 s | Anchor quality gates. |

## Risk

| Parameter | Default | Rationale |
| - | - | - |
| `bankroll` | $1,000 (virtual) | Paper default. |
| `max_risk_per_match_frac` | 2% | Shared by every instrument on the match; positions are never netted. |
| `max_portfolio_risk_frac` | 10% | Aggregate maximum settlement loss including fees. |
| `daily_loss_halt_frac` | 5% | Persistent latch; human reset only. |
| `kelly_multiplier` | 0.25 | Quarter Kelly on the fee-inclusive binary payoff. Validation caps it at 0.5. |
| `max_unmatched_order_exposure_frac` | 5% | Worst case of all working opening orders. |
| `live.max_order_value` | $0 | Live trading is impossible until a human sets this. |

## Simulation (deliberately pessimistic)

| Parameter | Default | Rationale |
| - | - | - |
| `taker_delay_ms` | 500 | Decision-to-exchange latency. |
| `fill_book_policy` | intersect | Fill only liquidity present in both the last book before arrival and the first book after it. |
| `paper_fill_fraction` | 0.5 | At most half of each displayed level, as makers pull quotes after events. |
| `cancel_delay_ms` | 300 | Cancels are not instant. |
| Maker queue | back of queue | Fill only on later trade prints. With no trade feed, makers never fill. |

## Data and feed

* Public scores and books are cached for up to 30 s, and tracking 12 markets gave real poll
  intervals of about 3.3 s (scores) and 2.1 s (books). `max_book_staleness_s` and
  `max_score_staleness_s` limit **data age**: receipt time minus the response's reported cache age.
* `unknown_cache_age_s` (default 30) is the age assumed for a public-route response that reports
  none, such as journals recorded before ages were captured. A cache hit with no `Age` header is
  assumed to be as old as `max-age` allows. Set it to 0 only for labelled research replays.
* The entry window (`entry_window_s`) runs from when the event data was current, not from receipt.
* The score string's point column order is inferred from `periodScores`. If ambiguous (all sets
  level), points are treated as unknown rather than guessed, and pricing uses the start of the
  current game.
* The provider's `servingTeamId` is cross-checked against serve rotation. Any contradiction
  blocks trading until the server is consistent again.
* The holding-time estimate in `priority_score` (about 10 games per set, 4 minutes per game) is a
  labelled screening heuristic, not a model.
