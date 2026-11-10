#!/usr/bin/env python3
"""음원 없이 numpy 만으로 배경음악·효과음을 합성한다(ElevenLabs Music 을 못 쓸 때의 대체 경로). 같은 입력이면 항상 같은 소리.

사용법
  synth_music.py music spec.json out.wav
  synth_music.py sfx <outdir> [--names impact,whoosh,riser]     효과음 키트(eleven.py sfx-kit 와 같은 이름)

spec.json
{
  "duration": 60, "mood": "cinematic",          ← cinematic(웅장·기본) | pulse(테크·일렉) | soft(감성·미니멀) | playful(경쾌)
  "bpm": 90, "key": "D", "minor": true,          ← 생략하면 무드 기본값
  "progression": ["i", "VI", "III", "VII"],       ← 로마 숫자(마이너/메이저 자동) 또는 ["Dm","Bb","F","C"]
  "sections": [{"start": 0, "energy": 0.2}, {"start": 8, "energy": 0.6}, {"start": 20, "energy": 0.9}, {"start": 52, "energy": 0.25}],
  "events": [{"t": 8.0, "type": "hit"}, {"t": 19.6, "type": "riser", "dur": 2.0}, {"t": 30, "type": "stop", "dur": 0.8},
             {"t": 57, "type": "chime"}, {"t": 44, "type": "boom"}, {"t": 12, "type": "braam"}],
  "fade_in": 0.5, "fade_out": 3.0
}
energy(0~1)에 따라 층이 쌓인다 — cinematic: 드론 → 패드(0.15) → 현악 오스티나토(0.35) → 하프타임 타이코(0.45)
→ 브라스 스탭(0.65) → 8분 타이코·심벌(0.75) → 고음 아르페지오(0.85). stop 은 음악만 잠깐 끊는다(정적 연출).
컷을 박자에 맞추려면 장면 경계를 60/bpm 의 배수에 두거나, 만든 뒤 music_tools.py analyze 로 히트를 읽는다.
"""
import _utf8  # noqa: F401  (Windows 에서 UTF-8 모드로 다시 실행)
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

SR = 48000
NOTE = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6, "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11}
ROMAN = {"i": 0, "ii": 2, "iii": 4, "iv": 5, "v": 7, "vi": 9, "vii": 11}
MOODS = {
    "cinematic": {"bpm": 90, "key": "D", "minor": True, "progression": ["i", "VI", "III", "VII"]},
    "pulse": {"bpm": 120, "key": "A", "minor": True, "progression": ["i", "VI", "III", "VII"]},
    "soft": {"bpm": 76, "key": "C", "minor": False, "progression": ["I", "V", "vi", "IV"]},
    "playful": {"bpm": 118, "key": "C", "minor": False, "progression": ["I", "IV", "V", "I"]},
}


def rng(seed):
    return np.random.default_rng(seed)


def tt(dur):
    return np.arange(int(dur * SR)) / SR


def mtof(m):
    return 440.0 * 2 ** ((m - 69) / 12)


# ── FFT 필터(scipy 없이) ──
def fft_filter(x, lo=None, hi=None):
    """2차 버터워스와 비슷한 크기 응답을 FFT 로 곱한다(위상 왜곡 없음, scipy 불필요)."""
    n = len(x)
    if n < 8:
        return x
    X = np.fft.rfft(x)
    f = np.maximum(np.fft.rfftfreq(n, 1 / SR), 1e-3)
    g = np.ones_like(f)
    if lo:
        g /= np.sqrt(1 + (lo / f) ** 4)
    if hi:
        g /= np.sqrt(1 + (f / hi) ** 4)
    return np.fft.irfft(X * g, n)


def lp(x, f):
    return fft_filter(x, hi=f)


def hp(x, f):
    return fft_filter(x, lo=f)


def bp(x, lo, hi):
    return fft_filter(x, lo=lo, hi=hi)


