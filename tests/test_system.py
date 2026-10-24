import copy
from datetime import date, datetime, time, timedelta
import pytest
from trader.brokers.base import BrokerError
from trader.candles import CandleBuilder
from trader.clock import IST
from trader.models import OrderType, Side, Tick, TradeState, DayState
from trader.risk import RiskManager
from trader.sim import build, default_cfg, make_path, run_day, flat_history, SimInstruments

DAY = date(2026, 10, 26)
UP = [("09:15", 25000), ("09:30", 25040), ("09:45", 25020), ("11:00", 25180), ("15:30", 25200)]
FAKE = [("09:15", 25000), ("09:30", 25040), ("09:50", 25070), ("10:30", 24960), ("15:30", 24940)]


def ts(hh, mm, ss=0):
    return datetime.combine(DAY, time(hh, mm, ss), IST)


def run(anchors, cfg=None, **kw):
    cfg = cfg or default_cfg()
    eng, feed, broker = build(cfg, DAY)
    trades = run_day(eng, feed, DAY, make_path(DAY, anchors, seed=7), history=flat_history(DAY, anchors[0][1]), **kw)
    return eng, feed, broker, trades


def test_candles_align_and_flush():
    b = CandleBuilder(5)
    assert b.on_price(ts(9, 15, 1), 100) == []
    assert b.on_price(ts(9, 19, 59), 105) == []
    out = b.on_price(ts(9, 20, 0), 103)
    assert len(out) == 1 and out[0].start == ts(9, 15) and out[0].high == 105 and out[0].close == 105
    assert b.flush(ts(9, 25, 3)) and b.on_price(ts(9, 24, 59), 1) == []   # late tick ignored


def test_risk_sizing():
    r = RiskManager(default_cfg()["risk"], default_cfg()["strategy"])
    lots, _ = r.size(150, 120, 65, 2)        # 30 pts * 65 = 1950 <= 3000 budget
    assert lots == 1
    lots, why = r.size(300, 240, 65, 2)      # 60 pts * 65 = 3900 > budget
    assert lots == 0 and "budget" in why


def test_trend_day_hits_target_and_sl_order_is_at_broker():
    eng, feed, broker, trades = run(UP)
    closed = [t for t in trades if t.state == TradeState.CLOSED]
    assert len(closed) == 1 and closed[0].exit_reason == "TARGET" and closed[0].net_pnl > 0
    sl_orders = [o for o in broker.orders.values() if o.order_type == OrderType.SL]
    assert sl_orders, "a stop-loss order must be placed at the broker after entry"
    assert broker.positions() == {}, "must be flat at end of day"


def test_fake_breakout_stops_out_with_bounded_loss():
    eng, feed, broker, trades = run(FAKE)
    first = trades[0]
    assert first.exit_reason == "STOPLOSS"
    assert first.net_pnl > -default_cfg()["risk"]["capital"] * 0.01 * 1.3   # ~1% risk plus slippage/charges
    assert broker.positions() == {}


def test_time_exit_flattens():
    cfg = default_cfg()
    cfg["risk"]["target_rr"] = 50                 # unreachable target
    cfg["risk"]["trail_after_rr"] = 50
    eng, feed, broker, trades = run(UP, cfg)
    assert trades[0].exit_reason == "TIME_EXIT"
    assert trades[0].exit_time.time() >= time(15, 10)
    assert broker.positions() == {}


def test_kill_switch_blocks_further_trades():
    cfg = default_cfg()
    cfg["risk"]["max_daily_loss"] = 1000
    eng, feed, broker, trades = run(FAKE, cfg)
    assert eng.day.kill_switch
    assert len([t for t in trades if t.state == TradeState.CLOSED]) == 1
    assert broker.positions() == {}


def test_paper_broker_never_goes_short():
    eng, feed, broker = build(default_cfg(), DAY)
    c = SimInstruments(DAY).option(25000, "CE", DAY + timedelta(days=3))
    feed.push(Tick(c.token, 100, ts(10, 0), 99.9, 100.1))
    with pytest.raises(BrokerError):
        broker.place_order(c, Side.SELL, 65, OrderType.LIMIT, price=99)


def test_gap_through_stop_loss_is_exited_actively():
    eng, feed, broker = build(default_cfg(), DAY)
    eng.start_day(DAY, ts(9, 15), flat_history(DAY, 25000))
    # Drive a trade to OPEN by hand
    from trader.models import Signal
    feed.push(Tick(feed.index_token, 25050, ts(10, 0)))
    c = SimInstruments(DAY).option(25050, "CE", DAY + timedelta(days=3))
    feed.subscribe([c])
    feed.push(Tick(c.token, 130, ts(10, 0), 129.9, 130.1))
    eng._on_signal(Signal("CE", "test", 25050, ts(10, 0)), ts(10, 0))
    eng.step(ts(10, 0, 1)); eng.step(ts(10, 0, 2))
    t = eng.trade
    assert t.state == TradeState.OPEN and t.sl_order_id
    # Price gaps far below the SL-limit: SL triggers but cannot fill
    for s in range(3, 15):
        feed.push(Tick(c.token, 80, ts(10, 0, s), 79.9, 80.1))
        eng.step(ts(10, 0, s))
    closed = eng.store.trades_for_day(DAY)[0]
    assert closed.state == TradeState.CLOSED and closed.exit_reason == "SL_GAP_EXIT"
    assert broker.positions() == {}


def test_crash_recovery_restores_protection(tmp_path):
    cfg = default_cfg()
    db = str(tmp_path / "t.db")
    eng, feed, broker = build(cfg, DAY, store_path=db)
    path = make_path(DAY, UP, seed=7)
    # run until a position is open, then "crash"
    run_day(eng, feed, DAY, path, end=time(10, 5), history=flat_history(DAY, 25000))
    assert eng.trade and eng.trade.state == TradeState.OPEN
    # restart with fresh objects on the same database
    eng2, feed2, broker2 = build(cfg, DAY, store_path=db)
    trades = run_day(eng2, feed2, DAY, path, start=time(10, 5), history=flat_history(DAY, 25000))
    closed = [t for t in trades if t.state == TradeState.CLOSED]
    assert len(closed) == 1 and closed[0].exit_reason in ("TARGET", "TRAILING_SL", "TIME_EXIT")
    assert any(o.order_type == OrderType.SL for o in broker2.orders.values()), "SL must be re-placed after restart"
    assert broker2.positions() == {}


def test_stale_feed_blocks_entries():
    eng, feed, broker = build(default_cfg(), DAY)
    feed.ok = False
    trades = run_day(eng, feed, DAY, make_path(DAY, UP, seed=7), history=flat_history(DAY, 25000))
    assert not [t for t in trades if t.state == TradeState.CLOSED]
