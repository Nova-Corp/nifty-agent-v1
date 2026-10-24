"""The trading engine.

Every trade moves through a small non-blocking state machine:

  AWAIT_QUOTE -> ENTRY_PENDING -> OPEN -> EXITING -> CLOSED
        \\______________\\_____________________________-> FAILED

Key safety properties:
  * A stop-loss order sits at the BROKER immediately after entry, so a crash or
    lost connection still leaves the position protected.
  * The SL order is cancelled (and confirmed) before any exit order is sent, and the
    paper broker refuses sells larger than the holding, so the bot never goes short.
  * If an SL triggers but does not fill (price gapped through), we exit actively.
  * Daily loss kill switch, reconciliation with broker positions, crash recovery.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Optional

from .brokers.base import BrokerError
from .candles import CandleBuilder
from .charges import order_charges
from .clock import hhmm
from .instruments import pick_strike
from .models import (DayState, OrderStatus, OrderType, Side, Signal, Trade, TradeState, TERMINAL, Candle)
from .util import round_tick

log = logging.getLogger("engine")


class Engine:
    def __init__(self, cfg, broker, feed, instruments, strategy, risk, store, alerts, recorder=None):
        self.cfg, self.broker, self.feed, self.instruments = cfg, broker, feed, instruments
        self.strategy, self.risk, self.store, self.alerts, self.recorder = strategy, risk, store, alerts, recorder
        s, r, x, u = cfg["strategy"], cfg["risk"], cfg["execution"], cfg["underlying"]
        self.tick = float(x.get("tick_size", 0.05))
        self.index_token = str(u["index_token"])
        self.strike_step = int(u["strike_step"])
        self.itm_offset = int(s.get("strike_itm_offset", 0))
        self.min_dte = int(s.get("min_days_to_expiry", 1))
        self.sl_pct, self.rr = float(r["sl_pct"]), float(r["target_rr"])
        self.trail_after, self.trail_gap = float(r.get("trail_after_rr", 1.0)), float(r.get("trail_gap_rr", 1.0))
        self.max_lots = int(r["max_lots"])
        self.max_daily_loss = float(r["max_daily_loss"])
        self.square_off = hhmm(r["square_off_time"])
        self.entry_buffer_ticks = int(x.get("entry_limit_buffer_ticks", 4))
        sec = lambda k, d: timedelta(seconds=float(x.get(k, d)))
        self.entry_timeout, self.quote_timeout = sec("entry_timeout_sec", 10), sec("quote_timeout_sec", 5)
        self.exit_timeout, self.reconcile_every = sec("exit_timeout_sec", 4), sec("reconcile_every_sec", 30)
        self.sl_unfilled, self.max_quote_age = sec("sl_unfilled_sec", 3), sec("max_quote_age_sec", 5)
        self.max_exit_attempts = int(x.get("max_exit_attempts", 5))
        self.sl_limit_buffer_pct = float(x.get("sl_limit_buffer_pct", 5))
        self.candles = CandleBuilder(int(s["timeframe_min"]))
        self.trade: Optional[Trade] = None
        self.day: Optional[DayState] = None
        self.started_at: Optional[datetime] = None
        self._last_reconcile: Optional[datetime] = None
        self._mismatches = 0

    # ------------------------------------------------------------------ setup
    def start_day(self, today, now: datetime, history: Optional[list[Candle]] = None):
        self.started_at = now
        self.day = self.store.load_day(today) or DayState(day=today)
        self.strategy.reset_day(today)
        for d in self.day.directions_taken:
            self.strategy.mark_taken(d)
        for c in history or []:
            self.strategy.on_candle(c, allow_signal=False)
            if c.start.date() == today:
                self.candles.last_closed_start = c.start
        self.store.save_day(self.day)
        for t in self.store.active_trades(today):
            self._recover(t, now)
        log.info("day started %s | trades so far %d | realized %.2f | kill=%s",
                 today, self.day.trades_taken, self.day.realized_pnl, self.day.kill_switch)

    # ------------------------------------------------------------------ main loop
    def step(self, now: datetime):
        closed = []
        for t in self.feed.drain():
            if self.recorder:
                self.recorder.write(t)
            if t.token == self.index_token:
                closed.extend(self.candles.on_price(t.ts, t.ltp))
        closed.extend(self.candles.flush(now))
        self.broker.process(now)
        if self.trade:
            self._advance(now)
        self._check_kill(now)
        for c in closed:
            # A candle that began before we started running is incomplete: never trade on it.
            allow = self.started_at is None or c.start >= self.started_at
            sig = self.strategy.on_candle(c, allow_signal=allow)
            if sig:
                self._on_signal(sig, now)
        if not self.broker.is_paper:
            self._reconcile(now)

    # ------------------------------------------------------------------ signals
    def _on_signal(self, sig: Signal, now: datetime):
        busy = self.trade is not None
        ok, why = self.risk.check_entry(now, self.day, busy, self.feed.healthy(now))
        if not ok:
            self._event(now, "INFO", f"signal {sig.direction} skipped: {why}")
            return
        try:
            expiry = self.instruments.select_expiry(now.date(), self.min_dte)
        except RuntimeError as e:
            self._event(now, "WARN", f"signal skipped: {e}")
            return
        strike = pick_strike(sig.underlying_price, self.strike_step, sig.direction, self.itm_offset)
        contract = self.instruments.option(strike, sig.direction, expiry)
        if contract is None:
            self._event(now, "WARN", f"signal skipped: no contract NIFTY {expiry} {strike} {sig.direction}")
            return
        self.feed.subscribe([contract])
        self.day.directions_taken.append(sig.direction)
        self.store.save_day(self.day)
        seq = len(self.store.trades_for_day(now.date())) + 1
        t = Trade(trade_id=f"{now:%Y%m%d}-{seq:02d}", day=now.date(), direction=sig.direction,
                  signal_reason=sig.reason, underlying_at_signal=sig.underlying_price, created=now,
                  contract=contract, deadline=now + self.quote_timeout)
        self.trade = t
        self._save(t)
        self.alerts.send(f"SIGNAL {sig.direction} | {sig.reason} | contract {contract.symbol}")

    # ------------------------------------------------------------------ state machine
    def _advance(self, now: datetime):
        t = self.trade
        {TradeState.AWAIT_QUOTE: self._await_quote, TradeState.ENTRY_PENDING: self._entry_pending,
         TradeState.OPEN: self._manage_open, TradeState.EXITING: self._exiting}[t.state](t, now)

    def _tag(self, t: Trade, kind: str) -> str:
        return f"NB{t.trade_id.replace('-', '')[2:]}{kind}{t.exit_attempts}"   # short alphanumeric

    def _await_quote(self, t: Trade, now: datetime):
        q = self.feed.quote(t.contract.token)
        fresh = q is not None and (now - q.ts) <= self.max_quote_age
        if not fresh or not q.ask:
            if now >= t.deadline:
                self._fail(t, now, "no fresh quote for option")
            return
        ok, why = self.risk.check_quote(q)
        if not ok:
            self._fail(t, now, f"quote check failed: {why}")
            return
        sl = round_tick(q.ask * (1 - self.sl_pct / 100), self.tick)
        lots, why = self.risk.size(q.ask, sl, t.contract.lot_size, self.max_lots)
        if lots < 1:
            self._fail(t, now, f"sizing: {why}")
            return
        t.planned_qty = lots * t.contract.lot_size
        limit = round_tick(q.ask + self.entry_buffer_ticks * self.tick, self.tick)
        try:
            t.entry_order_id = self.broker.place_order(t.contract, Side.BUY, t.planned_qty, OrderType.LIMIT,
                                                       price=limit, tag=self._tag(t, "E"))
        except BrokerError as e:
            self._fail(t, now, f"entry order error: {e}")
            return
        t.state, t.deadline = TradeState.ENTRY_PENDING, now + self.entry_timeout
        self._save(t)
        self._event(now, "INFO", f"{t.trade_id} entry BUY {t.planned_qty} {t.contract.symbol} limit {limit}")

    def _entry_pending(self, t: Trade, now: datetime):
        o = self.broker.get_order(t.entry_order_id)
        if o.status == OrderStatus.COMPLETE:
            self._on_entry_filled(t, o.filled_qty or t.planned_qty, o.avg_price, now)
        elif o.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            if o.filled_qty > 0:
                self._on_entry_filled(t, o.filled_qty, o.avg_price, now)
            else:
                self._fail(t, now, f"entry {o.status.value} {o.message}".strip())
        elif now >= t.deadline:
            if not t.cancel_requested:
                try:
                    self.broker.cancel_order(t.entry_order_id)
                except BrokerError as e:
                    log.warning("entry cancel error: %s", e)
                t.cancel_requested, t.deadline = True, now + timedelta(seconds=10)
                self._event(now, "INFO", f"{t.trade_id} entry not filled in time; cancelling")
            else:
                self._freeze(now, f"{t.trade_id} entry order {t.entry_order_id} stuck; check broker manually")
                t.deadline = now + timedelta(seconds=60)

    def _on_entry_filled(self, t: Trade, qty: int, avg: float, now: datetime):
        t.qty, t.entry_price, t.entry_time, t.highest = qty, avg, now, avg
        t.risk_pts = round_tick(avg * self.sl_pct / 100, self.tick)
        t.sl = round_tick(avg - t.risk_pts, self.tick)
        t.target = round_tick(avg + self.rr * t.risk_pts, self.tick)
        t.state = TradeState.OPEN
        self.day.trades_taken += 1
        self.store.save_day(self.day)
        self.alerts.send(f"ENTRY {t.trade_id} BUY {qty} {t.contract.symbol} @ {avg:.2f} | SL {t.sl} | TGT {t.target}")
        if not self._place_sl(t, now):
            self.alerts.send(f"{t.trade_id} could not place stop-loss; exiting immediately", "CRITICAL")
            self._exit(t, now, "SL_PLACE_FAILED")
        self._save(t)

    def _sl_limit(self, sl: float) -> float:
        return round_tick(max(self.tick, sl - max(sl * self.sl_limit_buffer_pct / 100, 2 * self.tick)), self.tick)

    def _place_sl(self, t: Trade, now: datetime) -> bool:
        rem = t.qty - t.exit_filled_qty
        t.sl_limit = self._sl_limit(t.sl)
        for _ in range(2):
            try:
                t.sl_order_id = self.broker.place_order(t.contract, Side.SELL, rem, OrderType.SL, price=t.sl_limit,
                                                        trigger=t.sl, tag=self._tag(t, "S"))
                return True
            except BrokerError as e:
                log.error("SL placement failed: %s", e)
        return False

    def _manage_open(self, t: Trade, now: datetime):
        if t.sl_order_id:
            o = self.broker.get_order(t.sl_order_id)
            if o.status in TERMINAL:
                self._absorb(t, o)
                if self._remaining(t) == 0:
                    self._finalize(t, now, "TRAILING_SL" if t.sl >= t.entry_price else "STOPLOSS")
                    return
                self.alerts.send(f"{t.trade_id} SL order {o.status.value} unexpectedly ({o.message}); re-placing", "WARN")
                t.sl_order_id = None
                if not self._place_sl(t, now):
                    self._exit(t, now, "SL_PLACE_FAILED")
                    return
        q = self.feed.quote(t.contract.token)
        if q is None:
            return
        ltp = q.ltp
        t.highest = max(t.highest, ltp)
        if now.time() >= self.square_off:
            self._exit(t, now, "TIME_EXIT")
        elif self.day.kill_switch:
            self._exit(t, now, "KILL_SWITCH")
        elif ltp >= t.target:
            self._exit(t, now, "TARGET")
        elif ltp <= t.sl_limit:
            # SL triggered but price is below our SL limit: the order may never fill.
            t.sl_breach_since = t.sl_breach_since or now
            if now - t.sl_breach_since >= self.sl_unfilled:
                self._exit(t, now, "SL_GAP_EXIT")
        else:
            t.sl_breach_since = None
            self._trail(t, now, ltp)

    def _trail(self, t: Trade, now: datetime, ltp: float):
        if t.highest < t.entry_price + self.trail_after * t.risk_pts or not t.sl_order_id:
            return
        new_sl = round_tick(max(t.entry_price, t.highest - self.trail_gap * t.risk_pts), self.tick)
        if new_sl < t.sl + 2 * self.tick or new_sl >= ltp - self.tick:
            return
        if t.last_sl_modify and (now - t.last_sl_modify).total_seconds() < 5:
            return
        try:
            self.broker.modify_order(t.sl_order_id, price=self._sl_limit(new_sl), trigger=new_sl)
            t.sl, t.sl_limit, t.last_sl_modify = new_sl, self._sl_limit(new_sl), now
            self._save(t)
            self._event(now, "INFO", f"{t.trade_id} trailed SL to {new_sl}")
        except BrokerError as e:
            log.warning("trail modify failed: %s", e)
            t.last_sl_modify = now

    # ------------------------------------------------------------------ exits
    def _exit(self, t: Trade, now: datetime, reason: str):
        if t.state == TradeState.EXITING:
            return
        t.exit_reason, t.state, t.exit_order_id = reason, TradeState.EXITING, None
        self._event(now, "INFO", f"{t.trade_id} exiting: {reason}")
        if t.sl_order_id:
            o = self.broker.get_order(t.sl_order_id)
            if o.status == OrderStatus.OPEN or o.status == OrderStatus.UNKNOWN:
                try:
                    self.broker.cancel_order(t.sl_order_id)
                except BrokerError as e:
                    log.warning("SL cancel error: %s", e)
        t.deadline = now + self.exit_timeout
        self._exiting(t, now)

    def _exiting(self, t: Trade, now: datetime):
        if t.exit_order_id is None:
            if t.sl_order_id:
                o = self.broker.get_order(t.sl_order_id)
                if o.status in TERMINAL:
                    self._absorb(t, o)
                    t.sl_order_id = None
                    if self._remaining(t) == 0:
                        self._finalize(t, now, "STOPLOSS" if o.status == OrderStatus.COMPLETE else None)
                        return
                elif now >= t.deadline:                     # cancel not confirmed yet
                    t.exit_attempts += 1
                    if t.exit_attempts > self.max_exit_attempts:
                        self._freeze(now, f"{t.trade_id} cannot cancel SL order; manual action needed")
                    try:
                        self.broker.cancel_order(t.sl_order_id)
                    except BrokerError:
                        pass
                    t.deadline = now + self.exit_timeout
                    return
                else:
                    return
            if self._remaining(t) == 0:
                self._finalize(t, now, "STOPLOSS" if not t.exit_reason else None)
                return
            self._place_exit_order(t, now)
            return
        o = self.broker.get_order(t.exit_order_id)
        if o.status in TERMINAL:
            self._absorb(t, o)
            if self._remaining(t) == 0:
                self._finalize(t, now)
                return
            if o.status == OrderStatus.REJECTED:
                self.alerts.send(f"{t.trade_id} exit order rejected: {o.message}", "WARN")
            t.exit_order_id = None
            t.exit_attempts += 1
            self._place_exit_order(t, now)
        elif now >= t.deadline:
            try:
                self.broker.cancel_order(t.exit_order_id)       # next step re-prices more aggressively
            except BrokerError as e:
                log.warning("exit cancel error: %s", e)
            t.deadline = now + self.exit_timeout

    def _place_exit_order(self, t: Trade, now: datetime):
        rem = self._remaining(t)
        if not self.broker.is_paper:
            held = self.broker.positions().get(t.contract.token, 0)
            if held < rem:
                if held <= 0:
                    t.exit_attempts += 1
                    if t.exit_attempts > self.max_exit_attempts:
                        self._freeze(now, f"{t.trade_id}: broker shows no position but trade not closed; verify")
                        t.exit_price = t.exit_price or t.entry_price
                        self._finalize(t, now, "BROKER_FLAT_UNKNOWN")
                    t.deadline = now + self.exit_timeout
                    return
                rem = held
        q = self.feed.quote(t.contract.token)
        ref = (q.bid or q.ltp) if q else None
        if ref is None or t.exit_attempts >= self.max_exit_attempts:
            otype, px = OrderType.MARKET, 0.0
        else:
            otype = OrderType.LIMIT
            px = round_tick(max(self.tick, ref - (2 + 3 * t.exit_attempts) * self.tick), self.tick)
        try:
            t.exit_order_id = self.broker.place_order(t.contract, Side.SELL, rem, otype, price=px, tag=self._tag(t, "X"))
        except BrokerError as e:
            t.exit_attempts += 1
            self.alerts.send(f"{t.trade_id} exit order error: {e}", "CRITICAL" if t.exit_attempts > 2 else "WARN")
            if t.exit_attempts > self.max_exit_attempts + 2:
                self._freeze(now, f"{t.trade_id} repeated exit failures; square off manually")
        t.deadline = now + self.exit_timeout
        self._save(t)

    def _absorb(self, t: Trade, o):
        if o.order_id in t.processed_orders:
            return
        t.processed_orders.append(o.order_id)
        if o.filled_qty > 0 and o.avg_price > 0:
            t.exit_filled_qty += o.filled_qty
            t.exit_value += o.filled_qty * o.avg_price

    def _remaining(self, t: Trade) -> int:
        return max(0, t.qty - t.exit_filled_qty)

    def _finalize(self, t: Trade, now: datetime, reason: Optional[str] = None):
        cc = self.cfg["charges"]
        if t.exit_filled_qty:
            t.exit_price = round(t.exit_value / t.exit_filled_qty, 2)
        t.exit_time = now
        if reason:
            t.exit_reason = reason
        t.gross_pnl = round((t.exit_price - t.entry_price) * t.qty, 2)
        t.charges = round(order_charges(Side.BUY, t.entry_price, t.qty, cc) +
                          order_charges(Side.SELL, t.exit_price, t.qty, cc), 2)
        t.net_pnl = round(t.gross_pnl - t.charges, 2)
        t.state = TradeState.CLOSED
        self.day.realized_pnl = round(self.day.realized_pnl + t.net_pnl, 2)
        self.store.save_day(self.day)
        self._save(t)
        self.alerts.send(f"EXIT {t.trade_id} {t.exit_reason} @ {t.exit_price:.2f} | net Rs {t.net_pnl:+.2f} "
                         f"(charges {t.charges:.2f}) | day Rs {self.day.realized_pnl:+.2f}")
        self.trade = None

    def _fail(self, t: Trade, now: datetime, why: str):
        t.state, t.note = TradeState.FAILED, why
        self._save(t)
        self._event(now, "WARN", f"{t.trade_id} not taken: {why}")
        self.trade = None

    # ------------------------------------------------------------------ protection
    def _check_kill(self, now: datetime):
        if self.day.kill_switch:
            return
        unreal = 0.0
        t = self.trade
        if t and t.state == TradeState.OPEN:
            q = self.feed.quote(t.contract.token)
            if q:
                unreal = (q.ltp - t.entry_price) * t.qty
        if self.day.realized_pnl + unreal <= -self.max_daily_loss:
            self._freeze(now, f"daily loss limit hit (realized {self.day.realized_pnl:.0f}, open {unreal:.0f})")

    def _freeze(self, now: datetime, reason: str):
        if not self.day.kill_switch:
            self.day.kill_switch, self.day.kill_reason = True, reason
            self.store.save_day(self.day)
            self.alerts.send(f"KILL SWITCH: {reason}. No new trades today.", "CRITICAL")

    def _reconcile(self, now: datetime):
        if self._last_reconcile and now - self._last_reconcile < self.reconcile_every:
            return
        self._last_reconcile = now
        t = self.trade
        if t and t.state in (TradeState.ENTRY_PENDING, TradeState.EXITING, TradeState.AWAIT_QUOTE):
            return
        try:
            broker_pos = {k: v for k, v in self.broker.positions().items()}
        except Exception as e:  # noqa: BLE001
            log.warning("reconcile: positions call failed: %s", e)
            return
        expected = {t.contract.token: self._remaining(t)} if t and t.state == TradeState.OPEN else {}
        if broker_pos == expected:
            self._mismatches = 0
            return
        self._mismatches += 1
        log.warning("reconcile mismatch #%d broker=%s expected=%s", self._mismatches, broker_pos, expected)
        if self._mismatches >= 2:
            self._freeze(now, f"position mismatch broker={broker_pos} bot={expected}")

    # ------------------------------------------------------------------ recovery
    def _recover(self, t: Trade, now: datetime):
        self.alerts.send(f"recovering trade {t.trade_id} in state {t.state.value}", "WARN")
        self.feed.subscribe([t.contract])
        if t.state in (TradeState.AWAIT_QUOTE, TradeState.ENTRY_PENDING) and self.broker.is_paper:
            self._fail(t, now, "stale after restart")
            return
        if t.state == TradeState.AWAIT_QUOTE:
            self._fail(t, now, "stale signal after restart")
            return
        self.trade = t
        if t.state == TradeState.ENTRY_PENDING:
            t.deadline = now
            return
        if self.broker.is_paper:
            self.broker.adopt_position(t.contract.token, self._remaining(t))
            t.sl_order_id, t.exit_order_id = None, None
            if t.state == TradeState.OPEN:
                self._place_sl(t, now)
            return
        held = self.broker.positions().get(t.contract.token, 0)
        if held <= 0:
            self.alerts.send(f"{t.trade_id}: broker shows no position after restart; marking closed. "
                             f"CHECK your tradebook for the real exit price.", "CRITICAL")
            t.exit_price = t.exit_price or t.entry_price
            self._finalize(t, now, "CLOSED_WHILE_OFFLINE")
            return
        if t.state == TradeState.OPEN:
            o = self.broker.get_order(t.sl_order_id) if t.sl_order_id else None
            if o is None or o.status != OrderStatus.OPEN:
                t.sl_order_id = None
                self._place_sl(t, now)

    # ------------------------------------------------------------------ misc
    def _save(self, t: Trade):
        self.store.save_trade(t)

    def _event(self, now: datetime, level: str, msg: str):
        getattr(log, "warning" if level == "WARN" else "info")(msg)
        self.store.event(now, level, msg)

    def flatten(self, now: datetime, reason: str = "MANUAL_FLATTEN"):
        if self.trade and self.trade.state == TradeState.OPEN:
            self._exit(self.trade, now, reason)

    def summary(self, d) -> str:
        trades = [t for t in self.store.trades_for_day(d) if t.state == TradeState.CLOSED]
        wins = sum(1 for t in trades if t.net_pnl > 0)
        lines = [f"Day {d} summary: {len(trades)} trades, {wins} winners, net Rs {sum(t.net_pnl for t in trades):+.2f}"]
        for t in trades:
            lines.append(f"  {t.trade_id} {t.contract.symbol} qty {t.qty} {t.entry_price:.2f}->{t.exit_price:.2f} "
                         f"{t.exit_reason} net {t.net_pnl:+.2f}")
        if self.day and self.day.kill_switch:
            lines.append(f"  kill switch: {self.day.kill_reason}")
        return "\n".join(lines)
