"""Decision outcomes and reason codes. A SKIP is a successful outcome."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from tennis_alpha.models.orders import OrderIntent


class Action(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    REDUCE = "REDUCE"
    HOLD = "HOLD"
    SKIP = "SKIP"
    CANCEL_ORDERS = "CANCEL_ORDERS"
    HALT = "HALT"


class Reason(StrEnum):
    OK = "ok"
    # data / model
    NO_ANCHOR = "no_anchor"
    CALIBRATION_FAILED = "calibration_failed"
    TOURNAMENT_UNSUPPORTED = "tournament_unsupported"
    SERVER_UNKNOWN = "server_unknown"
    SCORE_UNRELIABLE = "score_unreliable"
    SCORE_STALE = "score_stale"
    MODEL_INVALID = "model_invalid"
    BAND_TOO_WIDE = "model_band_too_wide"
    NEAR_CERTAIN = "near_certain_outcome"
    # market
    MARKET_NOT_OPEN = "market_not_open"
    BOOK_UNTRADEABLE = "book_untradeable"
    SPREAD_TOO_WIDE = "spread_too_wide"
    INSUFFICIENT_DEPTH = "insufficient_depth"
    # economics (ordered as the edge cascade)
    MARKET_ALREADY_REPRICED = "market_already_repriced"
    EDGE_CONSUMED_BY_FEES = "edge_consumed_by_fees"
    EDGE_WITHIN_MODEL_UNCERTAINTY = "edge_within_model_uncertainty"
    EDGE_BELOW_THRESHOLD = "edge_below_threshold"
    EV_BELOW_MIN_PNL = "expected_pnl_below_minimum"
    SIZE_BELOW_MINIMUM = "size_below_minimum"
    # risk / process
    RISK_REJECTED = "risk_rejected"
    COOLDOWN = "reentry_cooldown"
    DUPLICATE_EVENT = "duplicate_event"
    EVENT_WINDOW_EXPIRED = "event_window_expired"
    CONTRADICTORY_EXPOSURE = "contradictory_exposure"
    HALTED = "halted"
    RECORD_MODE = "record_mode"
    # exits
    LIQUIDATION_BEATS_HOLD = "liquidation_beats_continuation"
    PARTIAL_LIQUIDATION_OPTIMAL = "partial_liquidation_optimal"
    CONTINUATION_BEATS_LIQUIDATION = "continuation_beats_liquidation"
    MATCH_INTERRUPTED = "match_interrupted"


class Decision(BaseModel):
    decision_id: str
    local_ts_ns: int
    match_key: str
    market_slug: str | None = None
    action: Action
    reason: Reason
    intent: OrderIntent | None = None
    qty: Decimal | None = None
    limit_price: Decimal | None = None
    trigger: str | None = None  # dedupe key of the triggering event
    numbers: dict[str, float | str | None] = Field(default_factory=dict)
    detail: str | None = None
