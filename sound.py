# -*- coding: utf-8 -*-
"""
Synth for audio alerts (soft electric piano / marimba) and playback
through the standard Windows audio output. winsound.Beep is not used —
it is nearly inaudible (system PC speaker).

The timbre is built DETERMINISTICALLY (no random jitter — that very jitter
is what produced the hiss): pure sine harmonics with decaying envelopes, light
piano "damping" of the 2nd harmonic, a soft limiter (tanh) instead of
clipping. 44.1 kHz — highs are not cut.

Volume is "baked into" the WAV file name (alert_up_70.wav): on volume
change the files are regenerated.
"""

import math
import os
import struct
import threading
import wave

SR = 44100
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# notes (frequencies in Hz)
NOTE = {
    "C5": 523.25, "E5": 659.25, "G5": 783.99, "C6": 1046.5,
    "G4": 392.00, "E4": 329.63, "C4": 261.63, "C7": 2093.0,
}

# ascending major arpeggio (pump) / descending (dump) / short blip
SOUNDS = {
    "up":   [("C5", 0.00, 0.55), ("E5", 0.09, 0.55), ("G5", 0.18, 0.60), ("C6", 0.27, 1.10)],
    "down": [("G5", 0.00, 0.55), ("E5", 0.10, 0.55), ("C5", 0.20, 0.65), ("C4", 0.30, 1.20)],
    "blip": [("C6", 0.00, 0.20), ("E5", 0.08, 0.26)],
}

_lock = threading.Lock()


def _bell(freq, dur, amp):
    """One note: sine + harmonics with individual decay.

    Only odd/lower harmonics decay slowly — the sound is warm,
    "wooden" (marimba/Fender Rhodes), without high-frequency grit.
    """
    n = int(SR * dur)
    out = [0.0] * n
    tau = dur / 3.0                      # fundamental lifetime
    harmonics = (
        (1, 1.0000, 1.00),               # f      — body of the sound
        (2, 0.4200, 0.62),               # 2f     — brightness
        (3, 0.1500, 0.38),               # 3f     — bell-like attack
        (4, 0.0450, 0.25),               # 4f     — dies out very fast
        (5, 0.0150, 0.18),
    )
    for h, amp_h, dec_h in harmonics:
        f = freq * h
        # slight natural inharmonicity (deterministic, not noise!)
        f *= 1.0 + 0.0008 * (h - 1)
        w = 2.0 * math.pi * f / SR
        tau_h = tau * dec_h
        step = w
        ang = 0.0
        for i in range(n):
            t = i / SR
            env = math.exp(-t / tau_h)
            out[i] += math.sin(ang) * amp_h * env
            ang += step
            if ang > 6.283185307:
                ang -= 6.283185307
    # attack envelope: 6 ms cosine ramp — removes the start click
    atk = max(2, int(SR * 0.006))
    for i in range(min(atk, n)):
        out[i] *= 0.5 - 0.5 * math.cos(math.pi * i / atk)
    # cosine fade of the last 40 ms — removes the end click
    fd = min(n, int(SR * 0.04))
    for i in range(fd):
        t = i / fd
        out[n - fd + i] *= 0.5 + 0.5 * math.cos(math.pi * t)
    return [v * amp for v in out]


def _render_track(notes, volume01):
    total = max(s + d for _, s, d in notes) + 0.12
    n = int(SR * total)
    buf = [0.0] * n
    for name, start, dur in notes:
        off = int(start * SR)
        for i, v in enumerate(_bell(NOTE[name], dur, 1.0)):
            if off + i < n:
                buf[off + i] += v
    # mix: soft tanh limiter instead of hard clipping + volume
    # (drive 2.6 + peak-factor compensation for the bell timbre — otherwise
    # a clean unclipped signal feels noticeably quieter than the old one)
    peak = max(abs(min(buf)), max(buf)) or 1.0
    g = volume01 * 2.4
    return [int(32767.0 * math.tanh(g * v / peak)) for v in buf]


def ensure_sounds(volume):
    """Generates WAV files of all sounds at the given volume (0..100)."""
    vol = max(0.05, min(1.0, volume / 100.0))
    os.makedirs(DATA_DIR, exist_ok=True)
    tag = int(volume)
    paths = {}
    for key, notes in SOUNDS.items():
        p = os.path.join(DATA_DIR, f"alert_{key}_{tag}.wav")
        paths[key] = p
        if os.path.exists(p):
            continue
        samples = _render_track(notes, vol)
        with wave.open(p + ".tmp", "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SR)
            wf.writeframes(b"".join(struct.pack("<h", s) for s in samples))
        os.replace(p + ".tmp", p)
    # clean up files at the old volume and older synth versions
    for f in os.listdir(DATA_DIR):
        if f.startswith("alert_") and f.endswith(".wav") and f"_{tag}.wav" not in f:
            try:
                os.remove(os.path.join(DATA_DIR, f))
            except OSError:
                pass
    return paths


def play(kind, volume=70):
    """Non-blocking sound playback: up | down | blip."""
    def run():
        try:
            with _lock:  # generation and playback run sequentially
                paths = ensure_sounds(volume)
                p = paths.get(kind)
                if p:
                    winsound_play(p)
        except Exception as e:
            print(f"[sound] error: {e}")
    threading.Thread(target=run, daemon=True).start()


def winsound_play(path):
    import winsound
    winsound.PlaySound(path, winsound.SND_FILENAME)
