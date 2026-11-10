#!/usr/bin/env python3
"""script.json + 생성된 나레이션(audio/vo) → timeline.json (믹서·QA용) + reel/timeline.js (화면용, window.TL).

사용법
  timeline.py <project>/script.json [--music-analysis audio/music/analysis.json]

나레이션 줄 배치
  "at": 12.4          말이 시작되는 시각(초)을 직접 지정. 장면 계획에 맞춰 줄을 놓을 때 쓴다.
  "at": "line.n03.end+0.6"   다른 큐 기준 상대 시각도 된다.
  (없으면)            앞 줄이 끝난 뒤 "gap"(기본 0.35초) 만큼 쉬고 시작. 첫 줄은 "first_at"(기본 0.6초).
  클립 앞뒤의 무음은 정렬 정보로 계산해서 버리지 않고 비켜 놓는다(말 시작 = at).

만들어지는 큐(화면에서 REEL.cue('이름') 로 쓴다)
  line.<id>, line.<id>.end        줄의 말 시작·끝
  <cue 이름>                       script.json lines[].cues 에 적은 부분 문자열이 발음되는 시각
  scene.<id>, scene.<id>.end       script.json scenes[] 를 적었다면
  music.hit1 …, bar1 …            --music-analysis 를 주면 음악의 강한 히트와 마디 시작(다운비트). 장면 start/end 에 "bar9" 처럼 쓴다

효과음 (script.json "sfx")
  {"name": "impact", "at": "kbs", "offset": -0.03, "gain_db": -4, "align": "peak"}
  align: start(소리 시작을 at 에) | peak(가장 큰 지점을 at 에 — 타격음) | end(끝을 at 에 — 라이저)
  큐 이름·숫자·"이름+초" 모두 된다. 파일은 audio/sfx/<name>.mp3 (또는 "file" 로 지정)

검사: 줄 겹침, 목표 길이 초과, 큐를 못 찾음, 장면 경계 침범 — 문제가 있으면 ✗ 로 표시하고 종료 코드 1.
"""
import _utf8  # noqa: F401  (Windows 에서 UTF-8 모드로 다시 실행)
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np


