"""Risk gatekeeper: every entry must pass these checks. Nothing here talks to the broker."""
from __future__ import annotations
from datetime import datetime
from .clock import hhmm
from .models import DayState, Tick


class RiskManager:
    def __init__(self, r: dict, s: dict):
        self.r = r
        self.entry_start = hhmm(s.get("orb_end", "09:30"))
        self.entry_end = hhmm(s["entry_end"])

    def check_entry(self, now: datetime, day: DayState, has_position: bool, feed_ok: bool):
        r = self.r
        if day.kill_switch:
            return False, f"kill switch active ({day.kill_reason})"
        if not feed_ok:
            return False, "market data stale/disconnected"
        if has_position:
            return False, "a position is already open"
        if day.trades_taken >= int(r["max_trades_per_day"]):
            return False, "max trades for the day reached"
        if day.realized_pnl <= -float(r["max_daily_loss"]):
            return False, "daily loss limit reached"
        if not (self.entry_start <= now.time() <= self.entry_end):
            return False, "outside entry window"
        return True, ""

    def check_quote(self, q: Tick):
        if q is None or not q.bid or not q.ask or q.bid <= 0 or q.ask < q.bid:
            return False, "no valid two-sided quote"
        mid = (q.bid + q.ask) / 2
        spread_pct = (q.ask - q.bid) / mid * 100
        if spread_pct > float(self.r["max_spread_pct"]):
            return False, f"spread {spread_pct:.2f}% too wide"
        if not (float(self.r["min_premium"]) <= q.ask <= float(self.r["max_premium"])):
            return False, f"premium {q.ask} outside [{self.r['min_premium']}, {self.r['max_premium']}]"
        return True, ""

    def size(self, entry: float, sl: float, lot_size: int, max_lots: int):
        """Lots such that a stop-loss hit loses <= risk_per_trade_pct of capital."""
        risk_pts = entry - sl
        if risk_pts <= 0 or lot_size <= 0:
            return 0, "invalid stop-loss"
        capital = float(self.r["capital"])
        budget = capital * float(self.r["risk_per_trade_pct"]) / 100
        per_lot = risk_pts * lot_size
        lots = min(int(budget // per_lot), max_lots)
        cap_limit = capital * float(self.r.get("max_capital_per_trade_pct", 25)) / 100
        while lots > 0 and entry * lot_size * lots > cap_limit:
            lots -= 1
        if lots < 1:
            return 0, f"1 lot risks Rs {per_lot:.0f} (budget Rs {budget:.0f}) or exceeds capital cap"
        return lots, ""
