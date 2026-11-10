#!/usr/bin/env python3
"""timeline.json 을 읽어 나레이션·음악·효과음을 믹스하고 -14 LUFS / -2.0 dBTP 로 마스터링한다.

사용법
  mix.py <project>/timeline.json [--out audio/mix.wav] [--music-db -8] [--duck 0.75] [--sfx-duck 0.45]
         [--vo-db 0] [--hall 0] [--target -14] [--mute music,sfx]

처리 순서
  1) 나레이션: 줄마다 EQ·컴프레서(말소리 또렷하게) → 타임라인 위치(clip_start)에 배치 → 선택적 홀 리버브
     (--hall = 말소리 구간의 wet/dry 비율. 기본 0 = 리버브 없음. 0.06 이면 wet -24dB)
     리버브를 쓰면 '말 끝 잔향' dB 를 보고한다. -28dB 보다 크면(✗) 에코로 들린다(이전 기본값 2.4초·0.12 가 -22dB 로 에코 지적을 받았다)
  2) 말소리 마스크(10ms, 120ms 미리 당김, 어택 60ms / 릴리즈 450ms)로 덕킹 곡선을 만든다
  3) 음악: 말소리 대역(0.7~4.5kHz)은 깊게, 나머지는 얕게 덕킹 — 웅장함은 남기고 말은 들리게
  4) 효과음: align(start/peak/end)대로 배치, 말 위에서는 --sfx-duck 만큼 낮춘다
  5) 리미터 → 2패스 loudnorm(-14 LUFS, TP -2.0). AAC 인코딩 뒤 피크가 0.5~0.9dB 오르므로 여유를 둔다
출력: mix.wav + 점검용 스템(mix_vo.wav, mix_bed.wav) + 줄별 '나레이션 대 배경' dB 표.
      9dB 이상이면 충분, 6dB 미만(✗)이면 그 줄 근처 효과음을 줄이거나 --duck 을 올리거나 그 줄의 gain_db 를 +2~4.
  --mute music,sfx 로 원인 분리(STT 가 틀리면 음악 탓인지 발음 탓인지 가린다).
"""
import _utf8  # noqa: F401  (Windows 에서 UTF-8 모드로 다시 실행)
import argparse
import json
import re
import subprocess
from pathlib import Path

import numpy as np

SR = 48000
VO_CHAIN = ("highpass=f=70,equalizer=f=220:t=q:w=1.0:g=-2,equalizer=f=3200:t=q:w=1.1:g=3,"
            "equalizer=f=9000:t=h:w=0.7:g=1.5,acompressor=threshold=-24dB:ratio=3:attack=3:release=80:makeup=4dB")


def db(x):
    return 10 ** (x / 20)


def load(path, filt=None):
    cmd = ["ffmpeg", "-v", "error", "-i", str(path)]
    if filt:
        cmd += ["-af", filt]
    cmd += ["-ac", "2", "-ar", str(SR), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, 2).copy()


def place(buf, clip, t, gain=1.0):
    s = int(round(t * SR))
    if s >= len(buf) or s + len(clip) <= 0:
        return
    c0 = max(0, -s)
    s = max(0, s)
    e = min(len(buf), s + len(clip) - c0)
    buf[s:e] += clip[c0:c0 + e - s] * gain


def band(x, lo, hi):
    X = np.fft.rfft(x, axis=0)
    f = np.maximum(np.fft.rfftfreq(len(x), 1 / SR), 1e-3)[:, None]
    g = 1 / np.sqrt(1 + (lo / f) ** 4) / np.sqrt(1 + (f / hi) ** 4)
    return np.fft.irfft(X * g, len(x), axis=0)


