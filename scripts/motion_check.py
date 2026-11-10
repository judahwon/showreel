#!/usr/bin/env python3
"""화면이 얼마나 움직이는지 잰다. 초안(render --scale 0.5)에서 돌려 멈춘 구간을 찾아 고친 뒤 최종 렌더한다.

사용법
  motion_check.py out/draft.mp4 [--timeline timeline.json] [--skip-end 3] [--json out.json]

재는 것(그레인을 지우려고 64×36 으로 줄인 흑백 프레임, 0.1초 간격)
  움직임       0.1초 사이 평균 밝기 변화. 0.35 미만이면 '멈춤'으로 센다
  멈춘 비율    멈춘 0.1초 칸의 비율. 엔드카드(--skip-end 초)는 뺀다
  멈춘 구간    1초 넘게 이어진 멈춤 — 시각과 장면 이름을 보여 준다
  큰 움직임    상위 10% 움직임(p90). 구도 전환·큰 이동·매체 전환이 있으면 커진다
  섬광         qa_check.py 와 같은 판정(480×270, 원래 fps). 인접 프레임에서 밝기가 0.1 넘게 오른(내린) 화소가
               화면 10% 이상이면 전이, 반대 방향 전이 두 번 = 섬광 1회, 1초에 3회 초과면 ✗. 초안에서 미리 잡으려고 넣었다

기준(같은 요청을 스킬 없이 만든 영상과 비교해 정한 값)
  20초 이하: 멈춘 비율 ≤ 8%, p90 ≥ 15 · 60초 이하: ≤ 20%, p90 ≥ 7 · 그보다 긴 영상: ≤ 35%, p90 ≥ 6
  1초 넘는 멈춤은 의도한 정적(음악을 끊은 순간 등)만 남긴다
  (참고: 스킬 없이 만든 같은 요청의 영상 — 15초 2%·p90 53, 30초 12%·p90 8.7, 2분 39%·p90 5.7)
"""
import _utf8  # noqa: F401  (Windows 에서 UTF-8 모드로 다시 실행)
import argparse
import json
import subprocess
import sys

import numpy as np

STILL = 0.35


def measure(path, skip_end=3.0):
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", "fps=10,scale=64:36:flags=area,format=gray", "-f", "rawvideo", "-"],
                       capture_output=True, check=True).stdout
    f = np.frombuffer(p, np.uint8).reshape(-1, 36, 64).astype(np.float32)
    d = np.abs(f[1:] - f[:-1]).mean(axis=(1, 2))            # d[i] = i/10 ~ (i+1)/10 초
    dur = len(f) / 10
    body = d[: max(1, int((dur - skip_end) * 10))]
    still = body < STILL
    runs, s = [], None
    for i, x in enumerate(list(still) + [False]):
        if x and s is None:
            s = i
        elif not x and s is not None:
            if i - s >= 10:
                runs.append((s / 10, i / 10))
            s = None
    sec = [float(d[k * 10:(k + 1) * 10].mean()) for k in range(int(dur))]
    return {"duration": round(dur, 1), "still_ratio": round(float(still.mean()), 3), "median": round(float(np.median(body)), 2),
            "p90": round(float(np.percentile(body, 90)), 2), "still_runs": [(round(a, 1), round(b, 1)) for a, b in runs],
            "per_second": [round(x, 2) for x in sec]}


def flash_check(path):
    """qa_check.py 와 같은 섬광 판정. (섬광 시각 목록, 1초에 3회를 넘는 창의 시작 시각 목록, fps)"""
    W_, H_ = 480, 270
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                          "-of", "default=nw=1:nk=1", path], capture_output=True, text=True).stdout.split()
    fps_s = out[0].strip(",") if out else "30/1"
    n_, d_ = (fps_s.split("/") + ["1"])[:2]
    fps = float(n_) / float(d_ or 1)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", f"scale={W_}:{H_},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    nfr = len(raw) // (W_ * H_)
    v = np.frombuffer(raw[: nfr * W_ * H_], np.uint8).reshape(nfr, H_, W_).astype(np.float32) / 255.0
    lin = np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)
    events = []
    for i in range(1, nfr):
        dd = lin[i] - lin[i - 1]
        if float((dd >= 0.1).mean()) >= 0.10:
            events.append((i, 1))
        if float((dd <= -0.1).mean()) >= 0.10:
            events.append((i, -1))
    flashes, last = [], None
    for f, sgn in events:
        if last is not None and sgn != last:
            flashes.append(f)
            last = None
        else:
            last = sgn
    bad = sorted({f for f in flashes if sum(1 for x in flashes if f <= x < f + fps) > 3})
    return [f / fps for f in flashes], [f / fps for f in bad], fps


