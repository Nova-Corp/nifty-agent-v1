"""Market simulator: synthetic Nifty path + Black-Scholes option prices with a bid/ask
spread. Lets you exercise the WHOLE pipeline (candles -> signal -> orders -> exits)
on a weekend, and is what the automated tests use."""
from __future__ import annotations
import math
import random
from datetime import datetime, date, time, timedelta
from typing import Optional
from .alerts import Alerter
from .brokers.paper import PaperBroker
from .clock import IST
from .engine import Engine
from .feed import MarketFeed
from .instruments import InstrumentProvider
from .models import Tick, OptionContract, Candle
from .risk import RiskManager
from .storage import Store
from .strategy_orb import ORBStrategy
from .util import round_tick


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(S, K, T, vol, typ, r=0.065):
    if T <= 0:
        return max(0.0, S - K) if typ == "CE" else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * vol * vol) * T) / (vol * math.sqrt(T))
    d2 = d1 - vol * math.sqrt(T)
    if typ == "CE":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


class SimFeed(MarketFeed):
    def __init__(self, index_token: str):
        self.index_token = index_token
        self.quotes: dict[str, Tick] = {}
        self.pending: list[Tick] = []
        self.subs: dict[str, OptionContract] = {}
        self.ok = True

    def subscribe(self, contracts):
        for c in contracts:
            self.subs[c.token] = c

    def quote(self, token) -> Optional[Tick]:
        return self.quotes.get(token)

    def drain(self):
        out, self.pending = self.pending, []
        return out

    def healthy(self, now):
        return self.ok

    def push(self, t: Tick):
        self.quotes[t.token] = t
        self.pending.append(t)


class SimInstruments(InstrumentProvider):
    def __init__(self, today: date, dte: int = 3, lot: int = 65):
        self.exp, self.lot = today + timedelta(days=dte), lot

    def expiries(self):
        return [self.exp]

    def option(self, strike, typ, expiry):
        return OptionContract("NFO", f"{typ}{int(strike)}", f"NIFTY-SIM-{int(strike)}{typ}", "NIFTY",
                              float(strike), typ, expiry, self.lot)


def make_path(day: date, anchors: list[tuple[str, float]], seed: int = 0, noise: float = 1.5):
    """Piecewise-linear index path through (HH:MM, level) anchors plus mean-reverting noise."""
    rnd = random.Random(seed)
    pts = [(datetime.combine(day, time(*map(int, a.split(":"))), IST), v) for a, v in anchors]
    cache, n = {}, 0.0

    def f(ts: datetime) -> float:
        nonlocal n
        if ts in cache:
            return cache[ts]
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            if t0 <= ts <= t1:
                base = v0 + (v1 - v0) * (ts - t0).total_seconds() / max(1, (t1 - t0).total_seconds())
                break
        else:
            base = pts[-1][1] if ts > pts[-1][0] else pts[0][1]
        n = 0.97 * n + rnd.gauss(0, noise)
        cache[ts] = round(base + n, 2)
        return cache[ts]
    return f


def default_cfg() -> dict:
    import yaml
    from pathlib import Path
    return yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text())


def build(cfg: dict, today: date, store_path=":memory:"):
    feed = SimFeed(str(cfg["underlying"]["index_token"]))
    broker = PaperBroker(feed, int(cfg["paper"]["slippage_ticks"]), float(cfg["execution"]["tick_size"]))
    eng = Engine(cfg, broker, feed, SimInstruments(today), ORBStrategy(cfg["strategy"]),
                 RiskManager(cfg["risk"], cfg["strategy"]), Store(store_path), Alerter())
    return eng, feed, broker


def flat_history(today: date, level: float, tf: int = 5) -> list[Candle]:
    prev = today - timedelta(days=1)
    t = datetime.combine(prev, time(9, 15), IST)
    out = []
    while t.time() < time(15, 30):
        out.append(Candle(t, level, level + 5, level - 5, level))
        t += timedelta(minutes=tf)
    return out


def run_day(eng: Engine, feed: SimFeed, today: date, path, iv: float = 0.13,
            start=time(9, 15), end=time(15, 30), step_sec: int = 1, history=None):
    now = datetime.combine(today, start, IST)
    stop = datetime.combine(today, end, IST)
    eng.start_day(today, now, history)
    while now < stop:
        S = path(now)
        feed.push(Tick(feed.index_token, S, now))
        for tok, c in feed.subs.items():
            T = max(0.0, (datetime.combine(c.expiry, time(15, 30), IST) - now).total_seconds()) / (365 * 86400)
            p = max(0.05, bs_price(S, c.strike, T, iv, c.option_type))
            half = max(0.05, p * 0.0015)
            feed.push(Tick(tok, round_tick(p), now, round_tick(max(0.05, p - half)), round_tick(p + half)))
        eng.step(now)
        now += timedelta(seconds=step_sec)
    return eng.store.trades_for_day(today)
