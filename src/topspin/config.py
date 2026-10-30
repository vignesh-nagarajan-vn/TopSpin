"""Validated configuration. Every operating parameter lives here, not in strategy code.

Loading merges YAML files left to right (``config/default.yaml`` then a mode overlay) and
validates the result. Unknown keys and unsafe combinations are rejected at startup.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from topspin.models.market import MarketKind
from topspin.pricing.uncertainty import MODEL_VERSION


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExchangeConfig(_Strict):
    venue: Literal["polymarket_us"] = "polymarket_us"
    public_base_url: str = "https://gateway.polymarket.us"
    api_base_url: str = "https://api.polymarket.us"
    ws_markets_url: str = "wss://api.polymarket.us/v1/ws/markets"
    ws_private_url: str = "wss://api.polymarket.us/v1/ws/private"
    request_timeout_s: float = 10.0
    # Documented retail limit is 25 req/s per IP; stay far below it.
    max_requests_per_second: float = 8.0
    retry_max_attempts: int = 3
    retry_backoff_s: float = 1.0
    leagues: tuple[str, ...] = ("atp", "wta")
    supported_market_types: tuple[MarketKind, ...] = (MarketKind.MATCH_WINNER,)


class DiscoveryConfig(_Strict):
    max_tracked_matches: int = Field(8, ge=1, le=50)
    refresh_interval_s: float = 60.0
    lookahead_hours: float = 12.0
    max_spread_for_tracking: float = 0.10
    tournaments_file: str = "config/tournaments.yaml"
    overrides_file: str = "config/match_overrides.yaml"


class FeedConfig(_Strict):
    score_poll_interval_s: float = Field(2.0, gt=0)
    book_poll_interval_s: float = Field(1.0, gt=0)
    use_market_websocket: bool = False
    # Both limits apply to data age (receipt time minus reported cache age), not receipt time.
    max_score_staleness_s: float = 20.0
    max_book_staleness_s: float = 5.0
    # Assumed age of a public-route response that reports none (e.g. older journals).
    unknown_cache_age_s: float = Field(30.0, ge=0)


class ModelConfig(_Strict):
    model_version: str = MODEL_VERSION
    serve_baseline: dict[str, float] = Field(default_factory=lambda: {"ATP": 0.645, "WTA": 0.565})
    serve_baseline_delta: float = 0.03
    serve_p_min: float = 0.40
    serve_p_max: float = 0.85
    calibration_tolerance: float = 1e-6
    uncertainty_floor: float = 0.01
    max_band_width: float = 0.15
    anchor_max_spread: float = 0.04
    anchor_max_age_s: float = 300.0
    # The provider's "not started" can lag the first serve; the anchor must predate the
    # first in-play observation by at least this margin.
    anchor_min_lead_s: float = 30.0
    anchor_min_depth: float = 50.0
    anchor_min_prob: float = 0.05
    anchor_max_prob: float = 0.95

    @field_validator("serve_baseline")
    @classmethod
    def _baselines(cls, v: dict[str, float]) -> dict[str, float]:
        for k, p in v.items():
            if not 0.45 <= p <= 0.80:
                raise ValueError(f"serve baseline {k}={p} is unrealistic")
        return v


class StrategyConfig(_Strict):
    enabled_entry_events: tuple[Literal["break", "set_complete"], ...] = ("break", "set_complete")
    min_net_edge: float = 0.02
    min_expected_net_pnl: float = 0.50
    max_spread: float = 0.04
    min_executable_depth: float = 25.0
    adverse_selection_allowance: float = 0.01
    slippage_allowance_ticks: int = 1
    entry_window_s: float = 15.0
    reentry_cooldown_s: float = 120.0
    reentry_min_edge_increment: float = 0.01
    exit_min_improvement: float = 0.0
    no_entry_above_prob: float = 0.95
    no_entry_below_prob: float = 0.05
    minutes_per_game: float = 4.0
    min_holding_minutes: float = 1.0
    maker_policy: Literal["never", "exits_only"] = "never"
    resting_exit_lifetime_s: float = 10.0
    allow_swing_ev_entry: bool = False
    set_winner_trading: bool = False
    point_scalping: bool = False
    market_making: bool = False

    @model_validator(mode="after")
    def _v1_scope(self) -> StrategyConfig:
        if self.point_scalping:
            raise ValueError("point-scalping execution is out of scope for v1")
        if self.market_making:
            raise ValueError("continuous market making is out of scope for v1")
        if self.allow_swing_ev_entry:
            raise ValueError(
                "swing-EV entries need empirically fitted market-response parameters; "
                "none exist yet"
            )
        if self.min_net_edge < 0 or self.min_expected_net_pnl < 0:
            raise ValueError("minimum edge thresholds must be non-negative")
        return self


class RiskConfig(_Strict):
    bankroll: Decimal = Decimal("1000")
    max_risk_per_match_frac: float = 0.02
    max_portfolio_risk_frac: float = 0.10
    daily_loss_halt_frac: float = 0.05
    kelly_multiplier: float = 0.25
    min_order_qty: Decimal = Decimal("1")
    max_order_qty: Decimal = Decimal("5000")
    max_unmatched_order_exposure_frac: float = 0.05
    halt_latch_path: str = "state/halt_latch.json"

    @model_validator(mode="after")
    def _safe(self) -> RiskConfig:
        if self.bankroll <= 0:
            raise ValueError("bankroll must be positive")
        if not 0 < self.kelly_multiplier <= 0.5:
            raise ValueError("kelly_multiplier must be in (0, 0.5]; v1 default is 0.25")
        if not 0 < self.max_risk_per_match_frac <= self.max_portfolio_risk_frac <= 0.5:
            raise ValueError("need 0 < per-match risk <= portfolio risk <= 50%")
        if not 0 < self.daily_loss_halt_frac <= 0.25:
            raise ValueError("daily loss halt must be in (0, 25%]")
        if self.min_order_qty <= 0 or self.max_order_qty < self.min_order_qty:
            raise ValueError("bad order quantity bounds")
        return self


class FeeConfig(_Strict):
    schedule_version: str = "polymarket-us-2026-10-19"
    taker_theta: Decimal = Decimal("0.0695")
    # Rebates are never counted as expected profit in v1.
    count_maker_rebates: bool = False
    # A market's own feeCoefficient overrides the default; mismatches are journaled.
    use_market_fee_coefficient: bool = True


class SimulationConfig(_Strict):
    taker_delay_ms: int = Field(500, ge=0)
    cancel_delay_ms: int = Field(300, ge=0)
    fill_book_policy: Literal["prior", "next", "intersect"] = "intersect"
    paper_fill_fraction: float = Field(0.5, gt=0, le=1)
    maker_queue: Literal["back_of_queue"] = "back_of_queue"
    slippage_stress_ticks: int = Field(0, ge=0)
    seed: int = 7


class OperationsConfig(_Strict):
    mode: Literal["record", "paper", "live"] = "paper"
    data_dir: str = "data"
    journal_dir: str = "data/journal"
    db_path: str = "data/topspin.sqlite"
    report_dir: str = "reports"
    log_level: str = "INFO"
    emergency_stop_file: str = "state/EMERGENCY_STOP"


class LiveConfig(_Strict):
    enabled: bool = False
    max_order_value: Decimal = Decimal("0")
    authorization_file: str = "config/live_authorization.yaml"
    cancel_policy: Literal["bot_orders_only"] = "bot_orders_only"
    require_gate_evidence: bool = True
    gate_evidence_file: str = "reports/gate_evidence.json"


class AppConfig(_Strict):
    exchange: ExchangeConfig = ExchangeConfig()  # frozen, so a shared default is safe
    discovery: DiscoveryConfig = DiscoveryConfig()  # frozen, so a shared default is safe
    feeds: FeedConfig = FeedConfig()  # frozen, so a shared default is safe
    model: ModelConfig = ModelConfig()  # frozen, so a shared default is safe
    strategy: StrategyConfig = StrategyConfig()  # frozen, so a shared default is safe
    risk: RiskConfig = RiskConfig()  # frozen, so a shared default is safe
    fees: FeeConfig = FeeConfig()  # frozen, so a shared default is safe
    simulation: SimulationConfig = SimulationConfig()  # frozen, so a shared default is safe
    operations: OperationsConfig = OperationsConfig()  # frozen, so a shared default is safe
    live: LiveConfig = LiveConfig()  # frozen, so a shared default is safe

    @model_validator(mode="after")
    def _mode_safety(self) -> AppConfig:
        if self.operations.mode != "live" and self.live.enabled:
            raise ValueError("live.enabled may only be set in a live-mode configuration")
        if self.live.max_order_value < 0:
            raise ValueError("live.max_order_value must be >= 0")
        if self.strategy.set_winner_trading and MarketKind.MATCH_WINNER not in (
            self.exchange.supported_market_types
        ):
            raise ValueError("set-winner trading requires match-winner support")
        return self


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(*paths: str | Path, overrides: dict[str, Any] | None = None) -> AppConfig:
    data: dict[str, Any] = {}
    for p in paths:
        path = Path(p)
        if not path.exists():
            raise FileNotFoundError(f"config file not found: {path}")
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} must contain a mapping")
        data = _deep_merge(data, loaded)
    if overrides:
        data = _deep_merge(data, overrides)
    return AppConfig.model_validate(data)


def default_config_paths(mode: str, root: Path | None = None) -> list[Path]:
    root = root or Path.cwd()
    paths = [root / "config" / "default.yaml"]
    overlay = root / "config" / f"{mode}.yaml"
    if overlay.exists():
        paths.append(overlay)
    return paths
