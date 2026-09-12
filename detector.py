# -*- coding: utf-8 -*-
"""
Price-move detector (standalone, no external dependencies).

Why "price now vs an hour ago" does not work:
  a memecoin can do +400% in 10 minutes and come back.
  By the hour mark the change is ~0% — no alert fires, yet the move was Tradable.

Solution — several independent sliding windows (1m/5m/15m/1h/24h):
  * net        = change over the window (sustained trend)
  * up_pct     = how far the high (peak) went from the window start
  * down_pct   = how far the low (trough) went from the window start
An alert fires if the TRIGGER is fresh:
  - trend: net >= threshold (price held the move)
  - spike: peak/trough >= threshold AND the peak was in the last ~20% of the window
           (the pullback after the peak is still caught, while the "ghost" of an old
            peak does not fire forever)
Additionally: per-direction cooldown (separate up/down),
window warmup (no data — no decisions).
"""

from bisect import bisect_left
import math
import statistics

# Floors for automatic thresholds: the threshold never drops below them even if
# the token is absolutely calm (insurance against alerts on microscopic noise).
DEFAULT_FLOORS = {"1m": 4.0, "5m": 6.0, "15m": 9.0, "1h": 15.0}


def estimate_vol(pts, lookback_ms):
    """Volatility estimate for a token from recent log-returns.

    Returns (sigma_of_one_step, median_dt_ms) or (None, None) if there is not
    enough data. Seed gaps (coarse history from the API priceChange) and
    rare polls are filtered out: returns with dt > 6*median are not counted.
    """
    if not pts or len(pts) < 25:
        return None, None
    cutoff = pts[-1][0] - lookback_ms
    seg = [p for p in pts if p[0] >= cutoff]
    if len(seg) < 20:
        seg = pts[-25:]
    if len(seg) < 20:
        return None, None
    pairs = list(zip(seg, seg[1:]))
    dts = [b[0] - a[0] for a, b in pairs if b[0] > a[0]]
    if len(dts) < 15:
        return None, None
    dt = statistics.median(dts)
    if dt <= 0:
        return None, None
    rets = []
    for (t1, p1), (t2, p2) in pairs:
        if p1 and p2 and p1 > 0 and p2 > 0 and (t2 - t1) < dt * 6:
            rets.append(math.log(p2 / p1))
    if len(rets) < 12:
        return None, None
    try:
        sig = statistics.stdev(rets)
    except statistics.StatisticsError:
        return None, None
    if not sig or sig <= 0:
        return None, None
    return sig, dt


def adaptive_windows(base_windows, pts, acfg, floors=None):
    """If adaptive is on — thresholds = max(floor, k × sigma_window).

    sigma_window = sigma_step × sqrt(window/dt). A calm token gets low
    thresholds (catches +18%), a noisy one gets high (no spam on junk).
    Windows with no floor in the table (e.g. 24h) always take the static threshold.
    """
    floors = floors or DEFAULT_FLOORS
    if not acfg or not acfg.get("enabled"):
        return base_windows
    look_ms = int(acfg.get("lookbackMin", 30)) * 60000
    k = float(acfg.get("k", 4.0))
    sig, dt = estimate_vol(pts, look_ms)
    if sig is None or dt is None:
        return base_windows          # not enough data → work off statics
    out = []
    for w in base_windows:
        floor = floors.get(w.get("name"))
        if floor is None:
            out.append(w)
            continue
        window_ms = max(1, int(w.get("seconds") or 1)) * 1000
        sig_win = sig * math.sqrt(window_ms / dt) * 100.0
        thr = max(float(floor), k * sig_win)
        out.append({**w, "threshold": round(thr, 1)})
    return out


def _pct(new, base):
    if not base or base <= 0:
        return 0.0
    return (new - base) / base * 100.0