def envelope(path, dt, db_floor=None):
    """소리 크기 곡선(0~1). 나레이션은 줄마다 최대값으로 나눈 피크, 음악은 db_floor~0dB 를 0~1 로 바꾼 RMS.
    화면에서 REEL.level(t) 로 읽는다(파형 막대, 말할 때 빛나는 테두리, 음악 에너지에 반응하는 배경)."""
    sr = 8000
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                         capture_output=True).stdout
    x = np.frombuffer(raw, np.float32)
    hop = int(sr * dt)
    n = len(x) // hop
    if n < 2:
        return []
    blk = x[:n * hop].reshape(n, hop)
    if db_floor is None:
        e = np.abs(blk).max(axis=1)
        e = e / (e.max() + 1e-9)
    else:
        db = 20 * np.log10(np.sqrt((blk ** 2).mean(axis=1)) + 1e-9)
        e = np.clip((db - db.max() - db_floor) / -db_floor, 0, 1)
    return [round(float(v), 2) for v in e]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("script")
    ap.add_argument("--music-analysis")
    a = ap.parse_args()
    sp = Path(a.script).resolve()
    proj = sp.parent
    cfg = json.loads(sp.read_text())
    fps = cfg.get("fps", 30)
    dur = float(cfg.get("duration", 60))
    vo_dir = proj / "audio" / "vo"
    rep = json.loads((vo_dir / "report.json").read_text()) if (vo_dir / "report.json").exists() else {}
    cues, errors, warns = {}, [], []

    def resolve(x, ctx=""):
        if isinstance(x, (int, float)):
            return float(x)
        m = re.fullmatch(r"\s*([^\s+]+?)\s*(?:([+-])\s*([\d.]+))?\s*", str(x))
        if not m:
            errors.append(f"{ctx}: 시각 표현을 읽을 수 없음 '{x}'")
            return None
        name, sign, off = m.groups()
        try:
            base = float(name)
        except ValueError:
            if name not in cues:
                errors.append(f"{ctx}: 큐 '{name}' 가 없다(아직 정의 전이거나 오타)")
                return None
            base = cues[name]
        return base + (float(off) * (1 if sign == "+" else -1) if off else 0.0)

    # ── 음악 분석(선택) ── 나레이션·장면·효과음보다 먼저: 셋 모두 "bar9", "music.hit2" 큐를 쓸 수 있게
    music_tl = None
    ma = a.music_analysis or (cfg.get("music") or {}).get("analysis")
    ma_path = (Path(ma) if Path(ma).is_absolute() else proj / ma) if ma else None
    if ma_path and ma_path.exists():
        an = json.loads(ma_path.read_text())
        off = float((cfg.get("music") or {}).get("start", 0))
        hits = [round(h + off, 3) for h in an.get("hits", [])]
        for k, h in enumerate(hits):
            cues[f"music.hit{k + 1}"] = h
        for k, b in enumerate(an.get("downbeats", [])):   # 마디 큐: 장면 경계를 "bar8" 처럼 박 격자에 놓는다
            cues[f"bar{k + 1}"] = round(b + off, 3)
        music_tl = {"bpm": an.get("bpm"), "hits": hits, "beats": [round(b + off, 3) for b in an.get("beats", [])],
                    "downbeats": [round(b + off, 3) for b in an.get("downbeats", [])]}

    # ── 나레이션 배치 ──
    vo = []
    prev_end = None
    for i, l in enumerate(cfg.get("lines", [])):
        lid = l["id"]
        r = rep.get(lid)
        alf = vo_dir / f"{lid}.align.json"
        if not r or not alf.exists():
            errors.append(f"{lid}: 나레이션이 아직 없다 → narrate.py 먼저")
            continue
        al = json.loads(alf.read_text())
        s0, s1 = r["speech_start"], r["speech_end"]
        if "at" in l:
            start = resolve(l["at"], lid)
            if start is None:
                continue
        elif prev_end is None:
            start = float(cfg.get("first_at", 0.6))
        else:
            start = prev_end + float(l.get("gap", cfg.get("gap", 0.35)))
        end = start + (s1 - s0)
        clip_start = start - s0
        chars = al.get("characters") or []
        cst = al.get("character_start_times_seconds") or []
        cen = al.get("character_end_times_seconds") or []
        joined = "".join(chars)
        # 어절 단위 단어 타이밍
        words, cur, w0, w1 = [], "", None, None
        for c, a0, a1 in zip(chars, cst, cen):
            if c.isspace():
                if cur:
                    words.append([cur, round(clip_start + w0, 3), round(clip_start + w1, 3)])
                cur, w0 = "", None
                continue
            if w0 is None:
                w0 = a0
            cur += c
            w1 = a1
        if cur:
            words.append([cur, round(clip_start + w0, 3), round(clip_start + w1, 3)])
        for w in words:   # 마지막 어절 끝에 붙는 뒤쪽 무음을 잘라 낸다(정렬 정보는 클립 끝까지 늘어나는 경우가 있다)
            w[2] = round(min(w[2], end), 3)
            w[1] = round(min(w[1], w[2]), 3)
        cues[f"line.{lid}"] = round(start, 3)
        cues[f"line.{lid}.end"] = round(end, 3)
        for name, sub in (l.get("cues") or {}).items():
            k = joined.find(sub)
            if k < 0:
                k2 = joined.replace(" ", "").find(sub.replace(" ", ""))
                if k2 >= 0:   # 공백 차이 보정
                    nz = [j for j, c in enumerate(chars) if not c.isspace()]
                    k = nz[k2]
            if k < 0:
                errors.append(f"{lid}: 큐 '{name}' 의 '{sub}' 를 발음 표기(tts)에서 못 찾음 — tts 문자열 그대로 적을 것")
                continue
            cues[name] = round(clip_start + cst[k], 3)
        if prev_end is not None and start < prev_end + 0.12:
            errors.append(f"{lid}: 앞 줄과 겹침({start:.2f}s < 앞 줄 끝 {prev_end:.2f}s + 0.12) → at 을 늦추거나 앞 줄을 줄일 것")
        if end > dur - 0.4:
            errors.append(f"{lid}: 목표 길이 {dur:g}s 를 넘김(끝 {end:.2f}s) → 문장을 줄이거나 앞 줄들을 당길 것")
        vo.append({"id": lid, "text": l["text"], "sub": l.get("sub", l["text"]), "nosub": bool(l.get("nosub")), "nosrt": bool(l.get("nosrt")),
                   "start": round(start, 3), "end": round(end, 3), "clip": f"audio/vo/{lid}.mp3",
                   "clip_start": round(clip_start, 3), "gain_db": l.get("gain_db", 0), "words": words})
        prev_end = end

    # ── 장면 ──
    scenes = []
    for s in cfg.get("scenes", []):
        st = resolve(s["start"], f"scene {s['id']}")
        en = resolve(s["end"], f"scene {s['id']}") if "end" in s else None
        if st is None:
            continue
        cues[f"scene.{s['id']}"] = round(st, 3)
        if en is not None:
            cues[f"scene.{s['id']}.end"] = round(en, 3)
        scenes.append({"id": s["id"], "start": round(st, 3), "end": round(en, 3) if en is not None else None})
    for i, s in enumerate(scenes):
        if s["end"] is None:
            s["end"] = scenes[i + 1]["start"] if i + 1 < len(scenes) else dur
            cues[f"scene.{s['id']}.end"] = round(s["end"], 3)   # end 를 적지 않은 장면도 .end 큐를 쓸 수 있게

    # ── 효과음 ──
    sfx = []
    for k, e in enumerate(cfg.get("sfx", [])):
        t = resolve(e.get("at", 0), f"sfx {e.get('name')}")
        if t is None:
            continue
        t += float(e.get("offset", 0))
        f = e.get("file")
        if not f:   # ElevenLabs(mp3) 또는 합성(wav) 어느 쪽이든
            f = next((c for c in (f"audio/sfx/{e['name']}.mp3", f"audio/sfx/{e['name']}.wav") if (proj / c).exists()), f"audio/sfx/{e['name']}.mp3")
        if not (proj / f).exists():
            warns.append(f"sfx {e['name']}: 파일 없음 {f} (eleven.py sfx-kit 또는 synth_music.py sfx 로 만들 것)")
        sfx.append({"name": e["name"], "file": f, "t": round(t, 3), "gain_db": e.get("gain_db", -6), "align": e.get("align", "start")})

    music = cfg.get("music")
    if music and music.get("file") and not (proj / music["file"]).exists():
        warns.append(f"음악 파일 없음: {music['file']}")

    # ── 소리 크기 곡선(화면 반응용, REEL.level) ──
    for v in vo:
        v["env"] = {"t0": v["clip_start"], "dt": 0.02, "v": envelope(proj / v["clip"], 0.02)}
    if music and music.get("file") and (proj / music["file"]).exists():
        music_tl = music_tl or {}
        music_tl["energy"] = {"t0": float(music.get("start", 0)), "dt": 0.05,
                              "v": envelope(proj / music["file"], 0.05, db_floor=-30)}

    tl = {"duration": dur, "fps": fps, "vo": [{k: x for k, x in v.items() if k != "env"} for v in vo], "cues": dict(sorted(cues.items(), key=lambda x: x[1])),
          "scenes": scenes, "sfx": sfx, "music": music,
          "music_tl": {k: x for k, x in music_tl.items() if k != "energy"} if music_tl else None}
    (proj / "timeline.json").write_text(json.dumps(tl, ensure_ascii=False, indent=1))
    reel = proj / "reel"
    reel.mkdir(exist_ok=True)
    page_tl = {"duration": dur, "cues": tl["cues"], "scenes": scenes, "music": music_tl,
               "vo": [{k: v[k] for k in ("id", "text", "sub", "nosub", "start", "end", "words", "env")} for v in vo]}
    (reel / "timeline.js").write_text("// timeline.py 가 만든다 — 직접 고치지 말고 script.json 을 고친 뒤 다시 실행\nwindow.TL = " + json.dumps(page_tl, ensure_ascii=False) + ";\n")

    # ── 보고 ──
    speech = sum(v["end"] - v["start"] for v in vo)
    print(f"{'id':6s} {'시작':>6s} {'끝':>6s} {'길이':>5s}  자막")
    for v in vo:
        print(f"{v['id']:6s} {v['start']:6.2f} {v['end']:6.2f} {v['end'] - v['start']:5.2f}  {v['sub']}")
    bar = ["·"] * int(dur)
    for v in vo:
        for s in range(int(v["start"]), min(int(dur), int(v["end"]) + 1)):
            bar[s] = "█"
    print("발화 분포(1칸=1초): " + "".join(bar))
    print(f"발화 {speech:.1f}s / {dur:g}s = {speech / dur * 100:.0f}% · 큐 {len(cues)}개 · 효과음 {len(sfx)}개")
    for w in warns:
        print("△ " + w)
    for e in errors:
        print("✗ " + e)
    print(f"→ {proj / 'timeline.json'} · {reel / 'timeline.js'}")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
