# Nifty Intraday Option-Buying Agent (Alice Blue)

Paper trading by default. Live trading is locked behind three separate switches.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # fill in ALICE_USER_ID and ALICE_API_KEY (Telegram optional)

python -m pytest -q             # 10 safety tests, no login needed
python simulate.py              # 4 synthetic market days through the full pipeline
python check_setup.py           # real login + data checks; places NO orders
python main.py                  # paper trade today (start before 09:15)
```

Alice Blue requires you to log in once a day via web/app before the API session works,
and API sessions are closed at end of day, so run `main.py` fresh each morning
(cron / Task Scheduler at ~09:05).

## Project layout

| Path | Role |
|---|---|
| `trader/strategy_orb.py` | Signal logic only (Opening Range Breakout + EMA filter) |
| `trader/risk.py` | Entry gates, position sizing, quote/spread checks |
| `trader/engine.py` | Order state machine, SL at broker, trailing, exits, kill switch, recovery |
| `trader/brokers/paper.py` | Fills against live bid/ask + slippage; blocks accidental shorts |
| `trader/brokers/aliceblue.py` | Live orders via `pya3` |
| `trader/feed.py` | Websocket feed with partial-update merging, staleness watchdog, auto-reconnect |
| `trader/instruments.py` | Contract master parsing, expiry and strike selection |
| `trader/storage.py` | SQLite: trades, day state, events (`data/trader.db`) |
| `trader/recorder.py` | Every tick saved to `data/ticks/` for future backtests |
| `trader/sim.py` | Black-Scholes market simulator used by tests and `simulate.py` |

## Starting strategy: Opening Range Breakout

1. Opening range = Nifty high/low from 09:15 to 09:30.
2. Skip the day if the range is < 30 or > 150 points.
3. A 5-min candle **closing** above range-high + 5 and above EMA(20) → buy ATM CE.
   Closing below range-low − 5 and below EMA(20) → buy ATM PE.
4. One trade per direction, max 2 trades/day, no entries after 13:30.
5. SL 20% of premium (placed at the broker immediately), target 2R, trail to breakeven
   at +1R then trail by 1R, hard square-off 15:10.
6. Never buys 0-DTE options: on expiry Tuesday it uses next week's expiry.

This is a reasonable, testable baseline, not a proven edge. Treat paper results as the evidence.

## Reliability features

- Stop-loss lives at the broker (SL-limit; SL-M isn't allowed for index options), so a crash
  or disconnect leaves you protected.
- If price gaps through the SL limit and it doesn't fill within 3 s, the bot exits actively.
- SL is cancelled and confirmed before any exit order, so it can't double-sell into a short.
- Exits re-price more aggressively on each retry, falling back to a market (MPP) order.
- Network error during order placement → checks the order book by tag before retrying (no duplicates).
- Daily loss kill switch includes open P&L; it persists across restarts for that day.
- Live mode reconciles broker positions every 30 s; two mismatches freeze trading.
- Restart mid-day → resumes managing the open trade and re-places a missing SL.
- No trades on stale data, on a candle that started before the bot did, or on an incomplete opening range.
- Order book is cached for 1.5 s to stay inside Alice Blue's 1800 requests / 15 min limit.

## Paper-trading phase (do at least 4–6 weeks)

Review daily: `data/logs/<date>.log`, the Telegram summary, and `data/trader.db`.
Track win rate, average win vs average loss, net P&L after charges, and max drawdown.
Things worth checking: how often entries miss (timeouts), real spreads at entry, and
whether SL fills happen near the trigger.

## Go-live checklist

1. `check_setup.py` output looks right; confirm the field names in `FIELDS`
   (`trader/brokers/aliceblue.py`) match the raw order book/positions it prints.
2. Static IP obtained and whitelisted in Alice Blue's API settings (SEBI rule since 1 Apr 2026).
3. Run the bot from a machine with that IP; set `broker.expected_static_ip`.
4. Use a dedicated account (or don't trade Nifty options manually in it): manual
   positions will trip the reconciliation freeze.
5. Set `mode: live`, `live_confirm: "I ACCEPT REAL MONEY RISK"`, run `python main.py --live`.
   Live is capped at `live.max_lots` (1) regardless of `risk.max_lots`.
6. Watch the first live week closely and keep the broker app open.

## Things to keep current

Lot size is read from the contract master, but verify charges (`charges:` section) against
Alice Blue's current sheet, and add NSE holidays to `calendar.holidays`.