def sweep(x, centers, q=1.2, block=2048):
    """시간에 따라 중심 주파수가 움직이는 밴드패스(오버랩-애드 STFT)."""
    hop = block // 2
    win = np.hanning(block)
    out = np.zeros(len(x) + block)
    xp = np.concatenate([x, np.zeros(block)])
    f = np.fft.rfftfreq(block, 1 / SR)
    for i in range(0, len(x), hop):
        c = float(centers[min(i, len(centers) - 1)])
        g = np.exp(-0.5 * (np.log2(np.maximum(f, 1) / c) * q * 2) ** 2)
        seg = np.fft.irfft(np.fft.rfft(xp[i:i + block] * win) * g, block)
        out[i:i + block] += seg
    return out[:len(x)]


def saw(freq, t, harmonics=14):
    out = np.zeros_like(t)
    for k in range(1, harmonics + 1):
        if freq * k > SR / 2 - 2000:
            break
        out += np.sin(2 * np.pi * freq * k * t) / k
    return out


def env_adsr(n, a=0.01, d=0.1, s=0.7, r=0.2):
    t = np.arange(n) / SR
    dur = n / SR
    e = np.where(t < a, t / max(a, 1e-4), np.where(t < a + d, 1 - (1 - s) * (t - a) / max(d, 1e-4), s))
    rel = np.clip((dur - t) / max(r, 1e-4), 0, 1)
    return e * rel


def place(buf, sig, t, gain=1.0):
    i = int(round(t * SR))
    if sig.ndim == 1 and buf.ndim == 2:
        sig = np.stack([sig, sig], 1)
    if i >= len(buf) or i + len(sig) <= 0:
        return
    s0 = max(0, -i)
    i = max(0, i)
    n = min(len(sig) - s0, len(buf) - i)
    buf[i:i + n] += gain * sig[s0:s0 + n]


def reverb(x, seconds=2.4, seed=7, wet=0.25, dark=5500):
    n = int(seconds * SR)
    t = np.arange(n) / SR
    out = np.zeros_like(x)
    for ch in range(x.shape[1]):
        ir = rng(seed + ch).standard_normal(n) * np.exp(-t * 6.9 / seconds)
        ir = lp(ir, dark)
        ir /= np.sqrt(np.sum(ir ** 2)) + 1e-9
        m = len(x) + n
        y = np.fft.irfft(np.fft.rfft(x[:, ch], m) * np.fft.rfft(ir, m), m)[:len(x)]
        out[:, ch] = y
    return x + wet * out


def pan(sig, p):
    l, r = np.cos((p + 1) * np.pi / 4), np.sin((p + 1) * np.pi / 4)
    return np.stack([sig * l, sig * r], 1)


# ── 악기 ──
def kick(seed=1):
    t = tt(0.5)
    f = 46 + 110 * np.exp(-t * 28)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 7)
    click = rng(seed).standard_normal(len(t)) * np.exp(-t * 900) * 0.4
    return np.tanh(1.6 * (body + click))


def taiko(seed=1, pitch=1.0):
    t = tt(1.2)
    f = (62 + 70 * np.exp(-t * 18)) * pitch
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 4.5)
    skin = bp(rng(seed).standard_normal(len(t)), 120, 1400) * np.exp(-t * 22) * 0.9
    return np.tanh(1.4 * (body + skin))


def snare(seed=2):
    t = tt(0.35)
    n = bp(rng(seed).standard_normal(len(t)), 900, 7000) * np.exp(-t * 18)
    tone = np.sin(2 * np.pi * 190 * t) * np.exp(-t * 30) * 0.5
    return (n + tone) * 0.8


def hat(seed=3, open_=False):
    t = tt(0.3 if open_ else 0.06)
    return hp(rng(seed).standard_normal(len(t)), 7000) * np.exp(-t * (14 if open_ else 90)) * 0.6


def cymbal(seed=4):
    t = tt(3.0)
    return hp(rng(seed).standard_normal(len(t)), 4500) * np.exp(-t * 1.6) * 0.5


