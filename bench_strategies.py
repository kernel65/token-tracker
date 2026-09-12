# -*- coding: utf-8 -*-
"""
Benchmark of detection strategies (does NOT touch production code, only imports
detector.evaluate from the current version).

Compared on synthetic but realistic price paths:
  1) STATIC   — current version (1m@8 / 5m@20 / 15m@40 / 1h@80)
  2) ADAPTIVE — same engine, but threshold = k × token volatility, with a floor
  3) NAIVE-1h — "price vs one hour ago" @40 (classic mistake)
  4) NAIVE-1m — one short window @8 (the spamming kind)

Tokens:
  T1 calm       (σ1m≈0.15%): pump +18% with retrace, spike +300%, flash-crash -30%
  T2 typical    (σ1m≈0.6%):  pump&dump +400%, steady trend +60%, dump -50%
  T3 thin/noisy (σ1m≈1.2% + fat spread outliers): NO events — pure false positives

Metrics: catch (first alert of the right direction BEFORE the peak = tradable),
latency to first alert (s), false alerts/hour outside events.
"""

import math
import random
import statistics
import detector
from detector import evaluate

TICK = 15_000
HOUR = 3_600_000

WINDOWS_STATIC = [
    {"name": "1m", "seconds": 60, "threshold": 8},
    {"name": "5m", "seconds": 300, "threshold": 20},
    {"name": "15m", "seconds": 900, "threshold": 40},
    {"name": "1h", "seconds": 3600, "threshold": 80},
]

# ------------------------------------------------------------------ generator

def gen_path(seed, hours, sig1m, events, fat_prob=0.0, fat_size=0.0):
    """price = exp(accumulated noise) × factor(events).

    events: [(start_min, up_dur_min, mag(4.0=+400%), revert_dur_min; 0=sustained)]
    """
    rng = random.Random(seed)
    n = int(hours * HOUR / TICK)
    t0 = 1_700_000_000_000
    sig_tick = (sig1m / 100.0) / math.sqrt(4.0)
    pts, truth = [], []
    for (s, d, m, r) in events:
        truth.append({"start_ms": t0 + s * 60000,
                      "peak_ms": t0 + (s + d) * 60000,
                      "end_ms": t0 + (s + d + max(r, 0)) * 60000,
                      "dir": "up" if m > 1 else "down",
                      "mag": abs(m - 1) * 100})
    noise = 0.0
    for i in range(n):
        ret = rng.gauss(0, sig_tick)
        if rng.random() < fat_prob:
            ret += rng.choice([-1, 1]) * rng.uniform(fat_size * 0.5, fat_size)
        noise += ret
        min_now = i * TICK / 60000.0
        factor = 1.0
        for (s, d, m, r) in events:
            if min_now < s:
                continue
            if min_now < s + d:
                factor *= 1.0 + (m - 1.0) * (min_now - s) / d
            elif r > 0 and min_now < s + d + r:
                f = (min_now - s - d) / r
                factor *= 1.0 + (m - 1.0) * (1.0 - f)
            elif r == 0:
                factor *= m
        pts.append([t0 + i * TICK, round(math.exp(noise) * factor, 10)])
    return pts, truth

# ------------------------------------------------------------------ strategies

def windows_static(tok, now):
    return WINDOWS_STATIC

def make_adaptive(k):
    """Wrapper OVER THE PRODUCTION function detector.adaptive_windows —
    the test validates exactly the code that runs in the server."""
    def w(tok, now):
        acfg = {"enabled": True, "k": k, "lookbackMin": 30}
        return detector.adaptive_windows(WINDOWS_STATIC, tok["history"], acfg)
    return w

def windows_naive1h(tok, now):
    return [{"name": "1h", "seconds": 3600, "threshold": 40}]

def windows_naive1m(tok, now):
    return [{"name": "1m", "seconds": 60, "threshold": 8}]

# ------------------------------------------------------------------- run

def run(win_fn, pts):
    tok = {"history": [pts[0]], "det": {}, "added_ts": pts[0][0]}
    alerts = []
    for i in range(1, len(pts)):
        ts = pts[i][0]
        tok["history"] = pts[:i + 1]
        for f in evaluate(tok, ts, win_fn(tok, ts), 180):
            alerts.append({"ts": ts, "dir": f["direction"], "pct": f["pct"],
                           "win": f["window"], "reason": f["reason"]})
    return alerts

def score(alerts, truth, warm_ts, measured_hours):
    caught, lats = 0, []
    event_ts = set()
    for ev in truth:
        t = ev["start_ms"]
        while t <= ev["end_ms"] + 10 * 60000:
            event_ts.add(t)
            t += TICK
    for ev in truth:
        mine = [a for a in alerts
                if a["dir"] == ev["dir"] and ev["start_ms"] <= a["ts"] <= ev["peak_ms"]]
        if mine:
            caught += 1
            lats.append(min(a["ts"] for a in mine) - ev["start_ms"])
    false = [a for a in alerts
             if a["ts"] >= warm_ts and a["ts"] not in event_ts]
    return {"caught": f"{caught}/{len(truth)}" if truth else f"—",
            "lat_s": f"{statistics.median(lats)/1000:.0f}" if lats else "—",
            "false_h": f"{len(false)/measured_hours:.1f}",
            "total": len([a for a in alerts if a['ts'] >= warm_ts])}

# ------------------------------------------------------------------ scenarios

SCEN = [
    ("T1 спокойный", dict(hours=6, sig1m=0.15,
        events=[(180, 4, 1.18, 6), (240, 8, 4.0, 12), (300, 2, 0.70, 20)])),
    ("T2 типичный", dict(hours=6, sig1m=0.6,
        events=[(180, 10, 5.0, 12), (260, 40, 1.6, 0), (330, 20, 0.5, 30)])),
    ("T3 тонкий/шум", dict(hours=6, sig1m=1.2, events=[],
        fat_prob=0.03, fat_size=0.09)),
]

STRATS = [
    ("STATIC (нынешняя)", windows_static),
    ("ADAPTIVE k=3", make_adaptive(3.0)),
    ("ADAPTIVE k=4", make_adaptive(4.0)),
    ("ADAPTIVE k=5", make_adaptive(5.0)),
    ("NAIVE 1ч@40", windows_naive1h),
    ("NAIVE 1м@8", windows_naive1m),
]

if __name__ == "__main__":
    for name, kw in SCEN:
        pts, truth = gen_path(7, **kw)
        warm = pts[0][0] + int(2.0 * HOUR)
        measured = kw["hours"] - 2.0
        print(f"\n=== {name} (σ1м={kw['sig1m']}%, событий: {len(truth)}) ===")
        print(f"{'стратегия':18} {'поймано':8} {'лат,с':6} {'ложн/ч':7} всего")
        for sname, fn in STRATS:
            al = run(fn, pts)
            r = score([a for a in al if a["ts"] >= warm], truth, warm, measured)
            print(f"{sname:18} {r['caught']:8} {r['lat_s']:6} {r['false_h']:7} {r['total']}")
