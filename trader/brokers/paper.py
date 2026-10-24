"""Paper broker: matches orders against LIVE bid/ask with slippage, never against LTP alone,
so paper results are closer to what real fills would be."""
from __future__ import annotations
import dataclasses
import itertools
import logging
import threading
from collections import defaultdict
from typing import Optional
from .base import Broker, BrokerError
from ..models import Order, OrderStatus, OrderType, Side
from ..util import round_tick

log = logging.getLogger("paper")


class PaperBroker(Broker):
    is_paper = True

    def __init__(self, feed, slippage_ticks: int = 1, tick: float = 0.05):
        self.feed, self.slip_ticks, self.tick = feed, slippage_ticks, tick
        self.orders: dict[str, Order] = {}
        self.pos: dict[str, int] = defaultdict(int)
        self.lot: dict[str, int] = {}
        self._ids = itertools.count(1)
        self.lock = threading.RLock()

    def place_order(self, contract, side, qty, order_type, price=0.0, trigger=None, tag=""):
        with self.lock:
            if qty <= 0 or qty % contract.lot_size:
                raise BrokerError(f"qty {qty} not a multiple of lot {contract.lot_size}")
            if order_type in (OrderType.LIMIT, OrderType.SL) and price <= 0:
                raise BrokerError("limit price required")
            if order_type == OrderType.SL and (trigger is None or (side == Side.SELL and price > trigger)):
                raise BrokerError("invalid SL trigger/limit")
            if side == Side.SELL:
                pending = sum(o.qty for o in self.orders.values()
                              if o.token == contract.token and o.side == Side.SELL and o.status == OrderStatus.OPEN)
                if pending + qty > self.pos[contract.token]:
                    raise BrokerError("sell would create a short position (blocked)")
            oid = f"P{next(self._ids):06d}"
            o = Order(oid, contract.token, side, qty, order_type, round_tick(price, self.tick),
                      round_tick(trigger, self.tick) if trigger else None, tag)
            self.orders[oid] = o
            self._try_fill(o)
            return oid

    def modify_order(self, order_id, price=None, trigger=None):
        with self.lock:
            o = self.orders.get(order_id)
            if o is None or o.status != OrderStatus.OPEN:
                raise BrokerError(f"order {order_id} not open")
            if price is not None:
                o.price = round_tick(price, self.tick)
            if trigger is not None:
                o.trigger = round_tick(trigger, self.tick)
                o.triggered = False
            self._try_fill(o)

    def cancel_order(self, order_id):
        with self.lock:
            o = self.orders.get(order_id)
            if o and o.status == OrderStatus.OPEN:
                o.status = OrderStatus.CANCELLED

    def get_order(self, order_id) -> Order:
        with self.lock:
            o = self.orders.get(order_id)
            if o is None:
                return Order(order_id, "", Side.BUY, 0, OrderType.LIMIT, 0, None, "", OrderStatus.UNKNOWN)
            return dataclasses.replace(o)

    def positions(self):
        with self.lock:
            return {k: v for k, v in self.pos.items() if v}

    def adopt_position(self, token: str, qty: int):
        """Used after a restart to rebuild paper positions from the database."""
        with self.lock:
            self.pos[token] = qty

    def process(self, now):
        with self.lock:
            for o in list(self.orders.values()):
                if o.status == OrderStatus.OPEN:
                    self._try_fill(o)

    def _try_fill(self, o: Order):
        q = self.feed.quote(o.token)
        if q is None:
            return
        slip = self.slip_ticks * self.tick
        bid, ask = q.bid or q.ltp, q.ask or q.ltp
        px: Optional[float] = None
        if o.order_type == OrderType.MARKET:
            px = ask + slip if o.side == Side.BUY else max(self.tick, bid - slip)
        else:
            if o.order_type == OrderType.SL and not o.triggered:
                hit = q.ltp <= o.trigger if o.side == Side.SELL else q.ltp >= o.trigger
                if not hit:
                    return
                o.triggered = True
            if o.side == Side.BUY and ask <= o.price:
                px = min(o.price, ask + slip)
            elif o.side == Side.SELL and bid >= o.price:
                px = max(o.price, bid - slip)
        if px is None:
            return
        o.status, o.filled_qty, o.avg_price = OrderStatus.COMPLETE, o.qty, round_tick(px, self.tick)
        self.pos[o.token] += o.qty if o.side == Side.BUY else -o.qty
        log.info("PAPER FILL %s %s %d @ %.2f (%s)", o.side.value, o.token, o.qty, o.avg_price, o.tag)