def pad(freqs, dur, cutoff, seed=0, attack=0.6):
    t = tt(dur)
    L, R = np.zeros_like(t), np.zeros_like(t)
    for j, f in enumerate(freqs):
        for det, side in ((-0.005, 0), (0.0, 2), (0.0055, 1)):
            v = saw(f * (1 + det), t + 0.013 * j, 10)
            if side in (0, 2):
                L += v
            if side in (1, 2):
                R += v
    e = np.minimum(1, t / attack) * np.clip((dur - t) / 0.6, 0, 1)
    return np.stack([lp(L, cutoff) * e, lp(R, cutoff) * e], 1) / max(1, len(freqs))


def string_note(freq, dur, bright=2400):
    t = tt(dur)
    x = saw(freq, t, 12) + 0.5 * saw(freq * 1.003, t, 12)
    return lp(x, bright) * env_adsr(len(t), 0.008, 0.08, 0.55, 0.05) * 0.5


def brass(freqs, dur, seed=0):
    t = tt(dur)
    x = np.zeros_like(t)
    for f in freqs:
        x += saw(f, t, 16) + saw(f * 1.004, t, 16)
    cut = 500 + 2600 * np.exp(-t * 3)
    out = sweep(x, cut, q=0.6) * env_adsr(len(t), 0.03, 0.25, 0.6, 0.4)
    return np.tanh(out * 0.6)


def pluck(freq, seed=0, decay=9):
    t = tt(0.6)
    return lp(saw(freq, t, 12), 3600) * np.exp(-t * decay)


def piano(freq, dur=2.5):
    t = tt(dur)
    x = sum(np.sin(2 * np.pi * freq * k * t) * (0.6 ** (k - 1)) * np.exp(-t * (1.6 + k * 0.7)) for k in range(1, 7))
    return x * np.minimum(1, t / 0.004)


def bell(freq, dur=3.0):
    t = tt(dur)
    mod = np.sin(2 * np.pi * freq * 3.5 * t) * 2.2 * np.exp(-t * 2)
    return np.sin(2 * np.pi * freq * t + mod) * np.exp(-t * 1.3) * np.minimum(1, t / 0.003)


def drone(root, dur, seed=0):
    t = tt(dur)
    x = 0.6 * np.sin(2 * np.pi * root * t) + 0.3 * np.sin(2 * np.pi * root * 1.5 * t + 0.4) + 0.25 * lp(saw(root, t, 8), 500)
    x *= 0.85 + 0.15 * np.sin(2 * np.pi * 0.11 * t + seed)
    return x * np.minimum(1, t / 2.0)


# ── 효과음 ──
def fx_boom(seed=0):
    t = tt(3.0)
    b = np.sin(2 * np.pi * np.cumsum(30 + 38 * np.exp(-t * 4)) / SR) * np.exp(-t * 1.6)
    burst = lp(rng(seed).standard_normal(len(t)), 1800) * np.exp(-t * 10) * 0.6
    return np.tanh(1.5 * (b + burst))


def fx_impact(seed=0):
    s = taiko(seed) * 0.9 + fx_boom(seed + 1)[:int(1.2 * SR)] * 0.8
    t = tt(len(s) / SR)
    s += hp(rng(seed + 2).standard_normal(len(t)), 3500) * np.exp(-t * 3) * 0.25
    return np.tanh(s)


def fx_braam(root=36.71, dur=4.0, seed=0):
    return brass([root, root * 2, root * 3], dur, seed) * 1.3


def fx_riser(dur=3.0, seed=0):
    t = tt(dur)
    p = t / dur
    n = rng(seed).standard_normal(len(t))
    x = sweep(n, 300 * (9000 / 300) ** p, q=0.9) * 1.2 + 0.3 * np.sin(2 * np.pi * np.cumsum(120 + 900 * p ** 2) / SR)
    return x * p ** 2.0