def evaluate(tok, now_ms, windows, cooldown_s):
    """Returns the list of firings for a token on the given tick.

    tok = {
      "history": [[ts_ms, price], ...],   # sorted by time
      "det": {state},                    # persistent detector state
      "added_ts": ts_ms,                 # when added to the tracker
    }
    windows = [{"name": "5m", "seconds": 300, "threshold": 20}, ...]
    """
    det = tok.setdefault("det", {})
    pts = tok["history"]
    if not pts:
        return []
    ts_list = [p[0] for p in pts]
    ts_now, price = pts[-1]
    warm_anchor = max(int(tok.get("added_ts") or 0), ts_list[0])

    up_at = det.get("last_alert_up") or 0
    down_at = det.get("last_alert_down") or 0
    cooldown_ms = int(cooldown_s) * 1000

    candidates = []
    for w in windows:
        sec = int(w.get("seconds") or 0)
        thr = float(w.get("threshold") or 100)
        name = w.get("name") or f"{sec}s"
        if sec <= 0 or price is None:
            continue
        window_ms = sec * 1000
        # warmup: skip the window until >=60% of real data is accumulated
        if now_ms - warm_anchor < window_ms * 0.6:
            continue
        i = bisect_left(ts_list, ts_now - window_ms)
        seg = pts[i:]
        if len(seg) < 2:
            continue
        base = seg[0][1]
        if not base or base <= 0:
            continue
        hi_val, hi_ts = seg[0][1], seg[0][0]
        lo_val, lo_ts = seg[0][1], seg[0][0]
        for t, v in seg:
            if v > hi_val:
                hi_val, hi_ts = v, t
            if v < lo_val:
                lo_val, lo_ts = v, t
        net = _pct(price, base)
        up_pct = _pct(hi_val, base)
        down_pct = _pct(lo_val, base)
        fresh_ms = max(3 * 15000, window_ms // 5)  # ~peak freshness
        peak_fresh = hi_ts >= ts_now - fresh_ms
        trough_fresh = lo_ts >= ts_now - fresh_ms
        if net >= thr:
            candidates.append(("up", name, thr, net, "trend", ts_now))
        elif peak_fresh and up_pct >= thr:
            candidates.append(("up", name, thr, up_pct, "spike", hi_ts))
        if net <= -thr:
            candidates.append(("down", name, thr, net, "trend", ts_now))
        elif trough_fresh and down_pct <= -thr:
            candidates.append(("down", name, thr, down_pct, "spike", lo_ts))

    if not candidates:
        return []

    order = {"1m": 0, "5m": 1, "15m": 2, "1h": 3, "6h": 4, "24h": 5}

    def strength(c):
        return (abs(c[3]) / c[2], -order.get(c[1], 9))

    def passes(cand, dir_state):
        """Anti-spam: escalation and spike deduplication within a direction."""
        direction, _, thr, pct, reason, trig_ts = cand
        last_ts = dir_state.get("last_ts") or 0
        last_pct = dir_state.get("last_pct")
        # new "episode": silence > 15 minutes resets escalation requirements
        if ts_now - last_ts > 15 * 60 * 1000:
            last_pct = None
        if last_pct is not None and abs(pct) < abs(last_pct) * 1.25:
            return False  # repeat without a meaningful improvement of the move
        if reason == "spike":
            # the very same peak/trough must not fire again
            if trig_ts <= (dir_state.get("spike_ts") or 0):
                return False
        return True

    fires = []
    for direction in ("up", "down"):
        st = det.setdefault("dir_" + direction, {})
        d = [c for c in candidates if c[0] == direction]
        if not d:
            continue
        d.sort(key=strength, reverse=True)
        best = next((c for c in d if passes(c, st)), None)
        if best is None:
            continue
        last = up_at if direction == "up" else down_at
        if ts_now - last < cooldown_ms:
            continue
        if direction == "up":
            det["last_alert_up"] = ts_now
        else:
            det["last_alert_down"] = ts_now
        st["last_ts"] = ts_now
        st["last_pct"] = best[3]
        if best[4] == "spike":
            st["spike_ts"] = best[5]
        fires.append({
            "direction": direction,
            "window": best[1],
            "threshold": best[2],
            "pct": round(best[3], 1),
            "reason": best[4],          # trend | spike
            "trigger_ts": best[5],
            "price": price,
            "ts": ts_now,
        })
    return fires
