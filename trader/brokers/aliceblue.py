"""Live Alice Blue broker via the official pya3 SDK.

IMPORTANT: Alice Blue's JSON field names are listed in FIELDS below. Run
`python check_setup.py` once; it prints a raw order book and positions so you can
confirm the names and edit FIELDS if your account returns different keys.
"""
from __future__ import annotations
import logging
import time
from typing import Optional
from .base import Broker, BrokerError
from ..models import Order, OrderStatus, OrderType, Side

log = logging.getLogger("aliceblue")

FIELDS = {
    "order_id": ("Nstordno", "NOrdNo", "nestOrderNumber"),
    "status": ("Status", "status", "orderStatus"),
    "avg_price": ("Avgprc", "averagePrice", "AvgPrice"),
    "filled_qty": ("Fillshares", "filledShares", "Fillshare"),
    "reason": ("RejReason", "rejectionReason", "rejReason"),
    "tag": ("remarks", "Remarks", "orderTag", "ordertag"),
    "pos_token": ("Token", "token"),
    "pos_netqty": ("Netqty", "netQty", "NetQty"),
}
STATUS = {"complete": OrderStatus.COMPLETE, "rejected": OrderStatus.REJECTED,
          "cancelled": OrderStatus.CANCELLED, "canceled": OrderStatus.CANCELLED}


def _pick(d: dict, key: str):
    for k in FIELDS[key]:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _ok(resp) -> bool:
    if isinstance(resp, list) and resp:
        resp = resp[0]
    return isinstance(resp, dict) and str(resp.get("stat", "")).lower() == "ok"


class AliceBlueBroker(Broker):
    is_paper = False

    def __init__(self, alice, retries: int = 2, cache_sec: float = 1.5):
        from pya3 import TransactionType, OrderType as AOT, ProductType
        self.alice, self.retries, self.cache_sec = alice, retries, cache_sec
        self.TT, self.AOT, self.PT = TransactionType, AOT, ProductType
        self.meta: dict[str, dict] = {}
        self._ob, self._ob_ts = [], 0.0
        self._inst_cache: dict = {}

    # Non-order API calls are rate-limited (1800 per 15 min), so the order book is cached briefly.
    def _orderbook(self, fresh=False) -> list:
        if fresh or time.monotonic() - self._ob_ts > self.cache_sec:
            ob = self.alice.get_order_history("")
            self._ob = ob if isinstance(ob, list) else []
            self._ob_ts = time.monotonic()
        return self._ob

    def _invalidate(self):
        self._ob_ts = 0.0

    def _aot(self, ot: OrderType):
        return {OrderType.LIMIT: self.AOT.Limit, OrderType.MARKET: self.AOT.Market,
                OrderType.SL: self.AOT.StopLossLimit}[ot]

    def _inst(self, exchange, token):
        key = (exchange, int(token))
        if key in self._inst_cache:              # pya3 re-reads the whole CSV on every lookup; cache it
            return self._inst_cache[key]
        inst = self.alice.get_instrument_by_token(exchange, int(token))
        if not inst or isinstance(inst, dict):
            raise BrokerError(f"instrument not found {exchange}:{token}: {inst}")
        self._inst_cache[key] = inst
        return inst

    def find_by_tag(self, tag: str) -> Optional[str]:
        for row in self._orderbook(fresh=True):
            if str(_pick(row, "tag") or "") == tag:
                return str(_pick(row, "order_id"))
        return None

    def place_order(self, contract, side, qty, order_type, price=0.0, trigger=None, tag=""):
        kw = dict(transaction_type=self.TT.Buy if side == Side.BUY else self.TT.Sell,
                  instrument=self._inst(contract.exchange, contract.token), quantity=int(qty),
                  order_type=self._aot(order_type), product_type=self.PT.Intraday,
                  price=float(price), trigger_price=float(trigger) if trigger else None,
                  stop_loss=None, square_off=None, trailing_sl=None, is_amo=False, order_tag=tag)
        last = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.alice.place_order(**kw)
            except Exception as e:  # network error: the order MAY have reached the exchange
                last = e
                log.warning("place_order exception (%s); checking order book for tag %s", e, tag)
                time.sleep(0.7)
                existing = self.find_by_tag(tag) if tag else None
                if existing:
                    self._remember(existing, contract, side, qty, order_type, price, trigger)
                    return existing
                continue
            self._invalidate()
            if _ok(resp):
                r = resp[0] if isinstance(resp, list) else resp
                oid = str(r.get("NOrdNo") or r.get("nestOrderNumber") or "")
                if oid:
                    self._remember(oid, contract, side, qty, order_type, price, trigger)
                    return oid
            raise BrokerError(f"order rejected by API: {resp}")   # explicit rejection: never blind-retry
        raise BrokerError(f"place_order failed after retries: {last}")

    def _remember(self, oid, contract, side, qty, order_type, price, trigger):
        self.meta[oid] = dict(contract=contract, side=side, qty=qty, order_type=order_type, price=price, trigger=trigger)

    def modify_order(self, order_id, price=None, trigger=None):
        m = self.meta.get(order_id)
        if m is None:
            raise BrokerError(f"no local metadata for order {order_id}")
        new_price = m["price"] if price is None else price
        new_trig = m["trigger"] if trigger is None else trigger
        resp = self.alice.modify_order(transaction_type=self.TT.Buy if m["side"] == Side.BUY else self.TT.Sell,
                                       instrument=self._inst(m["contract"].exchange, m["contract"].token),
                                       order_id=order_id, quantity=int(m["qty"]),
                                       order_type=self._aot(m["order_type"]), product_type=self.PT.Intraday,
                                       price=float(new_price), trigger_price=float(new_trig) if new_trig else 0.0)
        self._invalidate()
        if not _ok(resp):
            raise BrokerError(f"modify rejected: {resp}")
        m["price"], m["trigger"] = new_price, new_trig

    def cancel_order(self, order_id):
        resp = self.alice.cancel_order(order_id)
        self._invalidate()
        if not _ok(resp):
            raise BrokerError(f"cancel rejected: {resp}")

    def get_order(self, order_id) -> Order:
        m = self.meta.get(order_id, {})
        base = Order(order_id, m["contract"].token if m else "", m.get("side", Side.BUY), m.get("qty", 0),
                     m.get("order_type", OrderType.LIMIT), m.get("price", 0.0), m.get("trigger"), "",
                     OrderStatus.UNKNOWN)
        for row in self._orderbook():
            if str(_pick(row, "order_id")) == str(order_id):
                s = str(_pick(row, "status") or "").strip().lower()
                base.status = STATUS.get(s, OrderStatus.OPEN if s else OrderStatus.UNKNOWN)
                base.filled_qty = int(float(_pick(row, "filled_qty") or 0))
                base.avg_price = float(_pick(row, "avg_price") or 0)
                base.message = str(_pick(row, "reason") or "")
                if base.status == OrderStatus.COMPLETE and not base.filled_qty:
                    base.filled_qty = base.qty
                break
        return base

    def positions(self) -> dict[str, int]:
        rows = self.alice.get_daywise_positions()
        out = {}
        if isinstance(rows, list):
            for r in rows:
                tok, q = _pick(r, "pos_token"), _pick(r, "pos_netqty")
                if tok is not None and q not in (None, "") and int(float(q)) != 0:
                    out[str(tok)] = int(float(q))
        return out