def fx_whoosh(dur=1.0, seed=0, deep=False):
    t = tt(dur)
    p = t / dur
    n = rng(seed).standard_normal(len(t))
    c = (150 if deep else 400) + (900 if deep else 3200) * np.sin(np.pi * p) ** 2
    return sweep(n, c, q=0.8) * np.sin(np.pi * p) ** 2 * 1.6


def fx_reverse(dur=2.0, seed=0):
    s = fx_impact(seed)
    s = s[:int(dur * SR)][::-1]
    return s * np.linspace(0, 1, len(s)) ** 2


def fx_glitch(seed=0):
    r = rng(seed)
    t = tt(0.6)
    x = np.zeros_like(t)
    i = 0
    while i < len(t):
        n = int(r.integers(400, 3000))
        x[i:i + n] = np.sign(np.sin(2 * np.pi * r.uniform(80, 2000) * t[i:i + n])) * r.uniform(0.2, 0.8) if r.random() > 0.3 else 0
        i += n + int(r.integers(0, 800))
    return np.round(x * 6) / 6 * 0.7


def fx_shimmer(dur=3.0, seed=0):
    r = rng(seed)
    t = tt(dur)
    x = np.zeros_like(t)
    for k in range(40):
        f = r.uniform(2000, 9000)
        on = r.uniform(0, dur * 0.7)
        x += np.sin(2 * np.pi * f * t) * np.exp(-np.maximum(0, t - on) * 5) * (t > on) * 0.08
    return x * np.sin(np.pi * t / dur)


def fx_tick(seed=0, freq=3200):
    t = tt(0.03)
    return (np.sin(2 * np.pi * freq * t) * 0.6 + hp(rng(seed).standard_normal(len(t)), 4000) * 0.4) * np.exp(-t * 260)


def fx_typing(dur=2.0, seed=0):
    r = rng(seed)
    out = np.zeros(int(dur * SR))
    t = 0.0
    while t < dur - 0.05:
        place(out, fx_tick(int(t * 1000), 2400 + r.uniform(-300, 600)) * 0.7, t)
        t += r.uniform(0.05, 0.11)
    return out


def fx_heartbeat(dur=3.0):
    out = np.zeros(int(dur * SR))
    for k in np.arange(0, dur - 0.4, 0.9):
        for d, g in ((0, 1.0), (0.27, 0.7)):
            t = tt(0.3)
            place(out, np.sin(2 * np.pi * (58 + 20 * np.exp(-t * 20)) * t) * np.exp(-t * 14) * g, k + d)
    return out


def fx_chime(freq=1174.7):
    return seq((bell(freq, 3.0) * 0.8, 0), (bell(freq * 2, 2.0) * 0.25, 0))


def seq(*parts):
    """(신호, 시작초) 들을 이어 붙인 한 신호."""
    n = max(int(t * SR) + len(x) for x, t in parts)
    out = np.zeros(n)
    for x, t in parts:
        place(out, x, t)
    return out


SFX = {
    "sub_boom": lambda: fx_boom(1),
    "impact": lambda: fx_impact(2),
    "braam": lambda: fx_braam(),
    "logo_hit": lambda: seq((fx_impact(5), 0), (fx_braam(dur=3.0) * 0.5, 0.02), (fx_shimmer(2.5, 5) * 0.6, 0.1)),
    "final_hit": lambda: seq((fx_impact(9), 0), (fx_boom(10) * 0.7, 0), (np.zeros(int(1.0 * SR)), 3.0)),
    "riser": lambda: fx_riser(4.0, 3),
    "reverse_swell": lambda: fx_reverse(2.0, 4),
    "whoosh": lambda: fx_whoosh(1.0, 5),
    "whoosh_deep": lambda: fx_whoosh(1.5, 6, deep=True),
    "glitch": lambda: fx_glitch(7),
    "shimmer": lambda: fx_shimmer(3.0, 8),
    "ui_blip": lambda: bell(2400, 0.25) * 0.5,
    "ui_ticks": lambda: fx_typing(2.0, 9) * 0.6,
    "typing": lambda: fx_typing(2.0, 10),
    "stamp": lambda: seq((taiko(11, 2.2)[: int(0.4 * SR)], 0), (bell(1568, 0.6) * 0.3, 0.25)),
    "shutter": lambda: seq((fx_tick(12, 1800) * 2, 0), (fx_tick(13, 1200) * 2, 0.06)),
    "counter": lambda: fx_typing(2.0, 14) * 0.8,
    "heartbeat": lambda: fx_heartbeat(3.0),
    "clock_tick": lambda: seq(*[(fx_tick(k, 2600), k * 0.5) for k in range(8)]),
    "pop": lambda: np.sin(2 * np.pi * np.cumsum(400 + 900 * np.exp(-tt(0.15) * 40)) / SR) * np.exp(-tt(0.15) * 30),
    "ding": lambda: bell(1760, 1.5),
    "chime": lambda: fx_chime(),
    "drone": lambda: drone(36.71, 15.0),
}


