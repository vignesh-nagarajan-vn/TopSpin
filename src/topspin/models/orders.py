"""Order and fill models mirroring Polymarket US order intents.

Only the YES instrument trades. ``price`` is always the YES price. Intents:

* BUY_LONG   - buy YES; consumes asks; costs ``p`` per contract.
* SELL_LONG  - sell YES held; consumes bids; receives ``p``.
* BUY_SHORT  - buy NO (synthetic short YES); consumes bids; costs ``1 - p``.
* SELL_SHORT - sell NO held; consumes asks; receives ``1 - p``.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from topspin.models.market import ONE


class OrderIntent(StrEnum):
    BUY_LONG = "ORDER_INTENT_BUY_LONG"
    SELL_LONG = "ORDER_INTENT_SELL_LONG"
    BUY_SHORT = "ORDER_INTENT_BUY_SHORT"
    SELL_SHORT = "ORDER_INTENT_SELL_SHORT"

    @property
    def consumes(self) -> str:
        """Which side of the YES book a marketable order of this intent consumes."""
        return "ask" if self in (OrderIntent.BUY_LONG, OrderIntent.SELL_SHORT) else "bid"

    @property
    def holding(self) -> str:
        """The position side this intent opens or closes: 'YES' or 'NO'."""
        return "YES" if self in (OrderIntent.BUY_LONG, OrderIntent.SELL_LONG) else "NO"

    @property
    def opens(self) -> bool:
        return self in (OrderIntent.BUY_LONG, OrderIntent.BUY_SHORT)

    def side_price(self, yes_price: Decimal) -> Decimal:
        """Per-contract price in the held side's terms (YES price or NO price)."""
        return yes_price if self.holding == "YES" else ONE - yes_price

    def closing_intent(self) -> OrderIntent:
        return {
            OrderIntent.BUY_LONG: OrderIntent.SELL_LONG,
            OrderIntent.BUY_SHORT: OrderIntent.SELL_SHORT,
        }[self]


class OrderType(StrEnum):
    LIMIT = "ORDER_TYPE_LIMIT"
    MARKET = "ORDER_TYPE_MARKET"


class TimeInForce(StrEnum):
    GTC = "TIME_IN_FORCE_GOOD_TILL_CANCEL"
    GTD = "TIME_IN_FORCE_GOOD_TILL_DATE"
    IOC = "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    FOK = "TIME_IN_FORCE_FILL_OR_KILL"


class OrderState(StrEnum):
    PENDING_NEW = "pending_new"  # submitted, not yet at the (simulated) exchange
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def terminal(self) -> bool:
        return self in (
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        )


class Liquidity(StrEnum):
    TAKER = "taker"
    MAKER = "maker"


class Order(BaseModel):
    client_order_id: str
    market_slug: str
    match_key: str
    intent: OrderIntent
    order_type: OrderType = OrderType.LIMIT
    tif: TimeInForce = TimeInForce.IOC
    limit_price: Decimal  # YES price
    qty: Decimal
    filled_qty: Decimal = Decimal("0")
    state: OrderState = OrderState.PENDING_NEW
    created_local_ts_ns: int
    arrival_local_ts_ns: int | None = None
    expire_local_ts_ns: int | None = None
    exchange_order_id: str | None = None
    decision_id: str | None = None
    reason: str | None = None
    post_only: bool = False
    # Paper maker queue tracking: displayed qty ahead of us at our price when we joined.
    queue_ahead: Decimal | None = None

    @property
    def remaining(self) -> Decimal:
        return self.qty - self.filled_qty


class Fill(BaseModel):
    fill_id: str
    client_order_id: str
    market_slug: str
    match_key: str
    intent: OrderIntent
    price: Decimal  # YES price
    qty: Decimal
    fee: Decimal  # charged (rounded) fee in USD, >= 0
    exact_fee: Decimal
    liquidity: Liquidity
    local_ts_ns: int
    simulated: bool = True
    meta: dict[str, str] = Field(default_factory=dict)
