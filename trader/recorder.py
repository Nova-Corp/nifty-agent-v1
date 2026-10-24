"""Append every tick to a daily CSV. After a few weeks this becomes real data for backtests."""
from __future__ import annotations
import csv
from pathlib import Path
from .models import Tick


class TickRecorder:
    def __init__(self, folder: str, day):
        Path(folder).mkdir(parents=True, exist_ok=True)
        self.path = Path(folder) / f"ticks_{day.isoformat()}.csv"
        new = not self.path.exists()
        self.f = open(self.path, "a", newline="")
        self.w = csv.writer(self.f)
        if new:
            self.w.writerow(["ts", "token", "ltp", "bid", "ask", "volume"])
        self.n = 0

    def write(self, t: Tick):
        self.w.writerow([t.ts.isoformat(), t.token, t.ltp, t.bid, t.ask, t.volume])
        self.n += 1
        if self.n % 200 == 0:
            self.f.flush()

    def close(self):
        self.f.flush()
        self.f.close()
