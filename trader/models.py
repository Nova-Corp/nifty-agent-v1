"""Plain data objects shared by every module."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"   # brokers convert this to MPP under the 2026 SEBI rules
    SL = "SL"           # stop-loss LIMIT (SL-M is not allowed for NSE index options)


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


TERMINAL = {OrderStatus.COMPLETE, OrderStatus.CANCELLED, OrderStatus.REJECTED}


@dataclass
class Tick:
    token: str
    ltp: float
    ts: datetime
    bid: Optional[float] = None
    ask: Optional[float] = None
    volume: Optional[int] = None


@dataclass
class Candle:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


@dataclass(frozen=True)
class OptionContract:
    exchange: str
    token: str
    symbol: str
    underlying: str
    strike: float
    option_type: str      # "CE" / "PE"
    expiry: date
    lot_size: int

    def to_dict(self):
        d = asdict(self)
        d["expiry"] = self.expiry.isoformat()
        return d

    @staticmethod
    def from_dict(d):
        d = dict(d)
        d["expiry"] = date.fromisoformat(d["expiry"])
        return OptionContract(**d)


@dataclass
class Signal:
    direction: str        # "CE" (bullish) or "PE" (bearish)
    reason: str
    underlying_price: float
    ts: datetime


@dataclass
class Order:
    order_id: str
    token: str
    side: Side
    qty: int
    order_type: OrderType
    price: float
    trigger: Optional[float]
    tag: str
    status: OrderStatus = OrderStatus.OPEN
    filled_qty: int = 0
    avg_price: float = 0.0
    message: str = ""
    triggered: bool = False


class TradeState(str, Enum):
    AWAIT_QUOTE = "AWAIT_QUOTE"
    ENTRY_PENDING = "ENTRY_PENDING"
    OPEN = "OPEN"
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


ACTIVE_STATES = {TradeState.AWAIT_QUOTE, TradeState.ENTRY_PENDING, TradeState.OPEN, TradeState.EXITING}
_DT_FIELDS = ("created", "entry_time", "exit_time", "deadline", "last_sl_modify", "sl_breach_since")


@dataclass
class Trade:
    trade_id: str
    day: date
    direction: str
    signal_reason: str
    underlying_at_signal: float
    created: datetime
    state: TradeState = TradeState.AWAIT_QUOTE
    contract: Optional[OptionContract] = None
    planned_qty: int = 0
    qty: int = 0
    entry_price: float = 0.0
    entry_time: Optional[datetime] = None
    sl: float = 0.0
    sl_limit: float = 0.0
    target: float = 0.0
    risk_pts: float = 0.0
    highest: float = 0.0
    entry_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    cancel_requested: bool = False
    deadline: Optional[datetime] = None
    exit_attempts: int = 0
    last_sl_modify: Optional[datetime] = None
    sl_breach_since: Optional[datetime] = None
    exit_filled_qty: int = 0
    exit_value: float = 0.0
    processed_orders: list = field(default_factory=list)
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    exit_reason: str = ""
    gross_pnl: float = 0.0
    charges: float = 0.0
    net_pnl: float = 0.0
    note: str = ""

    def to_dict(self):
        d = asdict(self)
        d["day"] = self.day.isoformat()
        d["state"] = self.state.value
        d["contract"] = self.contract.to_dict() if self.contract else None
        for k in _DT_FIELDS:
            d[k] = getattr(self, k).isoformat() if getattr(self, k) else None
        return d

    @staticmethod
    def from_dict(d):
        d = dict(d)
        d["day"] = date.fromisoformat(d["day"])
        d["state"] = TradeState(d["state"])
        d["contract"] = OptionContract.from_dict(d["contract"]) if d.get("contract") else None
        for k in _DT_FIELDS:
            d[k] = datetime.fromisoformat(d[k]) if d.get(k) else None
        return Trade(**d)


@dataclass
class DayState:
    day: date
    trades_taken: int = 0
    directions_taken: list = field(default_factory=list)
    realized_pnl: float = 0.0
    kill_switch: bool = False
    kill_reason: str = ""

    def to_dict(self):
        d = asdict(self)
        d["day"] = self.day.isoformat()
        return d

    @staticmethod
    def from_dict(d):
        d = dict(d)
        d["day"] = date.fromisoformat(d["day"])
        return DayState(**d)
