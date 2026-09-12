# -*- coding: utf-8 -*-
"""Unit tests for the adaptive layer (detector.adaptive_windows / estimate_vol)."""

import math
import detector

TICK = 15_000
WINDOWS = [
    {"name": "1m", "seconds": 60, "threshold": 8},
    {"name": "5m", "seconds": 300, "threshold": 20},
    {"name": "15m", "seconds": 900, "threshold": 40},
    {"name": "24h", "seconds": 86400, "threshold": 150},
]
A_ON = {"enabled": True, "k": 4.0, "lookbackMin": 30}


def path(n, sigma, seed=3, start=1_700_000_000_000):
    import random
    rng = random.Random(seed)
    pts, p = [], 1.0
    for i in range(n):
        p *= math.exp(rng.gauss(0, sigma))
        pts.append([start + i * TICK, p])
    return pts


def thr(res, name):
    return next(w["threshold"] for w in res if w["name"] == name)


def test_passthrough_off():
    res = detector.adaptive_windows(WINDOWS, path(120, 0.01), {"enabled": False})
    assert [w["threshold"] for w in res] == [8, 20, 40, 150], res
    print("PASS выключенный адаптив = статические пороги")


def test_calm_hits_floor():
    """Calm token: thresholds fall to the floors, they don't fly off to 0."""
    res = detector.adaptive_windows(WINDOWS, path(120, 0.0002), A_ON)
    assert thr(res, "1m") == detector.DEFAULT_FLOORS["1m"], res
    assert thr(res, "5m") == detector.DEFAULT_FLOORS["5m"], res
    assert thr(res, "1m") < 8, "нужно ниже базового 8% — чувствительнее"
    print("PASS спокойный токен → полы (1м≥4% вместо 8%)")


def test_noisy_goes_above_base():
    """Noisy token: thresholds rise above base → less spam."""
    res = detector.adaptive_windows(WINDOWS, path(120, 0.02), A_ON)
    assert thr(res, "1m") > 8, res
    assert thr(res, "15m") > 40, res
    print("PASS шумный токен → пороги выше базы")


def test_24h_untouched():
    """A window with no floor in the table stays static."""
    res = detector.adaptive_windows(WINDOWS, path(120, 0.02), A_ON)
    assert thr(res, "24h") == 150, res
    print("PASS окно 24h не трогается адаптивом")


def test_short_history_fallback():
    """Little data (new token) → fully static, we don't crash."""
    res = detector.adaptive_windows(WINDOWS, path(5, 0.02), A_ON)
    assert [w["threshold"] for w in res] == [8, 20, 40, 150], res
    print("PASS короткий ряд → фолбэк на статику (прогрев)")


def test_gapped_seed_history():
    """API seed (points hours apart) + rare polling: gaps are filtered out,
    threshold stays within sane bounds (floor ≤ thr ≤ 3×base)."""
    pts = [[1_700_000_000_000 + i * 3_600_000, 1.0 + i * 0.002] for i in range(6)]
    last_t, last_p = pts[-1]
    for i in range(1, 61):                       # 60 real ticks of 15s each
        pts.append([last_t + i * TICK, last_p * math.exp(0.0003 * i)])
    res = detector.adaptive_windows(WINDOWS, pts, A_ON)
    t1 = thr(res, "1m")
    assert detector.DEFAULT_FLOORS["1m"] <= t1 <= 24, f"порог вне разумных рамок: {t1}"
    print(f"PASS посев+дырки не ломают оценку (1м порог = {t1}%)")


def test_evaluate_accepts_adaptive_windows():
    """evaluate() with adaptive windows catches a pump earlier than with static
    windows on a calm token (+10% pump is below the static 20% threshold in the 5m window)."""
    base = path(200, 0.0005, seed=9)
    t0 = base[-1][0]
    pump = [[t0 + (i + 1) * TICK, base[-1][1] * (1 + 0.12 * (i + 1) / 20.0)] for i in range(20)]
    pts = base + pump
    now = pts[-1][0]

    def run(win):
        tok = {"history": pts, "det": {}, "added_ts": pts[0][0]}
        return detector.evaluate(tok, now, win, 180)

    ad = run(detector.adaptive_windows(WINDOWS, pts, A_ON))
    st = run(WINDOWS)
    assert ad, "адаптив не поймал +12% на спокойном токене"
    assert not st, "статика не должна была поймать +12% (порог 5м=20%)"
    print(f"PASS адаптив ловит +12% на спокойном токене (статика — нет)")


if __name__ == "__main__":
    test_passthrough_off()
    test_calm_hits_floor()
    test_noisy_goes_above_base()
    test_24h_untouched()
    test_short_history_fallback()
    test_gapped_seed_history()
    test_evaluate_accepts_adaptive_windows()
    print("\nВСЕ ЮНИТ-ТЕСТЫ АДАПТИВА ПРОЙДЕНЫ")