def hall(x, seconds=1.4, predelay=0.03, seed=3):
    """리버브 wet 신호만 돌려준다. 크기 보정은 호출하는 쪽에서 말소리 구간 기준으로 한다."""
    n = int((seconds + predelay) * SR)
    t = np.arange(n) / SR
    p0, fade = int(predelay * SR), int(0.012 * SR)
    out = np.zeros_like(x)
    rng = np.random.default_rng(seed)
    for ch in range(2):
        ir = rng.standard_normal(n) * np.exp(-np.maximum(t - predelay, 0) * 6.9 / seconds)
        ir[:p0] = 0
        ir[p0:p0 + fade] *= np.sin(np.linspace(0, np.pi / 2, fade)) ** 2   # 첫 반사를 12ms 에 걸쳐 키운다(급한 시작은 말이 두 번 들린다)
        ir = band(ir[:, None], 300, 5000)[:, 0]
        ir /= np.sqrt(np.sum(ir ** 2)) + 1e-9
        m = len(x) + n
        out[:, ch] = np.fft.irfft(np.fft.rfft(x[:, ch], m) * np.fft.rfft(ir, m), m)[: len(x)]
    return out


def speech_frames(x, floor=-40):
    """10ms 프레임 최대값이 floor dBFS 를 넘는 샘플 마스크(길이 len(x))."""
    hop = SR // 100
    m = np.abs(x).mean(axis=1)
    n = (len(m) + hop - 1) // hop
    fr = np.pad(m, (0, n * hop - len(m))).reshape(n, hop).max(axis=1)
    return np.repeat(fr > db(floor), hop)[: len(m)]


def active_rms_db(x, floor=-45):
    m = np.abs(x).mean(axis=1)
    hop = SR // 100
    fr = np.array([np.sqrt(np.mean(m[i:i + hop] ** 2)) for i in range(0, len(m) - hop, hop)]) if len(m) > hop else np.array([1e-9])
    act = fr[20 * np.log10(fr + 1e-9) > floor]
    return 20 * np.log10(np.sqrt(np.mean(act ** 2)) + 1e-9) if len(act) else -60.0


def rms_db(x):
    return 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)


def write(path, x, codec="pcm_f32le"):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "-", "-c:a", codec, str(path)],
                   input=x.astype(np.float32).tobytes(), check=True)


