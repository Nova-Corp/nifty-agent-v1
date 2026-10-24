"""Run synthetic market days through the full pipeline (no broker login needed).

    python simulate.py
"""
import logging
from datetime import date
from trader.sim import build, default_cfg, make_path, run_day, flat_history

logging.basicConfig(level=logging.WARNING, format="%(message)s")

SCENARIOS = {
    "trend_up":        [("09:15", 25000), ("09:30", 25040), ("09:45", 25020), ("11:00", 25180), ("15:30", 25200)],
    "fake_breakout":   [("09:15", 25000), ("09:30", 25040), ("09:50", 25070), ("10:30", 24960), ("15:30", 24940)],
    "trend_down":      [("09:15", 25000), ("09:30", 24960), ("10:00", 24930), ("12:00", 24780), ("15:30", 24760)],
    "chop":            [("09:15", 25000), ("09:30", 25030), ("11:00", 25010), ("13:00", 25025), ("15:30", 25005)],
}

if __name__ == "__main__":
    day = date(2026, 10, 26)
    for name, anchors in SCENARIOS.items():
        eng, feed, _ = build(default_cfg(), day)
        trades = run_day(eng, feed, day, make_path(day, anchors, seed=7), history=flat_history(day, anchors[0][1]))
        print(f"\n=== {name} ===")
        print(eng.summary(day))
        for t in trades:
            if t.state.value == "FAILED":
                print(f"  {t.trade_id} {t.direction} not taken: {t.note}")
