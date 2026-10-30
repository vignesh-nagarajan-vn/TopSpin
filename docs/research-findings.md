# Research findings

**Bottom line (2026-10-30): no trading edge has been demonstrated, and today's data cannot
settle the v1 hypothesis.** The public endpoints used by record and paper modes are cached by
Cloudflare for up to 30 s, so scores and order books both reach us stale. The engine now measures
that age and fails closed, and the latency study rejects this route as a valid source. In a
research-only replay, the engine evaluated 5 real entries and skipped all 5, and the model stayed
within about 2.6¢ of the market. Nothing should be traded with real money.

> **Correction.** An earlier version of this page and the README said the venue's score feed runs
> about 15 s behind the provider, with the market repricing first. That blamed the feed for what is
> almost entirely CDN caching, and the ordering of market and score moves cannot be established
> from cached data. An earlier version of the venue notes also said no read-only market-data scope
> is documented; the institutional API documents one.

## Gate status

| Gate | Status | Notes |
| - | - | - |
| 1. Data quality | **In progress** | Discovery, mapping and event detection work on real data. Public data is cached for up to 30 s, so the study returns `invalid_source` and the gate cannot pass on this route, however many events it collects. It has 6 of the 30 break/set moves it needs. |
| 2. Mathematical correctness | **Passed (tests)** | 212 automated tests: scoring, best of 3 and 5, tiebreak rotation, first-to-7/10, slam deciders, calibration, fees (documented examples), sizing, risk, broker, replay determinism. |
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
* The tracked "ATP" and "WTA" series include Challenger and WTA 125 events. Discovery excludes
  them via the tournament name, or the event tags before play.
* Fees: every tennis market observed carried `feeCoefficient = 0.0695`, matching the schedule.

## Real-data session (2026-10-30, 06:08 to 22:06 UTC)

Record mode ran over 25 ATP/WTA markets. Replaying the 118,707 recorded inputs through the engine
gives the same fingerprint on every run.

**Coverage had large holes because the laptop slept** (gaps of 8.25 h, 62, 60, 50, 44, 25 and
21 minutes). Useful live stretches came from Kym vs Lehecka, Zverev vs Fery, Struff vs Borges and
Berrettini vs de Minaur.

| Measurement | Result |
| - | - |
| Events detected | 12 converted breaks, 10 set completions, 30 holds, 8 recording gaps |
| Server changes vs serve rotation | 39 with 0 contradictions; break and hold labels checked by hand on 2 full match sequences |
| Live snapshot fields | Sets, games, server, status, timestamp 100%; points 90.7% (unknown when level scores make the column order unprovable) |
| Invalid scores / invalid books / failed book polls | 0 / 0 / 3 |
| Prematch anchors under the 300 s rule | 1 of 13 frozen (Berrettini vs de Minaur); that match had no break during its covered play |
| Orders under the default config | 0 |

### Public data is cached for up to 30 s

Both `/v1/events/slug/{slug}` and `/v1/markets/{slug}/book` return `Cache-Control: public,
max-age=30` with `cf-cache-status: HIT` and `Age` values up to 29 s. Polling faster does not give
fresher data. The observed score delay (provider `updatedAt` to our first receipt, 234 updates)
matches a simple model of a 0-30 s cache age plus our 3.3 s poll interval:

| Percentile | 10% | 25% | 50% | 75% | 90% | 99% |
| - | - | - | - | - | - | - |
| Observed (s) | 6.0 | 9.9 | 16.6 | 24.2 | 29.2 | 34.0 |
| Cache + polling model (s) | 4.7 | 9.1 | 16.6 | 24.2 | 28.7 | 31.9 |

Books show the same effect: receipt minus the exchange's `transactTime`, on changed books, has a
median of 8.3 s and a 90th percentile of 25.4 s. Request round trips (median 79 ms) mostly measure
the cache, not data age. The local clock is not the cause: it agrees with HTTP `Date` headers to
within about a second.

