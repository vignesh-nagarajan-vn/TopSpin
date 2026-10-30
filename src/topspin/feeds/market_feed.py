"""Local order-book state with explicit synchronization status.

Polymarket US delivers *complete* top-of-book snapshots (REST ``/book`` and the market
WebSocket ``MARKET_DATA`` stream alike), not incremental deltas, so a "rebuild" after a gap
is simply the next valid snapshot. The tracker still guards against out-of-order,
duplicate, crossed, stale and non-open books, and never reports a book as tradeable unless
it is synchronized and fresh.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from topspin.models.market import BookSnapshot, InvalidBookError, MarketState, OrderBookView
from topspin.timeutil import NS_PER_S


class BookSync(StrEnum):
    UNSYNCED = "unsynced"  # no valid snapshot yet, or invalidated by a feed gap
    SYNCED = "synced"
    INVALID = "invalid"  # last snapshot failed validation (e.g. crossed)
    NOT_OPEN = "not_open"  # market suspended / halted / expired / pre-open


@dataclass(frozen=True, slots=True)
class ApplyResult:
    applied: bool
    reason: str


class MarketBookTracker:
    def __init__(
        self, market_slug: str, max_staleness_s: float, unknown_cache_age_s: float = 0.0
    ) -> None:
        self.market_slug = market_slug
        self.max_staleness_ns = int(max_staleness_s * NS_PER_S)
        self.unknown_cache_age_s = unknown_cache_age_s
        self.as_of_ns: int | None = None
        self.sync = BookSync.UNSYNCED
        self.view: OrderBookView | None = None
        self.snapshot: BookSnapshot | None = None
        self.market_state = MarketState.UNKNOWN
        self.last_receipt_ns: int | None = None
        self.last_transact_ns: int | None = None
        self.duplicates = 0
        self.out_of_order = 0
        self.invalid = 0

    def apply(self, snap: BookSnapshot) -> ApplyResult:
        if snap.market_slug != self.market_slug:
            return ApplyResult(False, "wrong_market")
        as_of = snap.as_of_ns(self.unknown_cache_age_s)
        if (
            self.last_transact_ns is not None
            and snap.transact_time_ns is not None
            and snap.transact_time_ns < self.last_transact_ns
        ):
            self.out_of_order += 1
            return ApplyResult(False, "out_of_order")
        if (
            self.snapshot is not None
            and snap.transact_time_ns == self.last_transact_ns
            and snap.bids == self.snapshot.bids
            and snap.asks == self.snapshot.asks
            and snap.state == self.snapshot.state
        ):
            # Same book re-delivered: confirms it was still current as of this response.
            self.duplicates += 1
            self.last_receipt_ns = snap.local_ts_ns
            self.as_of_ns = max(as_of, self.as_of_ns or as_of)
            self.sync = BookSync.SYNCED if snap.state == MarketState.OPEN else BookSync.NOT_OPEN
            return ApplyResult(True, "duplicate_refresh")
        if self.as_of_ns is not None and as_of < self.as_of_ns:
            # Different cache nodes can answer out of order: never step back to older data.
            self.out_of_order += 1
            return ApplyResult(False, "out_of_order")
        try:
            view = snap.view()
            view.validate()
        except (InvalidBookError, ArithmeticError, ValueError) as exc:
            self.invalid += 1
            self.sync = BookSync.INVALID
            self.view = None
            self.snapshot = None  # a re-delivered valid book must be applied in full
            return ApplyResult(False, f"invalid:{exc}")
        self.view = view
        self.snapshot = snap
        self.market_state = snap.state
        self.last_receipt_ns = snap.local_ts_ns
        self.as_of_ns = as_of
        if snap.transact_time_ns is not None:
            self.last_transact_ns = snap.transact_time_ns
        self.sync = BookSync.SYNCED if snap.state == MarketState.OPEN else BookSync.NOT_OPEN
        return ApplyResult(True, "applied")

    def mark_gap(self, reason: str = "feed_gap") -> None:
        """Called on disconnect / failed poll: distrust the book until a fresh snapshot."""
        self.sync = BookSync.UNSYNCED

    def age_ns(self, now_ns: int) -> int | None:
        """Age of the data itself, including any time it spent in a cache."""
        return None if self.as_of_ns is None else now_ns - self.as_of_ns

    def tradeable(self, now_ns: int) -> tuple[bool, str]:
        if self.sync != BookSync.SYNCED or self.view is None:
            return False, f"book_{self.sync.value}"
        age = self.age_ns(now_ns)
        if age is None or age > self.max_staleness_ns:
            return False, "book_stale"
        if not self.view.two_sided:
            return False, "book_one_sided"
        return True, "ok"


class MarketDataSource(Protocol):
    """Anything that yields full book snapshots (REST poller, WebSocket, replay)."""

    async def next_snapshot(self) -> BookSnapshot: ...
