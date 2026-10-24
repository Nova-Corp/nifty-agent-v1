"""One-time (and any-time) setup check. Places NO orders.

    python check_setup.py

Verifies login, public IP, contract master parsing, expiry/strike selection,
historical data, live websocket ticks, and prints raw order book / position
rows so you can confirm the field names in trader/brokers/aliceblue.py (FIELDS).
"""
import json
import time
from datetime import timedelta
from main import connect_alice, public_ip, fetch_history
from trader.clock import now_ist
from trader.config import load_config
from trader.instruments import AliceInstruments, pick_strike
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
cfg = load_config()
u = cfg["underlying"]

print("\n1) Public IP:", public_ip(), "| expected (live):", cfg["broker"].get("expected_static_ip") or "(not set)")
alice = connect_alice(cfg)
print("2) Profile:", alice.get_profile())
print("   Balance:", alice.get_balance())

alice.get_contract_master(u["index_exchange"])
inst = AliceInstruments(alice, u["name"], u["option_exchange"], lot_size_fallback=int(u["lot_size_fallback"]))
today = now_ist().date()
exp = inst.select_expiry(today, int(cfg["strategy"]["min_days_to_expiry"]))
print(f"3) Expiries: {inst.expiries()[:4]} -> selected {exp}")

hist = fetch_history(alice, cfg, now_ist())
spot = hist[-1].close if hist else 25000
ce = inst.option(pick_strike(spot, int(u["strike_step"]), "CE"), "CE", exp)
pe = inst.option(pick_strike(spot, int(u["strike_step"]), "PE"), "PE", exp)
print(f"4) Spot~{spot} ATM CE: {ce}\n   ATM PE: {pe}")

print("5) Websocket test (15 s, raw messages):")
seen = []
def on_msg(m):
    if len(seen) < 8:
        print("   ", m)
    seen.append(m)
def on_open():
    pass
alice.start_websocket(socket_open_callback=on_open, socket_close_callback=lambda: None,
                      socket_error_callback=lambda e: print("   ws error", e),
                      subscription_callback=lambda m: (on_msg(m), json.loads(m).get("t") == "ck" and alice.subscribe(
                          [alice.get_instrument_by_token(u["index_exchange"], int(u["index_token"])),
                           alice.get_instrument_by_token("NFO", int(ce.token))])),
                      run_in_background=True, market_depth=False)
time.sleep(15)
alice.stop_websocket()
print(f"   received {len(seen)} messages (outside market hours you may only see acks)")

print("6) Raw order book rows (check FIELDS names in trader/brokers/aliceblue.py):")
ob = alice.get_order_history("")
print("   ", ob[:2] if isinstance(ob, list) else ob)
print("7) Raw day positions:")
pos = alice.get_daywise_positions()
print("   ", pos[:2] if isinstance(pos, list) else pos)
print("\nDone. No orders were placed.")