A 60-second live check after the fix confirmed this directly. Every response reported a cache
age (152 books, 88 scores), with medians of 15.5 s and 15.0 s and a range of 0-30 s. Only 14.5% of
books and 19.3% of scores were 5 s old or newer.

**What this means:**

1. **The feed's own speed is still unknown.** Caching and polling explain nearly all the measured
   delay, leaving about 1 s unexplained at the low percentiles.
2. **The latency study cannot order events within about 30 s.** Scores and books pass through
   separate caches. The 6 independent break/set moves had a median pre-receipt share of 0.94 in
   both the 60 s and 30 s windows, but that only describes what we saw through the caches. It
   also includes market moves on earlier points of the same game. The automated verdict is now
   `invalid_source`, whatever the numbers say.
3. **A reaction strategy on this public route is not workable,** whatever the true ordering is: a
   score is typically 17 s old when it arrives. This rejects the route, not the hypothesis.
4. **The engine now fails closed on this route.** Freshness checks, the event entry window, anchor
   timing and paper fills all use data age (receipt time minus the reported cache age), and a
   public-route response with no reported age is assumed to be 30 s old. Replaying this day's
   recording, which predates the capture of cache ages, gives no entries: all 12 tradeable events
   expire. Paper mode on the public route will skip nearly everything from now on, by design. No
   paper P&L has been produced so far.

### Research-only diagnostic: entry logic on real data

To exercise the entry logic, the anchor age limit was relaxed from 300 s to 2 h and the assumed
age of this recording's responses was set to 0 s (`feeds.unknown_cache_age_s: 0`). These prices
were still confirmed prematch, just old because the machine slept. Nothing else was relaxed, the
books are subject to the cache problem above, and this counts toward no gate. The results below
are identical before and after the cache fix.

| Time (UTC) | Match | Event | Side | Model fair | Price | Raw edge | Net edge | Outcome |
| - | - | - | - | - | - | - | - | - |
| 08:31 | Eala vs Shnaider | Break | YES | 0.693 | 0.69 | +0.3¢ | -5.5¢ | `edge_consumed_by_fees` |
| 17:54 | Kym vs Lehecka | Set | NO | 0.881 | 0.85 | +3.1¢ | -1.0¢ | `edge_below_threshold` |
| 19:04 | Struff vs Borges | Break | YES | 0.705 | 0.71 | -0.5¢ | -6.4¢ | `market_already_repriced` |
| 19:15 | Struff vs Borges | Set | NO | 0.221 | 0.19 | +3.1¢ | -1.3¢ | `edge_below_threshold` |
| 19:23 | Struff vs Borges | Break | NO | 0.082 | 0.05 | +3.2¢ | -1.5¢ | `edge_below_threshold` |

Other break and set events were skipped earlier, mainly as near-certain outcomes or recording
gaps. No orders were placed.

Across 152 model updates in 5 matches, the absolute gap between model fair value and market mid
had a median of 2.6¢ and a maximum of 7.8¢. A taker round trip at 50¢ costs about 3.5¢ in fees
before spread, so gaps of this size are not obviously tradeable. The sample is far too small to
generalize, and these matches come from one day.

### What a proper test needs

* **Uncached market data.** Two documented routes exist:
  * The retail WebSocket (`/v1/ws/markets`), which needs a retail API key. The retail docs mention
    no read-only scope.
  * The institutional Exchange API (`api.prod.polymarketexchange.com`, Private Key JWT, gRPC
    market data stream, a preprod environment), which documents `read:marketdata` and
    `read:l2marketdata` scopes but requires onboarding with Polymarket.
* **A score source outside the 30 s cache,** such as a licensed provider. Neither route above is
  documented as carrying live scores.
* **An always-on machine** and the 30 break/set events the study requires.

Bypassing the cache with changing query strings was not tried. The venue's guidance is to stream
rather than poll market data, and defeating its cache would work against that.

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

1. **Feed speed.** Professional market makers likely have faster point-by-point data than any
   public route. The real ordering of score and market moves is still unmeasured, because public
   data is cached for up to 30 s. If uncached data shows the market moves first, the break/set
   strategy is rejected. That would be a valid outcome.
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
