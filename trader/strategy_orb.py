"""Opening Range Breakout with trend filter.

Rules (all configurable):
  * Opening range = high/low of the index between orb_start and orb_end (default 09:15-09:30).
  * Skip the day if the range is too narrow (noise) or too wide (move already happened).
  * Bullish: a candle CLOSES above range-high + buffer and above the EMA  -> buy CE.
  * Bearish: a candle CLOSES below range-low - buffer and below the EMA   -> buy PE.
  * At most one signal per direction per day; no new signals after entry_end.
Exits (stop-loss, target, trailing, time) are handled by the engine/risk config.
"""
from __future__ import annotations
import logging
from datetime import timedelta
from typing import Optional
from .clock import hhmm
from .models import Candle, Signal

log = logging.getLogger("strategy")


class EMA:
    def __init__(self, n: int):
        self.n, self.k = n, 2 / (n + 1)
        self.value: Optional[float] = None
        self.count = 0

    def update(self, x: float) -> float:
        self.value = x if self.value is None else self.value + self.k * (x - self.value)
        self.count += 1
        return self.value

    @property
    def ready(self) -> bool:
        return self.count >= self.n


class ORBStrategy:
    name = "orb"

    def __init__(self, s: dict):
        self.tf = timedelta(minutes=int(s["timeframe_min"]))
        self.orb_start, self.orb_end = hhmm(s["orb_start"]), hhmm(s["orb_end"])
        self.entry_end = hhmm(s["entry_end"])
        self.buffer = float(s.get("breakout_buffer_pts", 5))
        self.min_rng, self.max_rng = float(s["min_range_pts"]), float(s["max_range_pts"])
        self.use_ema = bool(s.get("ema_filter", True))
        self.ema = EMA(int(s.get("ema_period", 20)))
        mins = (self.orb_end.hour * 60 + self.orb_end.minute) - (self.orb_start.hour * 60 + self.orb_start.minute)
        self.orb_candles_needed = max(1, mins // int(s["timeframe_min"]))
        self.day = None
        self.last_start = None
        self.reset_day(None)

    def reset_day(self, d):
        self.day = d
        self.orh = self.orl = None
        self.orb_count = 0
        self.taken: set[str] = set()
        self._range_logged = False

    def mark_taken(self, direction: str):
        self.taken.add(direction)

    def on_candle(self, c: Candle, allow_signal: bool = True) -> Optional[Signal]:
        if self.last_start is not None and c.start <= self.last_start:
            return None
        self.last_start = c.start
        ema = self.ema.update(c.close)
        if c.start.date() != self.day:
            return None                                  # history: only warms up EMA
        t, end = c.start.time(), (c.start + self.tf).time()
        if self.orb_start <= t and end <= self.orb_end:
            self.orh = c.high if self.orh is None else max(self.orh, c.high)
            self.orl = c.low if self.orl is None else min(self.orl, c.low)
            self.orb_count += 1
            if self.orb_count == self.orb_candles_needed:
                log.info("Opening range set: high=%.2f low=%.2f width=%.2f", self.orh, self.orl, self.orh - self.orl)
            return None
        if not allow_signal or self.orb_count < self.orb_candles_needed:
            return None
        if t < self.orb_end or end > self.entry_end:
            return None
        rng = self.orh - self.orl
        if not (self.min_rng <= rng <= self.max_rng):
            if not self._range_logged:
                log.info("Opening range %.1f pts outside [%s, %s]; no trades today", rng, self.min_rng, self.max_rng)
                self._range_logged = True
            return None
        up_ok = (not self.use_ema) or (self.ema.ready and c.close > ema)
        dn_ok = (not self.use_ema) or (self.ema.ready and c.close < ema)
        sig_ts = c.start + self.tf
        if "CE" not in self.taken and c.close > self.orh + self.buffer and up_ok:
            self.taken.add("CE")
            return Signal("CE", f"close {c.close:.2f} > ORH {self.orh:.2f}+{self.buffer:g}, EMA {ema:.2f}", c.close, sig_ts)
        if "PE" not in self.taken and c.close < self.orl - self.buffer and dn_ok:
            self.taken.add("PE")
            return Signal("PE", f"close {c.close:.2f} < ORL {self.orl:.2f}-{self.buffer:g}, EMA {ema:.2f}", c.close, sig_ts)
        return None