def parse_chord(sym, key, minor):
    """로마 숫자 또는 코드명 → (근음 MIDI, 음정 리스트)."""
    base = 48 + NOTE[key]
    if sym.lower().rstrip("°+") in ROMAN or sym.lower() in ROMAN:
        low = sym.lower()
        deg = ROMAN[low]
        if minor:   # 자연 단음계 기준 위치
            deg = {"i": 0, "ii": 2, "iii": 3, "iv": 5, "v": 7, "vi": 8, "vii": 10}[low]
        root = base + deg
        is_minor = sym == low
        return root, [0, 3, 7] if is_minor else [0, 4, 7]
    r = sym[0] + (sym[1] if len(sym) > 1 and sym[1] in "#b" else "")
    rest = sym[len(r):]
    root = 48 + NOTE[r]
    return root, [0, 3, 7] if rest.startswith("m") and not rest.startswith("maj") else [0, 4, 7]


def build(c):
    mood = c.get("mood", "cinematic")
    d = {**MOODS.get(mood, MOODS["cinematic"]), **{k: v for k, v in c.items() if k in ("bpm", "key", "minor", "progression")}}
    dur = float(c["duration"])
    bpm = float(d["bpm"])
    beat = 60 / bpm
    N = int((dur + 4) * SR)
    secs = sorted(c.get("sections", [{"start": 0, "energy": 0.6}]), key=lambda s: s["start"])

    def energy(t):
        e = 0.0
        for s in secs:
            if t >= s["start"]:
                e = float(s["energy"])
        return e

    bus = {k: np.zeros((N, 2)) for k in ("drums", "low", "pad", "mid", "high", "fx")}
    duck = np.ones(N)
    k_smp = kick() if mood in ("pulse", "playful") else taiko(1)
    nbeats = int(dur / beat) + 2
    chords = [parse_chord(s, d["key"], d["minor"]) for s in d["progression"]]
    root0 = chords[0][0]
    if mood in ("cinematic", "soft"):
        place(bus["low"], drone(mtof(root0 - 12), dur + 2), 0, 0.35 if mood == "cinematic" else 0.18)
    for b in range(nbeats):
        t0 = b * beat
        e = energy(t0)
        bar, pos = divmod(b, 4)
        root, iv = chords[bar % len(chords)]
        if pos == 0 and e >= 0.12:
            freqs = [mtof(root + 12 + i) for i in iv] + ([mtof(root + 24 + iv[0])] if e > 0.6 else [])
            cut = 500 + 3200 * e if mood != "soft" else 1400
            place(bus["pad"], pad(freqs, beat * 4 + 0.8, cut, b), t0, 0.5)
            if mood == "soft":
                for j, i in enumerate(iv):
                    place(bus["mid"], pan(piano(mtof(root + 12 + i)), -0.3 + 0.3 * j), t0 + j * beat * 0.5, 0.18)
        if mood == "cinematic":
            if e >= 0.35:   # 현악 8분 오스티나토
                for h in range(2):
                    note = root + (0 if (b * 2 + h) % 4 else 12)
                    place(bus["mid"], pan(string_note(mtof(note), beat / 2 * 0.95, 1200 + 2000 * e), -0.2 if h else 0.2), t0 + h * beat / 2, 0.22 * e)
            if e >= 0.45 and pos in (0, 2):
                place(bus["drums"], taiko(100 + b), t0, 0.9)
                place(duck, -0.35 * np.exp(-tt(beat) * 8), t0)
            if e >= 0.75:
                place(bus["drums"], taiko(300 + b, 1.35), t0 + beat / 2, 0.45)
                if pos == 0 and bar % 2 == 0:
                    place(bus["high"], pan(cymbal(400 + b), 0.1), t0, 0.35)
            if e >= 0.65 and pos == 0 and bar % 2 == 0:
                place(bus["low"], brass([mtof(root - 12 + i) for i in iv[:2]], beat * 1.5, b), t0, 0.35)
            if e >= 0.85:
                notes = [root + 24 + i for i in iv]
                for s in range(4):
                    place(bus["high"], pan(pluck(mtof(notes[(b * 4 + s) % 3])), 0.5 * (-1) ** s), t0 + s * beat / 4, 0.12)
        elif mood in ("pulse", "playful"):
            if e >= 0.3 and (e >= 0.5 or pos in (0, 2)):
                place(bus["drums"], k_smp, t0, 0.9)
                place(duck, -0.5 * np.exp(-tt(beat) * 9), t0)
            if e >= 0.5 and pos in (1, 3):
                place(bus["drums"], snare(200 + b), t0, 0.45)
            if e >= 0.4:
                place(bus["high"], pan(hat(300 + b, open_=e >= 0.75), 0.3), t0 + beat / 2, 0.5)
            if e >= 0.25:
                for h in range(2):
                    place(bus["low"], lp(saw(mtof(root - 12), tt(beat / 2 * 0.9), 8), 500) * env_adsr(int(beat / 2 * 0.9 * SR), 0.004, 0.05, 0.7, 0.03), t0 + h * beat / 2, 0.45)
            if e >= 0.6:
                notes = [root + 12 + i for i in iv] + [root + 24]
                for s in range(4):
                    place(bus["mid"], pan(pluck(mtof(notes[(b * 4 + s) % 4]), decay=12 if mood == "pulse" else 7), 0.4 * (-1) ** s), t0 + s * beat / 4, 0.16)
        elif mood == "soft":
            if e >= 0.5 and pos == 0:
                place(bus["drums"], kick(500 + b) * 0.5, t0, 0.4)
            if e >= 0.4 and pos in (1, 3):
                place(bus["high"], pan(pluck(mtof(root + 24 + iv[pos % 3]), decay=5), 0.3), t0, 0.1)

    # 이벤트
    for i, ev in enumerate(c.get("events", [])):
        t, typ, g = float(ev["t"]), ev["type"], float(ev.get("gain", 1.0))
        if typ == "hit":
            place(bus["fx"], fx_impact(1500 + i), t, 0.9 * g)
        elif typ == "boom":
            place(bus["fx"], fx_boom(1600 + i), t, 0.9 * g)
        elif typ == "braam":
            place(bus["fx"], fx_braam(mtof(root0 - 24), float(ev.get("dur", 3.5)), i), t, 0.7 * g)
        elif typ == "riser":
            dd = float(ev.get("dur", 2.0))
            place(bus["fx"], fx_riser(dd, 900 + i), t - dd, 0.45 * g)
        elif typ == "whoosh":
            dd = float(ev.get("dur", 0.8))
            place(bus["fx"], fx_whoosh(dd, 1200 + i), t - dd / 2, 0.5 * g)
        elif typ == "chime":
            place(bus["fx"], fx_chime(mtof(root0 + 24)), t, 0.4 * g)
        elif typ == "stop":
            a0, a1 = int(t * SR), int((t + float(ev.get("dur", 0.6))) * SR)
            ramp = int(0.015 * SR)
            gate = np.ones(N)
            gate[a0:a1] = 0
            gate[max(0, a0 - ramp):a0] = np.linspace(1, 0, min(ramp, a0))
            gate[a1:a1 + ramp] = np.linspace(0, 1, len(gate[a1:a1 + ramp]))
            for k in ("drums", "low", "pad", "mid", "high"):
                bus[k] *= gate[:, None]

    sc = np.clip(duck, 0.45, 1)[:, None]
    mix = (bus["drums"] * 0.8 + bus["low"] * 0.9 * sc + bus["pad"] * 0.5 * sc + bus["mid"] * 0.8 * sc + bus["high"] * 0.6 + bus["fx"])
    send = bus["pad"] * 0.4 + bus["mid"] * 0.5 + bus["high"] * 0.6 + bus["fx"] * 0.35 + bus["drums"] * 0.15
    mix = mix + reverb(send, 2.8 if mood in ("cinematic", "soft") else 1.6, wet=1.0)[:, :] * 0.22 - send * 0.22
    for ch in range(2):   # 마스터 EQ: 30Hz 하이패스 + 저역 -3dB(120Hz 아래) + 고역 +5dB(5kHz 위) — 합성음은 저역이 무겁고 고역이 어둡다
        X = np.fft.rfft(mix[:, ch])
        f = np.maximum(np.fft.rfftfreq(len(mix), 1 / SR), 1e-3)
        g_db = -3.0 / (1 + (f / 120) ** 2) + 5.0 / (1 + (5000 / f) ** 2)
        g = 10 ** (g_db / 20) / np.sqrt(1 + (30 / f) ** 4)
        mix[:, ch] = np.fft.irfft(X * g, len(mix))
    mix = mix[: int(dur * SR)]
    mix = np.tanh(mix * 1.1)
    fi, fo = int(float(c.get("fade_in", 0.3)) * SR), int(float(c.get("fade_out", 2.5)) * SR)
    if fi:
        mix[:fi] *= np.linspace(0, 1, fi)[:, None]
    if fo:
        mix[-fo:] *= (np.linspace(1, 0, fo) ** 1.5)[:, None]
    return mix


