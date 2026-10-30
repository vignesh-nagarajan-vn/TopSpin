# Architecture

A Python 3.12 modular monolith. One deterministic engine serves paper trading and replay. The
live adapter is a separate package that nothing in record or paper imports. A test enforces this
in a fresh interpreter.

```
            public REST (no credentials)
   ┌──────────────┬──────────────┬──────────────┐
   │ discovery    │ /events/slug │ /markets/book│   orchestration/runner.py (asyncio pollers)
   └──────┬───────┴──────┬───────┴──────┬───────┘
          ▼              ▼              ▼
     market_info       score          book        ── stamped with local receipt time
          └──────────────┼──────────────┘
                         ▼
              single ordered queue ──► JSONL journal (append-only, replayable)
                         │
                         ▼  (paper; replay reads the journal instead)
                 orchestration/engine.py  TradingEngine
   ┌────────────┬────────────┬────────────┬────────────┬─────────────┐
   │ feeds/     │ strategy/  │ pricing/   │ strategy/  │ risk/       │
   │ market_feed│ events     │ markov +   │ edge, exit │ limits,     │
   │ book sync  │ state mach.│ calibration│ policy     │ sizing,latch│
   └────────────┴────────────┴────────────┴────────────┴─────────────┘
                         │ orders
                         ▼
               simulation/broker.py PaperBroker ──► ledger (cash, positions, P&L)
                         │
                         ▼
               analytics/ (P&L, execution, latency study, gates, reports)

   live/ (execution adapter, reconciliation, safety)  ◄── orchestration/live_runner.py only
```

## Determinism and ordering

* Every input record carries `local_ts_ns` (receipt time). The consumer enforces strictly
  increasing timestamps, and the engine refuses out-of-order input.
* The engine reads time only from records, never from the wall clock. Decision and order ids are
  derived from timestamps and sequence numbers.
* Replay feeds recorded *inputs* (`market_info`, `book`, `trade`, `score`, `feed_gap`,
  `settlement`, `tick`) back through the same engine and regenerates every decision. A SHA-256
  fingerprint of decisions, orders and fills checks determinism.
* Fill simulation may look at the first book *after* an order's arrival (the exchange's view of
  that moment). Decisions never do.

## Module map

| Package | Responsibility |
| - | - |
| `models/` | Typed schemas: market, book, score, orders, positions and ledger |
| `discovery/` | Market discovery and prioritisation, tournament classification, identity matching, overrides |
| `feeds/` | Polymarket US public client and parsers, book sync tracker, score-feed interface |
| `pricing/` | Rulesets, Markov pricer, calibration, uncertainty band, fundamentals interface |
| `strategy/` | Event state machine, signals and reasons, entry edge, exit policy |
| `risk/` | Sizing, limits, circuit breakers, persistent latch |
| `simulation/` | Fee model, fill helpers, paper broker, replay |
| `storage/` | JSONL journal, SQLite index |
| `analytics/` | P&L, markouts, clustered bootstrap, latency study, reports and gates |
| `orchestration/` | Engine, async runner (record/paper), live runner |
| `live/` | Gated execution adapter, reconciliation, live preconditions |

## Storage choice

The JSONL journal is the source of truth. SQLite is a rebuildable index, chosen so a local setup
needs no database server. The SQL is plain and portable to PostgreSQL if volume demands it.
Parquet was not needed at current volumes (about 20 records/s).
