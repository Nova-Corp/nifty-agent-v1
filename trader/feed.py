"""Market data. The engine only sees the MarketFeed interface, so the same engine
runs on the live Alice Blue websocket or on the simulator."""
from __future__ import annotations
import json
import logging
import queue
import threading
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional
from .clock import now_ist
from .models import Tick, OptionContract

log = logging.getLogger("feed")


class MarketFeed(ABC):
    index_token: str

    @abstractmethod
    def subscribe(self, contracts: list[OptionContract]): ...
    @abstractmethod
    def quote(self, token: str) -> Optional[Tick]: ...
    @abstractmethod
    def drain(self) -> list[Tick]: ...
    @abstractmethod
    def healthy(self, now: datetime) -> bool: ...
    def start(self): ...
    def stop(self): ...
    def watchdog(self, now: datetime, market_open: bool): ...


def _f(v) -> Optional[float]:
    try:
        x = float(v)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


class AliceFeed(MarketFeed):
    """Wraps the pya3 websocket.

    Alice sends a full snapshot ('tk') and then partial updates ('tf') containing only
    changed fields, so we merge updates into a per-token state before building a Tick.
    """

    def __init__(self, alice, index_exchange: str, index_token, stale_sec: float = 10, restart_after_sec: float = 30):
        self.alice = alice
        self.index_token = str(index_token)
        self.stale_sec, self.restart_after = stale_sec, restart_after_sec
        self.lock = threading.RLock()
        self.state: dict[str, dict] = {}
        self.quotes: dict[str, Tick] = {}
        self.q: "queue.Queue[Tick]" = queue.Queue()
        self.instruments: dict[str, object] = {}
        self.connected = False
        self.last_index_rx: Optional[datetime] = None
        self.last_restart: Optional[datetime] = None
        self.instruments[self.index_token] = alice.get_instrument_by_token(index_exchange, int(index_token))

    # ---- websocket lifecycle -------------------------------------------------
    def start(self):
        log.info("starting websocket")
        self.alice.start_websocket(socket_open_callback=self._on_open, socket_close_callback=self._on_close,
                                   socket_error_callback=self._on_error, subscription_callback=self._on_msg,
                                   run_in_background=True, market_depth=False)

    def stop(self):
        try:
            self.alice.stop_websocket()
        except Exception as e:  # noqa: BLE001
            log.warning("stop_websocket: %s", e)
        self.connected = False

    def watchdog(self, now: datetime, market_open: bool):
        if not market_open or self.healthy(now):
            return
        if self.last_restart and (now - self.last_restart).total_seconds() < self.restart_after:
            return
        log.warning("feed unhealthy (connected=%s, last index tick=%s); restarting websocket", self.connected, self.last_index_rx)
        self.last_restart = now
        self.stop()
        self.start()

    def _on_open(self):
        log.info("websocket open")

    def _on_close(self):
        log.warning("websocket closed")
        self.connected = False

    def _on_error(self, message):
        log.error("websocket error: %s", message)

    def _resubscribe(self):
        with self.lock:
            insts = [i for i in self.instruments.values() if i]
        if insts:
            self.alice.subscribe(insts)
            log.info("subscribed %d instruments", len(insts))

    # ---- data ------------------------------------------------------------------
    def _on_msg(self, message):
        try:
            m = json.loads(message) if isinstance(message, (str, bytes)) else message
        except ValueError:
            return
        t = m.get("t")
        if t == "ck":
            self.connected = True
            self._resubscribe()
            return
        tk = str(m.get("tk") or "")
        if not tk or t not in ("tk", "tf", "dk", "df"):
            return
        with self.lock:
            st = self.state.setdefault(tk, {})
            for k in ("lp", "bp1", "sp1", "v"):
                if k in m and m[k] not in (None, ""):
                    st[k] = m[k]
            ltp = _f(st.get("lp"))
            if ltp is None:
                return
            vol = st.get("v")
            tick = Tick(tk, ltp, now_ist(), _f(st.get("bp1")), _f(st.get("sp1")),
                        int(float(vol)) if vol not in (None, "") else None)
            self.quotes[tk] = tick
            if tk == self.index_token:
                self.last_index_rx = tick.ts
        self.q.put(tick)

    def subscribe(self, contracts: list[OptionContract]):
        new = []
        with self.lock:
            for c in contracts:
                if c.token not in self.instruments:
                    inst = self.alice.get_instrument_by_token(c.exchange, int(c.token))
                    self.instruments[c.token] = inst
                    new.append(inst)
        if new and self.connected:
            self.alice.subscribe(new)

    def quote(self, token: str) -> Optional[Tick]:
        with self.lock:
            return self.quotes.get(str(token))

    def drain(self) -> list[Tick]:
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out

    def healthy(self, now: datetime) -> bool:
        return bool(self.connected and self.last_index_rx and
                    (now - self.last_index_rx).total_seconds() <= self.stale_sec)
