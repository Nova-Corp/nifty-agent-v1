"""Indian F&O option charges per order. Rates live in config so you can update them."""
from .models import Side


def order_charges(side: Side, price: float, qty: int, c: dict) -> float:
    if qty <= 0:
        return 0.0
    turnover = price * qty
    brokerage = float(c.get("brokerage_per_order", 20))
    stt = turnover * float(c.get("stt_sell_pct", 0.15)) / 100 if side == Side.SELL else 0.0
    exch = turnover * float(c.get("exchange_txn_pct", 0.03503)) / 100
    sebi = turnover * float(c.get("sebi_per_crore", 10)) / 1e7
    stamp = turnover * float(c.get("stamp_buy_pct", 0.003)) / 100 if side == Side.BUY else 0.0
    gst = (brokerage + exch + sebi) * float(c.get("gst_pct", 18)) / 100
    return round(brokerage + stt + exch + sebi + stamp + gst, 2)
