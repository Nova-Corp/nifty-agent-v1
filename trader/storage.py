"""SQLite persistence so a crash/restart can recover the day's state and open trades."""
from __future__ import annotations
import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from .models import Trade, DayState, TradeState, ACTIVE_STATES


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("CREATE TABLE IF NOT EXISTS trades (trade_id TEXT PRIMARY KEY, day TEXT, state TEXT, data TEXT)")
            self.conn.execute("CREATE TABLE IF NOT EXISTS days (day TEXT PRIMARY KEY, data TEXT)")
            self.conn.execute("CREATE TABLE IF NOT EXISTS events (ts TEXT, level TEXT, msg TEXT)")

    def save_trade(self, t: Trade):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO trades VALUES (?,?,?,?)",
                              (t.trade_id, t.day.isoformat(), t.state.value, json.dumps(t.to_dict())))

    def trades_for_day(self, d: date) -> list[Trade]:
        with self.lock:
            rows = self.conn.execute("SELECT data FROM trades WHERE day=? ORDER BY trade_id", (d.isoformat(),)).fetchall()
        return [Trade.from_dict(json.loads(r[0])) for r in rows]

    def active_trades(self, d: date) -> list[Trade]:
        return [t for t in self.trades_for_day(d) if t.state in ACTIVE_STATES]

    def save_day(self, ds: DayState):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO days VALUES (?,?)", (ds.day.isoformat(), json.dumps(ds.to_dict())))

    def load_day(self, d: date) -> Optional[DayState]:
        with self.lock:
            row = self.conn.execute("SELECT data FROM days WHERE day=?", (d.isoformat(),)).fetchone()
        return DayState.from_dict(json.loads(row[0])) if row else None

    def event(self, ts: datetime, level: str, msg: str):
        with self.lock:
            self.conn.execute("INSERT INTO events VALUES (?,?,?)", (ts.isoformat(), level, msg))
