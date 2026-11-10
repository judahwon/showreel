#!/usr/bin/env python3
"""컷 영상에서 사람이 자주 찾아내던 문제를 숫자로 먼저 잡는다. reel.py qa 가 부른다. 결과는 확인할 후보이고, 시각을 열어 보고 판단한다.

사용법: cut_checks.py <영상.mp4> --timeline timeline.json

검사
  컷 경계    경계 ±0.5초에 앞뒤 화면보다 크게 밝거나 어두운 순간이 끼는지(전환 중 드러나는 흰 바탕 등), 한 프레임에 화면이 크게 튀는지
  효과음     효과음 시각 ±0.25초에 화면 변화가 거의 없는지(소리가 사건보다 빠르거나 늦은 후보). 작은 변화까지 구분하지는 못하므로 효과음은 화면 사건의 큐에 묶는 것이 먼저다. 배경음은 cuts.json 에서 "ambient": true
  강조       cuts.json syncs 의 큐가 자막의 해당 구절 시각에서 벗어났는지
  자막 줄    한 줄에 문장 두 개가 들어 있거나 max_chars 글자를 넘는지(줄바꿈 후보). 줄바꿈은 자막 글에 \\n 으로 넣는다
"""
import _utf8  # noqa: F401
import argparse
import json
import re
import subprocess
from pathlib import Path

import numpy as np

WIDTH, HEIGHT = 64, 36
FPS = 30
SEAM_WINDOW = 0.5
SEAM_SIDE = 0.5
# 전환 중 밝기가 앞뒤 화면 범위를 이만큼 벗어나면 다른 화면이 끼어든 것으로 본다(0~255)
TRANSIENT_LEVEL = 18
# 한 프레임 사이 평균 밝기 변화가 이보다 크면 튐으로 본다
JUMP_LEVEL = 70
SFX_WINDOW = 0.25
# motion_check.py 의 멈춤 기준(0.1초 간격)과 같은 뜻. 30fps 프레임 간격에 맞춰 낮췄다
SFX_STILL_LEVEL = 0.15
# 후보에 덧붙일 가장 가까운 큰 변화를 찾는 범위와, 큰 변화로 칠 크기
SFX_NEARBY = 2.0
SFX_EVENT_LEVEL = 0.6
# 자막은 자동 줄바꿈되므로 이 길이는 두 줄에도 잘 안 들어가는 정도다
MAX_CHARS = 70
TWO_SENTENCES_MIN_CHARS = 40


def load_frames(video):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vf", f"fps={FPS},scale={WIDTH}:{HEIGHT}:flags=area,format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, HEIGHT, WIDTH).astype(np.float32)


def clock(seconds):
    return f"{int(seconds // 60):02d}:{seconds % 60:05.2f}"


def check_seams(frames, boundaries):
    findings = []
    levels = frames.mean(axis=(1, 2))
    deltas = np.abs(frames[1:] - frames[:-1]).mean(axis=(1, 2))
    window, side = int(SEAM_WINDOW * FPS), int(SEAM_SIDE * FPS)
    for name, at in boundaries:
        center = int(round(at * FPS))
        left, right = center - window, center + window
        if left - side < 0 or right + side >= len(frames):
            continue
        before = levels[left - side:left]
        after = levels[right:right + side]
        ceiling, floor = max(before.max(), after.max()), min(before.min(), after.min())
        middle = levels[left:right]
        if middle.max() - ceiling > TRANSIENT_LEVEL:
            peak = left + int(middle.argmax())
            findings.append(f"컷 경계 {name} {clock(peak / FPS)}: 앞뒤 화면보다 밝은 순간이 낀다(+{middle.max() - ceiling:.0f}). 전환 중 밝은 바탕이 드러나는지 본다")
        if floor - middle.min() > TRANSIENT_LEVEL:
            dip = left + int(middle.argmin())
            findings.append(f"컷 경계 {name} {clock(dip / FPS)}: 앞뒤 화면보다 어두운 순간이 낀다(-{floor - middle.min():.0f})")
        jump = deltas[left:right].max()
        if jump > JUMP_LEVEL:
            findings.append(f"컷 경계 {name} {clock((left + int(deltas[left:right].argmax())) / FPS)}: 한 프레임에 화면이 크게 튄다({jump:.0f}). 의도한 하드컷인지 본다")
    return findings


def check_sfx(frames, sfx):
    findings = []
    deltas = np.abs(frames[1:] - frames[:-1]).mean(axis=(1, 2))
    span = int(SFX_WINDOW * FPS)
    for item in sfx:
        if item.get("ambient"):
            continue
        center = int(round(item["t"] * FPS))
        chunk = deltas[max(0, center - span):center + span]
        if not len(chunk):
            continue
        reach = int(SFX_NEARBY * FPS)
        nearby_start = max(0, center - reach)
        nearby = deltas[nearby_start:center + reach]
        peak_at = nearby_start + int(nearby.argmax())
        gap = (peak_at - center) / FPS
        event = f" 가장 큰 변화는 {abs(gap):.1f}초 {'뒤' if gap > 0 else '앞'}" if nearby.max() >= SFX_EVENT_LEVEL else ""
        if chunk.max() < SFX_STILL_LEVEL:
            findings.append(f"효과음 {item['name']} {clock(item['t'])}: 소리 앞뒤 {SFX_WINDOW}초에 화면 변화가 없다.{event}")
    return findings


def check_syncs(syncs):
    return [f"강조 {row['label']} ({row['cut']}): 자막 {row['expected']:.2f}s 와 큐 {row['actual']:.2f}s 가 {abs(row['expected'] - row['actual']):.2f}초 어긋난다(허용 {row['tolerance']})"
            for row in syncs if abs(row["expected"] - row["actual"]) > row["tolerance"]]


def check_captions(lines, max_chars):
    findings = []
    for line in lines:
        for part in line["text"].split("\n"):
            sentences = [piece for piece in re.split(r"(?<=[.?!]) +", part.strip()) if piece]
            if len(sentences) > 1 and len(part) >= TWO_SENTENCES_MIN_CHARS:
                findings.append(f"자막 {line['id']} {clock(line['start'])}: 한 줄에 문장 {len(sentences)}개({len(part)}자). 문장 사이를 \\n 으로 나눈다")
            elif len(part) > max_chars:
                findings.append(f"자막 {line['id']} {clock(line['start'])}: 한 줄 {len(part)}자(기준 {max_chars}). 줄이 넘치면 \\n 으로 나눈다")
    return findings


def run(video, timeline, syncs=(), max_chars=MAX_CHARS):
    frames = load_frames(video)
    scenes = timeline.get("scenes", [])
    boundaries = [(f"{left['id']}→{right['id']}", right["start"]) for left, right in zip(scenes, scenes[1:])]
    return [*check_seams(frames, boundaries), *check_sfx(frames, timeline.get("sfx", [])), *check_syncs(syncs), *check_captions(timeline.get("vo", []), max_chars)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("video")
    parser.add_argument("--timeline", required=True)
    args = parser.parse_args()
    timeline = json.loads(Path(args.timeline).read_text(encoding="utf-8"))
    findings = run(args.video, timeline)
    print("\n".join("△ " + item for item in findings) if findings else "✓ 컷 검사 통과")


if __name__ == "__main__":
    main()
