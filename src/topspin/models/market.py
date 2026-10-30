"""Exchange reference data and order-book views (Polymarket US YES-price convention)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from topspin.timeutil import NS_PER_S

ZERO = Decimal("0")
ONE = Decimal("1")
EXCHANGE_MIN_PRICE = Decimal("0.01")
EXCHANGE_MAX_PRICE = Decimal("0.99")


class MarketState(StrEnum):
    OPEN = "MARKET_STATE_OPEN"
    PREOPEN = "MARKET_STATE_PREOPEN"
    SUSPENDED = "MARKET_STATE_SUSPENDED"
    HALTED = "MARKET_STATE_HALTED"
    EXPIRED = "MARKET_STATE_EXPIRED"
    TERMINATED = "MARKET_STATE_TERMINATED"
    AUCTION = "MARKET_STATE_MATCH_AND_CLOSE_AUCTION"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def parse(cls, value: str | None) -> MarketState:
        if not value:
            return cls.UNKNOWN
        try:
            return cls(value)
        except ValueError:
            # Retail status strings, e.g. MARKET_STATUS_OPEN.
            alias = {
                "MARKET_STATUS_OPEN": cls.OPEN,
                "MARKET_STATUS_PREOPEN": cls.PREOPEN,
                "MARKET_STATUS_SUSPENDED": cls.SUSPENDED,
                "MARKET_STATUS_HALTED": cls.HALTED,
                "MARKET_STATUS_EXPIRED": cls.EXPIRED,
                "MARKET_STATUS_TERMINATED": cls.TERMINATED,
            }
            return alias.get(value, cls.UNKNOWN)


class MarketKind(StrEnum):
    MATCH_WINNER = "tennis_match_winner"
    SET_1_WINNER = "tennis_set_1_winner"
    SET_2_WINNER = "tennis_set_2_winner"
    SET_3_WINNER = "tennis_set_3_winner"

    @property
    def set_number(self) -> int | None:
        return {
            MarketKind.SET_1_WINNER: 1,
            MarketKind.SET_2_WINNER: 2,
            MarketKind.SET_3_WINNER: 3,
        }.get(self)


class Participant(BaseModel):
    team_id: str
    name: str
    provider_ids: dict[str, str] = Field(default_factory=dict)
    league: str | None = None


class MarketInfo(BaseModel):
    """A validated, supported tennis instrument. YES pays if ``long`` wins."""

    slug: str
    market_id: str
    event_id: str
    event_slug: str
    kind: MarketKind
    tour: str  # "ATP" | "WTA"
    long: Participant
    short: Participant
    scheduled_start_ns: int | None
    state: MarketState
    active: bool
    closed: bool
    tick_size: Decimal
    min_qty: Decimal
    fee_coefficient: Decimal | None
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    volume: Decimal | None = None
    rules_text: str | None = None
    sportradar_event_id: str | None = None
    tournament_name: str | None = None
    tournament_hint: str | None = None
    round_name: str | None = None
    discovered_local_ts_ns: int = 0

    @property
    def match_key(self) -> str:
        return self.event_slug


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    qty: Decimal


@dataclass(frozen=True, slots=True)
class WalkResult:
    """Result of walking displayed depth for a hypothetical marketable order."""

    filled_qty: Decimal
    notional: Decimal  # sum(price * qty) in YES-price terms
    levels: tuple[BookLevel, ...]
    worst_price: Decimal | None

    @property
    def avg_price(self) -> Decimal | None:
        if self.filled_qty <= 0:
            return None
        return self.notional / self.filled_qty


class InvalidBookError(ValueError):
    pass


class OrderBookView:
    """Immutable, validated YES-side book.

    Bid/ask arrays are never assumed sorted: best bid is the highest valid bid price and
    best ask the lowest valid ask price. Levels at the same price are aggregated.
    """

    def __init__(self, bids: list[BookLevel], asks: list[BookLevel]) -> None:
        self.bids = self._aggregate(bids, reverse=True)
        self.asks = self._aggregate(asks, reverse=False)

    @staticmethod
    def _aggregate(levels: list[BookLevel], reverse: bool) -> tuple[BookLevel, ...]:
        agg: dict[Decimal, Decimal] = {}
        for lv in levels:
            if lv.qty <= 0:
                continue
            if not (ZERO < lv.price < ONE):
                raise InvalidBookError(f"price {lv.price} outside (0, 1)")
            agg[lv.price] = agg.get(lv.price, ZERO) + lv.qty
        return tuple(BookLevel(p, q) for p, q in sorted(agg.items(), reverse=reverse))

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def two_sided(self) -> bool:
        return bool(self.bids) and bool(self.asks)

    @property
    def crossed(self) -> bool:
        return self.two_sided and self.best_bid >= self.best_ask  # type: ignore[operator]

    @property
    def spread(self) -> Decimal | None:
        if not self.two_sided:
            return None
        return self.best_ask - self.best_bid  # type: ignore[operator]

    @property
    def mid(self) -> Decimal | None:
        """Reference midpoint. Never an executable price."""
        if not self.two_sided:
            return None
        return (self.best_ask + self.best_bid) / 2  # type: ignore[operator]

    def validate(self) -> None:
        if self.crossed:
            raise InvalidBookError(f"crossed book bid={self.best_bid} ask={self.best_ask}")

    def depth(self, side: str, within: Decimal | None = None) -> Decimal:
        """Cumulative displayed quantity on ``side`` ('bid'|'ask'), optionally limited to
        levels within ``within`` of the touch."""
        levels = self.bids if side == "bid" else self.asks
        if not levels:
            return ZERO
        touch = levels[0].price
        total = ZERO
        for lv in levels:
            if within is not None and abs(lv.price - touch) > within:
                break
            total += lv.qty
        return total

    def walk(self, side: str, qty: Decimal, limit: Decimal | None = None) -> WalkResult:
        """Consume displayed depth on ``side`` ('ask' to buy YES, 'bid' to sell YES).

        ``limit`` is in YES-price terms: the max price when consuming asks, the min price
        when consuming bids.
        """
        levels = self.asks if side == "ask" else self.bids
        remaining = qty
        notional = ZERO
        used: list[BookLevel] = []
        worst: Decimal | None = None
        for lv in levels:
            if remaining <= 0:
                break
            if limit is not None:
                if side == "ask" and lv.price > limit:
                    break
                if side == "bid" and lv.price < limit:
                    break
            take = min(remaining, lv.qty)
            notional += take * lv.price
            used.append(BookLevel(lv.price, take))
            worst = lv.price
            remaining -= take
        return WalkResult(qty - remaining, notional, tuple(used), worst)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bids": [[str(lv.price), str(lv.qty)] for lv in self.bids],
            "asks": [[str(lv.price), str(lv.qty)] for lv in self.asks],
        }


class BookSnapshot(BaseModel):
    """A full (top-N) book snapshot as received. Polymarket US REST and the market-data
    WebSocket both deliver complete snapshots rather than incremental deltas."""

    market_slug: str
    bids: list[tuple[str, str]]
    asks: list[tuple[str, str]]
    state: MarketState
    transact_time_ns: int | None
    local_ts_ns: int
    mono_ns: int = 0
    source: str = "rest"
    last_trade_px: str | None = None
    shares_traded: str | None = None
    # Age of this response in an HTTP cache, when the response reports one.
    cache_age_s: float | None = None
    cache_status: str | None = None

    def as_of_ns(self, unknown_cache_age_s: float = 0.0) -> int:
        """When this data was current. The public REST route is CDN-cached, so receipt time
        overstates freshness; a REST book with no reported age is assumed to be
        ``unknown_cache_age_s`` old. Streamed and replayed sources have no cache."""
        age = self.cache_age_s
        if age is None:
            age = unknown_cache_age_s if self.source == "rest" else 0.0
        return self.local_ts_ns - int(age * NS_PER_S)

    def view(self) -> OrderBookView:
        return OrderBookView(
            [BookLevel(Decimal(p), Decimal(q)) for p, q in self.bids],
            [BookLevel(Decimal(p), Decimal(q)) for p, q in self.asks],
        )


class TradePrint(BaseModel):
    market_slug: str
    price: str
    qty: str
    trade_time_ns: int | None
    local_ts_ns: int
    taker_side: str | None = None
