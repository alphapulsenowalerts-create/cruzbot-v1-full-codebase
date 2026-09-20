"""Pydantic schemas for market data, decisions, and orders."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class Bar(BaseModel):
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: Optional[float] = None

    @field_validator("symbol")
    @classmethod
    def upper_symbol(cls, v: str) -> str:
        return v.strip().upper()


class Quote(BaseModel):
    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @field_validator("symbol")
    @classmethod
    def upper_symbol(cls, v: str) -> str:
        return v.strip().upper()


class IndicatorSnapshot(BaseModel):
    symbol: str
    timestamp: datetime = Field(default_factory=utcnow)
    close: float
    volume: float
    vwap: Optional[float] = None
    rsi: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    atr: Optional[float] = None
    ema_cross: Optional[str] = None  # "bullish" | "bearish" | "none"
    extras: Dict[str, Any] = Field(default_factory=dict)


class Decision(BaseModel):
    """Structured agent decision — rigid schema, validated on emit."""

    action: Action
    symbol: str
    confidence: float = Field(ge=0, le=100)
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reasoning: str = ""
    quantity: Optional[float] = None
    limit_price: Optional[float] = None
    timestamp: datetime = Field(default_factory=utcnow)

    @field_validator("symbol")
    @classmethod
    def upper_symbol(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().upper()
        return v

    @classmethod
    def hold(cls, symbol: str, reasoning: str = "HOLD") -> "Decision":
        return cls(
            action=Action.HOLD,
            symbol=symbol,
            confidence=0.0,
            stop_loss=None,
            take_profit=None,
            reasoning=reasoning,
        )


class OrderRequest(BaseModel):
    symbol: str
    side: OrderSide
    qty: float = Field(gt=0)
    order_type: OrderType = OrderType.LIMIT
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    client_order_id: str = Field(default_factory=lambda: f"adt-{uuid4().hex[:16]}")
    time_in_force: str = "day"
    paper: bool = True
    post_only: bool = False  # maker-only limit (Coinbase post_only)

    @field_validator("symbol")
    @classmethod
    def upper_symbol(cls, v: str) -> str:
        return v.strip().upper()

    @model_validator(mode="after")
    def require_limit_price(self) -> "OrderRequest":
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit_price required for LIMIT orders")
        if self.post_only and self.order_type != OrderType.LIMIT:
            raise ValueError("post_only requires LIMIT order type")
        return self


class OrderResult(BaseModel):
    client_order_id: str
    broker_order_id: Optional[str] = None
    status: OrderStatus
    symbol: str
    side: OrderSide
    qty: float
    filled_qty: float = 0.0
    avg_fill_price: Optional[float] = None
    message: str = ""
    paper: bool = True
    timestamp: datetime = Field(default_factory=utcnow)


class Position(BaseModel):
    symbol: str
    qty: float
    avg_entry_price: float
    market_value: float = 0.0
    unrealized_pl: float = 0.0
    side: str = "long"
    # Bracket / hold state (paper book + hard exits)
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None  # TP2 final target in sweet-spot mode
    take_profit_1: Optional[float] = None  # TP1 (1R) — sell 50%
    trail_distance: Optional[float] = None  # unused in volume_sweet_spot
    trail_high_water: Optional[float] = None
    opened_at: Optional[datetime] = None
    tp1_done: bool = False
    initial_qty: Optional[float] = None
    entry_reason: Optional[str] = None


class AccountState(BaseModel):
    equity: float
    cash: float
    buying_power: float
    day_pl: float = 0.0
    day_pl_pct: float = 0.0
    paper: bool = True
    timestamp: datetime = Field(default_factory=utcnow)


class RiskVerdict(BaseModel):
    approved: bool
    reason: str = ""
    sized_qty: float = 0.0
    risk_amount: float = 0.0
    risk_pct: float = 0.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    trailing_stop_distance: Optional[float] = None
    circuit_breaker_active: bool = False



class HigherTimeframeContext(BaseModel):
    """1h (or higher) trend context for scalps — EMA200 slope + simple S/R."""

    timeframe: str = "1Hour"
    ema_200: Optional[float] = None
    ema_200_slope: Optional[float] = None  # >0 uptrend, <0 downtrend
    ema_200_1h: Optional[float] = None  # alias / explicit 1h EMA200 for MTF align
    ema_200_4h: Optional[float] = None  # 4h EMA200 for MTF align
    ema_200_15m: Optional[float] = None  # 15m EMA200 entry-direction block
    swing_high: Optional[float] = None
    swing_low: Optional[float] = None
    support: Optional[float] = None
    resistance: Optional[float] = None
    extras: Dict[str, Any] = Field(default_factory=dict)


class TradeMemoryEntry(BaseModel):
    """One recent trade for rolling agent/LLM memory."""

    symbol: str
    action: str
    outcome: str  # win | loss | stop_loss | flat | unknown
    reason: str = ""
    pnl: Optional[float] = None
    timestamp: datetime = Field(default_factory=utcnow)

    @field_validator("symbol")
    @classmethod
    def upper_symbol(cls, v: str) -> str:
        return v.strip().upper()


class AgentObservation(BaseModel):
    symbol: str
    indicators: IndicatorSnapshot
    quote: Optional[Quote] = None
    position: Optional[Position] = None
    account: Optional[AccountState] = None
    recent_bars: List[Bar] = Field(default_factory=list)
    # Multi-timeframe: 1m/5m entry indicators live in `indicators`; HTF is separate
    entry_timeframe: str = "1Min"
    htf: Optional[HigherTimeframeContext] = None
    trade_memory: List[TradeMemoryEntry] = Field(default_factory=list)
    macro_paused: bool = False
    revenge_locked: bool = False