def write(path, x, target_lufs=-16.0):
    """float 스테레오 → ffmpeg 로 2패스 없이 측정 후 정적 게인 + 리미터를 걸어 WAV 저장(소스 레벨 -16 LUFS)."""
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 1:
        x = np.stack([x, x], 1)
    x = x / (np.abs(x).max() + 1e-9) * 0.7
    raw = x.astype(np.float32).tobytes()
    m = subprocess.run(["ffmpeg", "-hide_banner", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "-", "-af", "ebur128", "-f", "null", "-"],
                       input=raw, capture_output=True).stderr.decode()
    try:
        I = float(m.rsplit("I:", 1)[1].split("LUFS")[0])
        gain = target_lufs - I
    except Exception:
        gain = 0.0
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "-",
                    "-af", f"volume={gain:.2f}dB,alimiter=limit=0.89:attack=3:release=60:level=disabled", "-c:a", "pcm_s16le", str(path)],
                   input=raw, check=True)


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("music", "sfx"):
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "music":
        spec = json.loads(Path(sys.argv[2]).read_text())
        out = sys.argv[3]
        x = build(spec)
        write(out, x)
        print(f"{out} {len(x) / SR:.1f}s mood={spec.get('mood', 'cinematic')}")
    else:
        outdir = Path(sys.argv[2])
        names = list(SFX)
        if "--names" in sys.argv:
            names = sys.argv[sys.argv.index("--names") + 1].split(",")
        for n in names:
            if n not in SFX:
                print(f"{n}: 합성 프리셋 없음(가능: {', '.join(SFX)})")
                continue
            f = outdir / f"{n}.wav"
            write(f, SFX[n](), target_lufs=-18.0)
            print(f)


if __name__ == "__main__":
    main()