def limits(dur):
    """길이별 기준. 짧을수록 더 촘촘하고 크게 움직여야 한다."""
    if dur <= 21:
        return 0.08, 15.0
    if dur <= 61:
        return 0.20, 7.0
    return 0.35, 6.0


def verdict(m, max_runs=10):
    lim, p90 = limits(m["duration"])
    issues = []
    if m["still_ratio"] > lim:
        issues.append(f"멈춘 화면 {m['still_ratio'] * 100:.0f}% (기준 {lim * 100:.0f}% 이하) — 의도한 정지가 아니면 새 사건(등장·재배치·카메라 이동)이나 그 구간의 아트 디렉션에 맞는 배경 움직임을 넣을 것")
    runs = sorted(m["still_runs"], key=lambda r: r[0] - r[1])[:max_runs]
    for a, b in sorted(runs):
        issues.append(f"{a:.1f}~{b:.1f}s 동안 {b - a:.1f}초 멈춤 — 새 사건(등장·재배치·카메라 이동)을 넣거나 의도한 정적인지 확인")
    if len(m["still_runs"]) > max_runs:
        issues.append(f"그 밖에 1초 넘는 멈춤 {len(m['still_runs']) - max_runs}곳 (긴 것부터 {max_runs}곳만 적음)")
    if m["p90"] < p90:
        issues.append(f"큰 움직임 p90 {m['p90']:.1f} (기준 {p90:g} 이상) — 화면 전체가 크게 바뀌는 순간(구도 전환·큰 이동·매체 전환)이 부족하다. 무엇으로 채울지는 구간의 아트 디렉션으로 정한다")
    return issues


def scene_at(tl, t):
    cur = ""
    for s in (tl or {}).get("scenes", []):
        if s["start"] <= t + 1e-6:
            cur = s["id"]
    return cur


def spark(v):
    return "".join("▁▂▃▄▅▆▇█"[max(0, min(7, int(np.log1p(x) / np.log1p(12) * 7.99)))] for x in v)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--timeline")
    ap.add_argument("--skip-end", type=float, default=3.0)
    ap.add_argument("--json")
    a = ap.parse_args()
    m = measure(a.video, a.skip_end)
    tl = json.load(open(a.timeline)) if a.timeline else None
    print(f"움직임: 중앙 {m['median']:.2f} · 큰 움직임 p90 {m['p90']:.1f} · 멈춘 화면 {m['still_ratio'] * 100:.0f}% (마지막 {a.skip_end:g}초 제외)")
    print("초별 움직임: " + spark(m["per_second"]))
    issues = verdict(m)
    for x in issues:
        for a0, b0 in m["still_runs"]:
            if x.startswith(f"{a0:.1f}~") and scene_at(tl, a0):
                x += f" [장면 {scene_at(tl, a0)}]"
        print("△ " + x)
    if not issues:
        print("✓ 움직임 기준 통과")
    fl, bad, _ = flash_check(a.video)
    if bad:
        starts, prev = [], -9
        for t in bad:
            if t - prev > 1.0:
                starts.append(t)
            prev = t
        where = ", ".join(f"{t:.2f}s" + (f" [장면 {scene_at(tl, t)}]" if scene_at(tl, t) else "") for t in starts[:8])
        print(f"✗ 섬광 과다(1초에 3회 초과): {where} — 동시에 바뀌는 면적을 나누거나 색 반전 대신 위치·크기로 박을 표현할 것")
        issues.append("섬광 과다: " + where)
    else:
        print(f"✓ 섬광 {len(fl)}회, 1초에 3회 넘는 구간 없음")
    if a.json:
        json.dump({**m, "issues": issues}, open(a.json, "w"), ensure_ascii=False, indent=1)
    sys.exit(0)


if __name__ == "__main__":
    main()
