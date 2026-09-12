# -*- coding: utf-8 -*-
"""Simulation: a +400% pump with retrace must be caught; «ghosts» must not."""
import time
from detector import evaluate

WINDOWS = [
    {"name": "1m", "seconds": 60, "threshold": 8},
    {"name": "5m", "seconds": 300, "threshold": 20},
    {"name": "15m", "seconds": 900, "threshold": 40},
    {"name": "1h", "seconds": 3600, "threshold": 80},
    {"name": "24h", "seconds": 86400, "threshold": 150},
]

def simulate():
    tok = {"history": [], "det": {}, "added_ts": 0}
    t0 = 1_700_000_000_000
    tok["added_ts"] = t0
    fires = []
    price = 0.01
    # 30 min flat, then pump +400% over 10 min, retrace over 5 min, another 15 min flat
    # poll every 15s
    def tick(ts, p):
        tok["history"].append([ts, p])
        if len(tok["history"]) > 10000:
            tok["history"] = tok["history"][-9000:]
        f = evaluate(tok, ts, WINDOWS, 180)
        for x in f:
            fires.append((ts, x["direction"], x["window"], x["pct"], x["reason"]))
    minutes = lambda m: m * 60 * 1000
    # phase 1: 35 min flat (with warm-up)
    for k in range(0, 35 * 60, 15):
        tick(t0 + minutes(k / 60), price)
    # phase 2: pump to 0.05 (+400%) over 10 min
    for k in range(1, 10 * 60 + 1, 15):
        frac = k / (10 * 60)
        tick(t0 + minutes((35 * 60 + k) / 60), price * (1 + 4.0 * frac))
    # phase 3: retrace to 0.01 over 5 min
    for k in range(1, 5 * 60 + 1, 15):
        frac = k / (5 * 60)
        tick(t0 + minutes((45 * 60 + k) / 60), 0.05 * (1 - 0.8 * frac))
    # phase 4: 20 min flat
    base_t = 50 * 60
    for k in range(1, 20 * 60 + 1, 15):
        tick(t0 + minutes((base_t + k) / 60), 0.01)

    ups = [f for f in fires if f[1] == "up"]
    downs = [f for f in fires if f[1] == "down"]
    print(f"всего алертов: {len(fires)} (up={len(ups)}, down={len(downs)})")
    for f in fires:
        print(f"  t=+{f[0]-t0:>9} {f[1]:4} {f[2]:3} {f[3]:+7.1f}% {f[4]}")
    assert ups, "памп +400% НЕ пойман — провал"
    assert downs, "возврат (дампа после пика) НЕ пойман — провал"
    # after stabilization (last 15 min) there must be no alerts
    tail_start = t0 + minutes(base_t + 5 * 60)
    ghosts = [f for f in fires if f[0] >= tail_start]
    assert not ghosts, f"призрачные алерты после стабилизации: {ghosts}"
    print("PASS: памп пойман в росте, возврат пойман, призраков нет")

if __name__ == "__main__":
    simulate()