def loudness(path):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    summ = r[r.rfind("Summary:"):]
    I = float(re.search(r"I:\s+(-?[\d.]+) LUFS", summ).group(1))
    tp = re.search(r"Peak:\s+(-?[\d.inf]+) dBFS", summ)
    return I, float(tp.group(1)) if tp else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("timeline")
    ap.add_argument("--out")
    ap.add_argument("--music-db", type=float)
    ap.add_argument("--duck", type=float)
    ap.add_argument("--sfx-duck", type=float, default=0.45)
    ap.add_argument("--vo-db", type=float, default=0.0)
    ap.add_argument("--hall", type=float, default=0.0,
                    help="나레이션 리버브 wet/dry 비율. 기본 0(없음). 공간감이 꼭 필요할 때만 0.04~0.08, 0.1 넘게 쓰지 않는다")
    ap.add_argument("--target", type=float, default=-14.0)
    ap.add_argument("--tp", type=float, default=-2.0, help="true peak 목표. AAC 인코딩(마스터·공유용 두 번) 뒤 0.5~0.9dB 오르므로 -2.0")
    ap.add_argument("--mute", default="")
    a = ap.parse_args()

    tlp = Path(a.timeline).resolve()
    proj = tlp.parent
    tl = json.loads(tlp.read_text())
    dur = float(tl["duration"])
    N = int(dur * SR)
    mute = set(x for x in a.mute.split(",") if x)
    out = Path(a.out) if a.out else proj / "audio" / "mix.wav"
    if not out.is_absolute():
        out = proj / out
    out.parent.mkdir(parents=True, exist_ok=True)
    music_cfg = tl.get("music") or {}
    music_db = a.music_db if a.music_db is not None else float(music_cfg.get("gain_db", -8))
    duck = a.duck if a.duck is not None else float(music_cfg.get("duck", 0.75))

    # 1) 나레이션
    vo = np.zeros((N, 2), np.float32)
    for v in tl.get("vo", []):
        clip = load(proj / v["clip"], VO_CHAIN)
        clip *= db(-20 - active_rms_db(clip))   # 소스마다 음량이 제각각 → 말소리 구간 RMS 를 -20dBFS 로 맞춘다
        place(vo, clip, v["clip_start"], db(a.vo_db + float(v.get("gain_db", 0))))
    if a.hall > 0 and vo.any():
        sp = speech_frames(vo)
        wet = hall(vo)
        wet *= a.hall * np.sqrt(np.mean(vo[sp] ** 2) / (np.mean(wet[sp] ** 2) + 1e-12))
        # 쉼(다음 줄까지 0.4초 이상)마다 말 끝 0.05~0.35초의 잔향 크기를 그 줄 말소리와 비교한다
        tails = []
        lines = sorted(tl.get("vo", []), key=lambda v: v["start"])
        for i, v in enumerate(lines):
            e = v["end"]
            nxt = lines[i + 1]["start"] if i + 1 < len(lines) else dur
            if nxt - e >= 0.4 and e + 0.35 < dur:
                tails.append(rms_db(wet[int((e + 0.05) * SR):int((e + 0.35) * SR)]) - rms_db(vo[int(v["start"] * SR):int(e * SR)]))
        if tails:
            t50 = float(np.median(tails))
            mark = "✓" if t50 <= -32 else ("△" if t50 <= -28 else "✗ 에코로 들린다. --hall 을 낮추거나 0 으로")
            print(f"리버브 --hall {a.hall}: 말 위 wet {20 * np.log10(a.hall):.1f}dB · 말 끝 잔향 중앙값 {t50:.1f}dB {mark}")
        vo = vo + wet
    if "vo" in mute:
        vo[:] = 0

    # 2) 덕킹 곡선
    hop = SR // 100
    mono = np.abs(vo).mean(axis=1)
    nb = (N + hop - 1) // hop
    env = np.array([mono[i * hop:(i + 1) * hop].max() if i * hop < N else 0 for i in range(nb)])
    mask = (env > db(-42)).astype(np.float32)
    m2 = mask.copy()
    for k in range(1, 13):
        m2[:-k] = np.maximum(m2[:-k], mask[k:])
    sm = np.zeros_like(m2)
    aa, ar = 1 - np.exp(-1 / 6), 1 - np.exp(-1 / 45)
    v_ = 0.0
    for i, x in enumerate(m2):
        v_ += (x - v_) * (aa if x > v_ else ar)
        sm[i] = v_
    dc = np.repeat(sm, hop)[:N][:, None]

    # 3) 음악
    music = np.zeros((N, 2), np.float32)
    if music_cfg.get("file") and "music" not in mute and (proj / music_cfg["file"]).exists():
        m = load(proj / music_cfg["file"])
        mI, _ = loudness(proj / music_cfg["file"])
        m *= db(-16 - mI)   # 음악 소스를 -16 LUFS 로 맞춘 뒤 --music-db 를 적용
        place(music, m, float(music_cfg.get("start", 0)))
        fi = int(float(music_cfg.get("fade_in", 0.2)) * SR)
        if fi:
            music[:fi] *= np.linspace(0, 1, fi)[:, None]
        # 음량 자동화: "automation": [[시각, dB], …] — 시각은 초·큐 이름·"큐+0.5". 점 사이는 dB 로 직선 보간.
        # 생성 음악은 대개 평평하므로, 큰 히트 2~4초 앞을 -6~-10dB 로 내렸다가 히트 프레임에 0dB 로 되돌리면 히트가 커진다.
        auto = music_cfg.get("automation") or []
        if auto:
            cues = tl.get("cues", {})
            def when(x):
                if isinstance(x, (int, float)):
                    return float(x)
                m_ = re.match(r"^(.*?)([+-]\d+(?:\.\d+)?)?$", str(x).strip())
                return float(cues[m_.group(1)]) + float(m_.group(2) or 0)
            pts = sorted((when(t_), float(g_)) for t_, g_ in auto)
            tt = np.arange(N) / SR
            music *= db(np.interp(tt, [q[0] for q in pts], [q[1] for q in pts]))[:, None].astype(np.float32)
        mid = band(music, 700, 4500)
        music = ((music - mid) * (1 - duck * 0.7 * dc) + mid * (1 - min(0.95, duck * 1.15) * dc)) * db(music_db)

    # 4) 효과음
    sfx = np.zeros((N, 2), np.float32)
    cache = {}
    for e in tl.get("sfx", []):
        f = proj / e["file"]
        if "sfx" in mute or not f.exists():
            continue
        if e["file"] not in cache:
            x = load(f)
            cache[e["file"]] = x * db(-3) / (np.abs(x).max() + 1e-9)   # 피크 -3dBFS 기준. gain_db 는 여기서부터의 상대값
        x = cache[e["file"]]
        t = e["t"]
        if e.get("align") == "peak":
            t -= np.argmax(np.abs(x).sum(axis=1)) / SR
        elif e.get("align") == "end":
            t -= len(x) / SR
        place(sfx, x, t, db(float(e.get("gain_db", -6))))
    sfx = sfx * (1 - a.sfx_duck * dc)

    # 5) 합산·스템·보고
    bed = music + sfx
    tail = np.ones((N, 1), np.float32)
    tf = int(0.6 * SR)
    tail[-tf:, 0] = np.linspace(1, 0, tf)
    mix = (vo + bed) * tail
    stem = out.with_suffix("")
    write(f"{stem}_premaster.wav", mix)
    write(f"{stem}_vo.wav", vo)
    write(f"{stem}_bed.wav", bed)
    if tl.get("vo") and bed.any():
        vb, bb = band(vo, 300, 4000), band(bed, 300, 4000)
        print(f"{'id':6s} {'VO대배경':>8s}  (말소리 대역 300~4000Hz, 9dB↑ 충분 · 6dB↓ 위험)")
        for v in tl["vo"]:
            s, e = int(v["start"] * SR), int(v["end"] * SR)
            if e <= s:
                continue
            d = rms_db(vb[s:e]) - rms_db(bb[s:e])
            mark = "✓" if d >= 9 else ("△" if d >= 6 else "✗")
            print(f"{v['id']:6s} {d:7.1f}dB {mark} {v.get('sub', '')[:40]}")

    if not tl.get("vo") and tl.get("sfx") and music.any():
        # 나레이션이 없으면 효과음이 음악에 묻히지 않는지 본다(타격 직후 0.3초 RMS 비교)
        print(f"{'효과음':14s} {'시각':>6s} {'대 음악':>7s}  (+3dB↑ 들림 · 0dB↓ 묻힘)")
        for e in tl["sfx"]:
            # 끝을 맞춘(align end) 효과음은 t 에서 끝나므로 t 직전 0.3초를, 나머지는 t 부터 0.3초를 잰다
            t0 = e["t"] - 0.3 if e.get("align") == "end" else e["t"]
            s0 = int(max(0, t0) * SR)
            s1 = min(N, s0 + int(0.3 * SR))
            if s1 - s0 < 100:
                continue
            d = rms_db(sfx[s0:s1]) - rms_db(music[s0:s1])
            print(f"{e['name'][:14]:14s} {e['t']:6.2f} {d:6.1f}dB {'✓' if d >= 3 else '△' if d >= 0 else '✗'}")

    # 6) 리미터 + 2패스 loudnorm
    pre = "alimiter=limit=0.85:attack=3:release=60:level=disabled"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", f"{stem}_premaster.wav", "-af",
                        f"{pre},loudnorm=I={a.target}:TP={a.tp}:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    js = json.loads(r[r.rindex("{"): r.rindex("}") + 1])
    ln = (f"loudnorm=I={a.target}:TP={a.tp}:LRA=11:measured_I={js['input_i']}:measured_TP={js['input_tp']}:"
          f"measured_LRA={js['input_lra']}:measured_thresh={js['input_thresh']}:offset={js['target_offset']}:linear=true")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", f"{stem}_premaster.wav", "-af", f"{pre},{ln},aresample={SR}",
                    "-c:a", "pcm_s24le", str(out)], check=True)
    I, tp = loudness(out)
    print(f"마스터: {out}  I {I:.1f} LUFS · peak {tp} dBFS (목표 {a.target} / {a.tp})")
    print(f"스템: {stem}_vo.wav, {stem}_bed.wav (STT·수치 점검용)")


if __name__ == "__main__":
    main()
