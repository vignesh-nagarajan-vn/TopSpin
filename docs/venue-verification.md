# Venue verification: Polymarket US

Verified on 2026-10-29/30 against the official documentation at https://docs.polymarket.us
(`llms.txt` index, API reference, fee schedule, sports schema, sports FAQs) and by read-only
probing of the **public** endpoints. No account, credentials or orders were used.

## What exists today

| Question | Finding | Evidence |
| - | - | - |
| Are ATP/WTA match-winner markets listed? | **Yes.** 26 ATP and 19 WTA `tennis_match_winner` markets were active on 2026-10-30. | `GET gateway.polymarket.us/v1/events?seriesId=16` (ATP) and `17` (WTA) |
| Are set-winner markets listed? | **No** `tennis_set_*_winner` instruments were active. The enum values are documented, so they may appear later. | same survey |
| Does the "ATP" series contain only tour-level events? | **No.** It mixes ATP Challenger events with tour events; the WTA series includes WTA 125 events. Tournament filtering is mandatory. | `eventState.tennisState.tournamentName` values such as "ATP Challenger Seoul..." |
| Is there a public live score? | **Yes.** Tennis event payloads embed a Sportradar-derived state: `score` (`"6-3, 1-2:15-0"`), `periodScores` per competitor id, `period` (`NS`, `S1`-`S5`, `SUS`, `FT`), `eventState.tennisState.servingTeamId`, `tournamentName`, `round`, `updatedAt`. | `GET /v1/events/slug/{slug}` |
| Is the live score available before the match? | Partly. `eventState` is `null` before play, so the tournament name is unknown prematch. Event **tags** often carry a tournament label (e.g. "Vienna", "Challenger Abidjan"), used only for prioritisation. | live payloads |
| Can the order book be read without credentials? | **Yes.** `GET /v1/markets/{slug}/book` returns full depth with a nanosecond `transactTime`. | live probe |
| Does the market-data WebSocket need credentials? | **Yes.** `wss://api.polymarket.us/v1/ws/markets` requires API-key signing in the handshake. No read-only key scope is documented. | websocket docs |

## Consequences for the design

* **Record and paper modes poll public REST** (`/book` about every 1 s, `/events/slug` about every 2 s per
  match, within the documented 25 req/s per-IP limit). They never hold credentials. The
  WebSocket path is left disabled (`feeds.use_market_websocket: false`), because enabling it would
  put a trading-capable key in a non-trading process.
* The implemented score feed (`polymarket_us_event_state`) comes from the venue's own payload.
  Its latency relative to market repricing is **unknown**. The Gate 1 latency study exists to
  measure it. A licensed low-latency provider is an interface only (`UnavailableScoreFeed`).
* Market identity uses typed fields only: `sportsMarketType`, `marketSides[].long`,
  `marketSides[].teamId`, and event `teams[].id`. Player A is the `long: true` participant
  (the YES side). Slugs are never parsed.
* Unknown `sportsMarketType` values are **rejected**. The docs suggest treating unknown values as
  props, but v1 deliberately refuses them. `table_tennis_*` is always rejected.

## Fees (schedule effective 2026-10-19)

* Taker: `fee = 0.0695 x C x p x (1 - p)` (Table Tennis 0.10, excluded). Each market also
  carries a `feeCoefficient` field (0.0695 on every tennis market observed). The engine uses it
  when present and journals any difference from the configured default.
* Rounding: banker's rounding to $0.01 per fill. For a multi-fill aggressive order, the running
  total charged never exceeds the rounding of the cumulative exact fee. The adjustment only
  reduces a fill's charge.
* Maker rebates: tier 1 (below $10M monthly maker volume) gets none. v1 never counts rebates as
  profit.
* Combo fees (with a coefficient change on 2026-10-28) do not apply: combos are out of scope.

## Order semantics

* Only YES trades. `price.value` is always the YES price.
  * `BUY_LONG`: buy YES, consumes asks.
  * `SELL_LONG`: sell YES, consumes bids.
  * `BUY_SHORT`: buy NO, consumes bids, costs `1 - p`.
  * `SELL_SHORT`: sell NO, consumes asks.
* Price limits are [0.01, 0.99]. Tick size (`orderPriceMinTickSize`) and minimum quantity
  (`minimumTradeQty`) are per market; tennis markets observed used 0.01 / 0.01.
* `POST /v1/orders` has **no client-order-id field**. The live adapter persists every intent
  before submission and never blindly retries a create call that timed out.
* During latency spikes, the exchange rejects orders not processed within 5 s with the message
  "Global Rate Limit Exceeded". This is a transient reject, not a fill.
* Batched cancel responses echo request ids and do not confirm per-order success. Cancels are
  confirmed by reading the order back.

## Tennis settlement rules (Sports FAQs and market rules text)

* A match begins at the first serve. Before that, walkover, withdrawal or cancellation settles
  at the **last fair market price** (LFMP) as of the announcement. The FAQ wording about $0.50
  for non-ITF matches is ambiguous; the per-market rules text observed says LFMP. The engine
  never assumes either value and waits for the exchange settlement record.
* **Mid-match retirement, default or disqualification: the official winner settles at $1.00**,
  regardless of the score. A player leading when the opponent retires still loses the contract.
  The model does not include retirement hazard; the uncertainty floor and position limits must
  absorb it.
* Delays or rescheduling outside two days of the original time settle at LFMP.

## Eligibility and compliance (unresolved; operator action required)

* Trading requires a Polymarket US account with completed KYC/identity verification and API keys
  from polymarket.us/developer. Reaching the public API proves nothing about eligibility.
* The international geoblock endpoint is not used and is not evidence of US eligibility.
* No VPN, proxy or foreign server is used or supported.
* Live mode refuses to start until the operator confirms eligibility, jurisdiction and API
  permissions in `config/live_authorization.yaml` (see the [operator runbook](operator-runbook.md)).
