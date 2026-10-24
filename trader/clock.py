"""Time helpers. Everything in the system is timezone-aware IST."""
from datetime import datetime, date, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def now_ist() -> datetime:
    return datetime.now(IST)


def hhmm(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


class MarketCalendar:
    def __init__(self, holidays=None):
        self.holidays = {date.fromisoformat(str(d)) for d in (holidays or [])}

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def is_market_open(self, dt: datetime) -> bool:
        return self.is_trading_day(dt.date()) and MARKET_OPEN <= dt.time() < MARKET_CLOSE
