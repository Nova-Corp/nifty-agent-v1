"""Run the agent for one trading day.

    python main.py                 # paper trading (default)
    python main.py --live          # live: also needs mode: live + live_confirm phrase in config.yaml
    python main.py --flatten-on-exit   # on Ctrl+C, exit any open position before stopping

Start it before 09:15 (e.g. 09:05 via cron / Task Scheduler); it stops itself after 15:30.
"""
from __future__ import annotations
import argparse
import logging
import signal
import sys
import time as _time
from datetime import datetime, time, timedelta
from pathlib import Path

import requests

from trader.alerts import Alerter
from trader.candles import resample
from trader.clock import IST, MarketCalendar, now_ist
from trader.config import load_config
from trader.engine import Engine
from trader.models import Candle, TradeState
from trader.recorder import TickRecorder
from trader.risk import RiskManager
from trader.storage import Store
from trader.strategy_orb import ORBStrategy

LIVE_PHRASE = "I ACCEPT REAL MONEY RISK"
log = logging.getLogger("main")


def setup_logging(log_dir: str, day):
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)-10s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=[
        logging.FileHandler(Path(log_dir) / f"{day.isoformat()}.log"), logging.StreamHandler(sys.stdout)])


def public_ip() -> str:
    return requests.get("https://api.ipify.org", timeout=8).text.strip()


def connect_alice(cfg):
    from pya3 import Aliceblue
    b = cfg["broker"]
    if not b.get("user_id") or not b.get("api_key"):
        sys.exit("Set ALICE_USER_ID and ALICE_API_KEY in .env")
    alice = Aliceblue(user_id=b["user_id"], api_key=b["api_key"])
    sess = alice.get_session_id()
    log.info("session response: %s", {k: v for k, v in (sess or {}).items() if k.lower() != "sessionid"}
             if isinstance(sess, dict) else type(sess))
    if isinstance(sess, dict) and str(sess.get("stat", "")).lower() == "not_ok":
        sys.exit(f"Alice Blue login failed: {sess}. Log in once via web/app today, then retry.")
    return alice


def fetch_history(alice, cfg, now: datetime) -> list[Candle]:
    """Last few sessions of 1-min index data, resampled; warms up EMA and rebuilds the opening range."""
    u = cfg["underlying"]
    try:
        inst = alice.get_instrument_by_token(u["index_exchange"], int(u["index_token"]))
        df = alice.get_historical(inst, now - timedelta(days=6), now, "1", indices=True)
        if isinstance(df, dict):
            raise RuntimeError(df)
        one = []
        for _, r in df.iterrows():
            ts = r["datetime"]
            ts = datetime.fromisoformat(str(ts)) if not isinstance(ts, datetime) else ts
            ts = ts.replace(tzinfo=IST) if ts.tzinfo is None else ts.astimezone(IST)
            one.append(Candle(ts, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
        out = resample(one, int(cfg["strategy"]["timeframe_min"]), now)
        log.info("history: %d x %d-min candles loaded", len(out), cfg["strategy"]["timeframe_min"])
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("history backfill failed (%s); EMA will warm up from live data and an incomplete "
                    "opening range will be ignored today", e)
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--flatten-on-exit", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    today = now_ist().date()
    st = cfg.get("storage", {})
    setup_logging(st.get("log_dir", "data/logs"), today)

    live = cfg["mode"] == "live"
    if live or args.live:
        if not (live and args.live and cfg.get("live_confirm") == LIVE_PHRASE):
            sys.exit(f"LIVE refused: need mode: live, --live flag, and live_confirm: '{LIVE_PHRASE}'")
        expected = cfg["broker"].get("expected_static_ip")
        ip = public_ip()
        if not expected or ip != expected:
            sys.exit(f"LIVE refused: public IP {ip} != expected_static_ip {expected!r} (SEBI static-IP rule)")
        cfg["risk"]["max_lots"] = min(int(cfg["risk"]["max_lots"]), int(cfg.get("live", {}).get("max_lots", 1)))
        live = True

    cal = MarketCalendar(cfg.get("calendar", {}).get("holidays"))
    if not cal.is_trading_day(today):
        log.info("%s is not a trading day; exiting", today)
        return

    alerts = Alerter(cfg["alerts"].get("telegram_bot_token"), cfg["alerts"].get("telegram_chat_id"),
                     prefix="[LIVE]" if live else "[PAPER]")
    alice = connect_alice(cfg)

    from trader.feed import AliceFeed
    from trader.instruments import AliceInstruments
    from trader.brokers.paper import PaperBroker
    u = cfg["underlying"]
    alice.get_contract_master(u["index_exchange"])
    instruments = AliceInstruments(alice, u["name"], u["option_exchange"], lot_size_fallback=int(u["lot_size_fallback"]))
    feed = AliceFeed(alice, u["index_exchange"], u["index_token"], float(cfg["risk"]["data_stale_sec"]))
    if live:
        from trader.brokers.aliceblue import AliceBlueBroker
        broker = AliceBlueBroker(alice)
    else:
        broker = PaperBroker(feed, int(cfg["paper"]["slippage_ticks"]), float(cfg["execution"]["tick_size"]))

    store = Store(st.get("db_path", "data/trader.db"))
    recorder = TickRecorder(st.get("tick_dir", "data/ticks"), today) if st.get("record_ticks", True) else None
    engine = Engine(cfg, broker, feed, instruments, ORBStrategy(cfg["strategy"]),
                    RiskManager(cfg["risk"], cfg["strategy"]), store, alerts, recorder)

    now = now_ist()
    engine.start_day(today, now, fetch_history(alice, cfg, now))
    feed.start()
    alerts.send(f"started in {'LIVE' if live else 'PAPER'} mode | capital {cfg['risk']['capital']} | "
                f"max lots {cfg['risk']['max_lots']} | daily loss limit {cfg['risk']['max_daily_loss']}")

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))

    end = datetime.combine(today, time(15, 31), IST)
    errors = 0
    while not stop["flag"] and now_ist() < end:
        now = now_ist()
        try:
            feed.watchdog(now, cal.is_market_open(now))
            engine.step(now)
            errors = 0
        except Exception as e:  # noqa: BLE001
            errors += 1
            log.exception("step error")
            if errors in (1, 10):
                alerts.send(f"engine error: {e!r}", "CRITICAL")
            if errors >= 20:
                engine._freeze(now, "repeated engine errors")
        _time.sleep(0.5)

    if stop["flag"] and args.flatten_on_exit and engine.trade:
        log.info("flattening before shutdown")
        engine.flatten(now_ist())
        deadline = _time.time() + 30
        while engine.trade and _time.time() < deadline:
            engine.step(now_ist())
            _time.sleep(0.5)
    if engine.trade and engine.trade.state in (TradeState.OPEN, TradeState.EXITING):
        alerts.send(f"stopped with open trade {engine.trade.trade_id}; the stop-loss order remains at the broker. "
                    "Restarting the bot will resume managing it.", "WARN")
    feed.stop()
    if recorder:
        recorder.close()
    summary = engine.summary(today)
    log.info(summary)
    alerts.send(summary)


if __name__ == "__main__":
    main()
