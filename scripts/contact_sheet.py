#!/usr/bin/env python3
"""검수용 콘택트 시트(라벨 붙은 썸네일 격자)를 만든다. 만든 PNG 는 Read 로 열어 직접 본다.

사용법
  스틸 폴더:   contact_sheet.py --dir qa/stills --out qa/sheet_stills.png
  영상 간격:   contact_sheet.py --video out/video.mp4 --every 1 --out qa/sheet.png [--from 0 --to 60]
  지정 시각:   contact_sheet.py --video out/video.mp4 --times 1.2,3.4,8 --out qa/sheet_t.png
  전환 구간:   contact_sheet.py --video out/video.mp4 --around 3.0,6.1 --frames 3 --out qa/sheet_cuts.png
               (각 시각의 ±3프레임을 한 줄로 — 빈 프레임·잔상·겹침 확인용)
  공통:        [--timeline timeline.json] 장면 id 를 라벨에 붙인다. [--cols 4] [--thumb 480] [--per 24]

칸이 --per 개를 넘으면 out_01.png, out_02.png … 로 나눠 저장한다.
ffmpeg 에 drawtext 가 없어도 되도록 라벨은 PIL 로 그린다. 프레임은 select 로 정확한 프레임 번호를 뽑는다.
"""
import _utf8  # noqa: F401  (Windows 에서 UTF-8 모드로 다시 실행)
import argparse
import glob
import json
import os
import re
import subprocess
import shutil

from PIL import Image, ImageDraw, ImageFont

FONTS = [
    os.path.expanduser("~/Library/Fonts/PretendardVariable.ttf"),
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def font(size):
    for f in FONTS:
        if os.path.exists(f):
            try:
                return ImageFont.truetype(f, size)
            except OSError:
                pass
    return ImageFont.load_default()


def probe(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=r_frame_rate,nb_frames,duration:format=duration", "-of", "json", video],
                         capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    st = d["streams"][0]
    num, den = st["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    dur = float(st.get("duration") or d["format"]["duration"])
    return fps, dur


def extract(video, frames, tmp):
    """정확한 프레임 번호들을 한 번에 뽑는다."""
    frames = sorted(set(frames))
    paths = {}
    for i in range(0, len(frames), 40):  # select 식에 eq() 가 100개를 넘으면 ffmpeg 가 메모리 오류로 멈춘다
        part = frames[i:i + 40]
        expr = "+".join(f"eq(n\\,{f})" for f in part)
        pat = os.path.join(tmp, f"b{i:05d}_%04d.png")
        command = ["ffmpeg", "-v", "error", "-y", "-i", video, "-vf", f"select='{expr}'"]
        # -vsync 는 ffmpeg 8 에서 없어졌고 -fps_mode 는 5.1 부터 있다
        if subprocess.run([*command, "-fps_mode", "vfr", pat], capture_output=True).returncode != 0:
            subprocess.run([*command, "-vsync", "vfr", pat], check=True)
        got = sorted(glob.glob(os.path.join(tmp, f"b{i:05d}_*.png")))
        for f, p in zip(part, got):
            paths[f] = p
    return paths


def scene_at(tl, t):
    if not tl:
        return ""
    cur = ""
    for s in tl.get("scenes", []):
        if s["start"] <= t + 1e-6:
            cur = s["id"]
    return cur


def build(items, out, cols, thumb, per, title=None):
    """items: [(image_path, label)]"""
    outs = []
    pages = [items[i:i + per] for i in range(0, len(items), per)] or [[]]
    f = font(max(13, thumb // 26))
    for pi, page in enumerate(pages):
        if not page:
            continue
        im0 = Image.open(page[0][0])
        w = thumb
        h = round(im0.height * thumb / im0.width)
        lab = max(20, thumb // 18)
        rows = (len(page) + cols - 1) // cols
        top = lab + 6 if title else 0
        sheet = Image.new("RGB", (cols * (w + 4) + 4, top + rows * (h + lab + 4) + 4), (24, 24, 26))
        d = ImageDraw.Draw(sheet)
        if title:
            d.text((8, 3), title, fill=(200, 200, 200), font=f)
        for i, (p, label) in enumerate(page):
            img = Image.open(p).convert("RGB").resize((w, h), Image.LANCZOS)
            x = 4 + (i % cols) * (w + 4)
            y = top + 4 + (i // cols) * (h + lab + 4)
            sheet.paste(img, (x, y + lab))
            d.text((x + 4, y + 2), label, fill=(255, 214, 90), font=f)
        path = out if len(pages) == 1 else re.sub(r"(\.\w+)$", f"_{pi + 1:02d}\\1", out)
        sheet.save(path)
        outs.append(path)
    return outs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir")
    ap.add_argument("--video")
    ap.add_argument("--out", required=True)
    ap.add_argument("--every", type=float)
    ap.add_argument("--times")
    ap.add_argument("--around")
    ap.add_argument("--frames", type=int, default=3)
    ap.add_argument("--from", dest="start", type=float, default=0.0)
    ap.add_argument("--to", dest="end", type=float, default=0.0)
    ap.add_argument("--timeline")
    ap.add_argument("--cols", type=int, default=4)
    ap.add_argument("--thumb", type=int, default=480)
    ap.add_argument("--per", type=int, default=24)
    a = ap.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    tl = json.load(open(a.timeline)) if a.timeline and os.path.exists(a.timeline) else None

    if a.dir:
        files = sorted(glob.glob(os.path.join(a.dir, "*.png")) + glob.glob(os.path.join(a.dir, "*.jpg")))
        items = []
        for p in files:
            m = re.search(r"t(\d+\.\d+)", os.path.basename(p))
            if m:
                t = float(m.group(1))
                items.append((p, f"{t:.2f}s {scene_at(tl, t)}".strip()))
            else:
                items.append((p, os.path.basename(p)))
        outs = build(items, a.out, a.cols, a.thumb, a.per)
    else:
        fps, dur = probe(a.video)
        end = a.end or dur
        if a.around:
            cols = 2 * a.frames + 1
            frames, items_spec = [], []
            for t in [float(x) for x in a.around.split(",")]:
                c = round(t * fps)
                for k in range(-a.frames, a.frames + 1):
                    f = min(max(0, c + k), round(dur * fps) - 1)
                    frames.append(f)
                    items_spec.append((f, f"{t:.2f}s {'+' if k >= 0 else ''}{k}f"))
            a.cols, a.per = cols, cols * 6
        else:
            if a.times:
                ts = [float(x) for x in a.times.split(",")]
            else:
                step = a.every or 1.0
                ts, x = [], a.start
                while x < end - 1e-6:
                    ts.append(x)
                    x += step
            frames = [min(round(t * fps), round(dur * fps) - 1) for t in ts]
            items_spec = [(f, f"{f / fps:.2f}s f{f} {scene_at(tl, f / fps)}".strip()) for f in frames]
        # 작업 파일은 프로젝트 안(시트 옆 .frames/)에 두고 끝나면 지운다 — 시스템 임시 폴더를 쓰지 않는다
        tmp = os.path.join(os.path.dirname(os.path.abspath(a.out)), f".frames_{os.getpid()}")   # 동시에 여러 시트를 뽑아도 섞이지 않게
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        try:
            paths = extract(a.video, frames, tmp)
            items = [(paths[f], lab) for f, lab in items_spec if f in paths]
            outs = build(items, a.out, a.cols, a.thumb, a.per)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    for o in outs:
        print(o)


if __name__ == "__main__":
    main()
