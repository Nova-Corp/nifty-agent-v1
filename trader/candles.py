"""Build time-aligned OHLC candles (aligned to 09:15) from ticks."""
from __future__ import annotations
from datetime import datetime, time, timedelta
from typing import Optional
from .models import Candle


class CandleBuilder:
    def __init__(self, tf_min: int, session_start=time(9, 15), session_end=time(15, 30)):
        self.tf = timedelta(minutes=tf_min)
        self.ss, self.se = session_start, session_end
        self.cur: Optional[Candle] = None
        self.last_closed_start: Optional[datetime] = None

    def bucket_start(self, ts: datetime) -> datetime:
        base = ts.replace(hour=self.ss.hour, minute=self.ss.minute, second=0, microsecond=0)
        return base + ((ts - base) // self.tf) * self.tf

    def on_price(self, ts: datetime, price: float, volume: int = 0) -> list[Candle]:
        if not (self.ss <= ts.time() < self.se):
            return []
        b = self.bucket_start(ts)
        if self.last_closed_start is not None and b <= self.last_closed_start:
            return []                       # late tick for an already-closed candle
        out = []
        if self.cur is not None and b > self.cur.start:
            out.append(self._close())
        if self.cur is None:
            self.cur = Candle(b, price, price, price, price, volume or 0)
        else:
            self.cur.high = max(self.cur.high, price)
            self.cur.low = min(self.cur.low, price)
            self.cur.close = price
            self.cur.volume += volume or 0
        return out

    def flush(self, now: datetime, grace_sec: float = 2.0) -> list[Candle]:
        """Close the running candle once its time is up, even if no new tick arrived."""
        if self.cur is not None and now >= self.cur.start + self.tf + timedelta(seconds=grace_sec):
            return [self._close()]
        return []

    def _close(self) -> Candle:
        c, self.cur = self.cur, None
        self.last_closed_start = c.start
        return c


def resample(candles: list[Candle], tf_min: int, now: Optional[datetime] = None) -> list[Candle]:
    """Resample 1-minute candles to tf_min candles; drops a still-forming last candle."""
    b = CandleBuilder(tf_min)
    out: list[Candle] = []
    groups: dict = {}
    for c in candles:
        if not (b.ss <= c.start.time() < b.se):
            continue
        k = b.bucket_start(c.start)
        g = groups.get(k)
        if g is None:
            groups[k] = Candle(k, c.open, c.high, c.low, c.close, c.volume)
        else:
            g.high, g.low, g.close = max(g.high, c.high), min(g.low, c.low), c.close
            g.volume += c.volume
    for k in sorted(groups):
        if now is not None and k + b.tf > now:
            continue
        out.append(groups[k])
    return out
