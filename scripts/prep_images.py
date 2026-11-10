#!/usr/bin/env python3
"""이미지 후처리: 생성·수집한 이미지를 영상에 쓰기 좋게 다듬는다.

사용법
  prep_images.py <입력 폴더 또는 파일들> --out assets/img [--width 2400] [--blur] [--cutout]
  prep_images.py --sheet <폴더> --out qa/sheet_images.png        (확인용 썸네일 시트)

처리
  - 레터박스 자동 제거: "16:9 (1536x1024)" 처럼 비율과 해상도가 어긋난 지시를 받으면 모델이 위아래에 검은 띠를 그려 넣는다 → 행 밝기로 찾아 잘라낸다
  - EXIF 회전 바로잡기, 긴 변 --width 로 Lanczos 확대/축소 + 약한 샤픈, JPG 품질 92 (투명 PNG 는 PNG 유지)
  - --blur: 배경·전환용 흐린 버전(<이름>_blur.jpg)도 만든다
  - --cutout: macOS 에서 Vision 으로 인물·피사체 누끼 PNG(<이름>_cut.png). 처음 한 번 Swift 로 컴파일한다(약 20초)
실존 인물 사진은 생성하지 말고 실제 사진(출처 기록)만 쓴다. 누끼는 원본 픽셀을 바꾸지 않는다(배경만 투명).
"""
import _utf8  # noqa: F401  (Windows 에서 UTF-8 모드로 다시 실행)
import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

HERE = Path(__file__).resolve().parent
EXTS = {".png", ".jpg", ".jpeg", ".webp", ".heic"}


def letterbox(im):
    g = im.convert("L")
    w, h = g.size
    px = g.load()
    def row_dark(y):
        vals = [px[x, y] for x in range(0, w, max(1, w // 200))]
        return max(vals) < 24 and sum(vals) / len(vals) < 6
    top = 0
    while top < h // 4 and row_dark(top):
        top += 1
    bot = 0
    while bot < h // 4 and row_dark(h - 1 - bot):
        bot += 1
    if top > 8 or bot > 8:
        return im.crop((0, top, w, h - bot)), (top, bot)
    return im, (0, 0)


def cutout_bin():
    b = HERE / "tools" / "cutout"
    if not b.exists():
        r = subprocess.run(["swiftc", "-O", str(HERE / "tools" / "cutout.swift"), "-o", str(b)], capture_output=True, text=True)
        if r.returncode != 0:
            print("누끼 도구 컴파일 실패(Xcode Command Line Tools 필요: xcode-select --install):\n" + r.stderr[-400:])
            return None
    return b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="*")
    ap.add_argument("--out", required=True)
    ap.add_argument("--width", type=int, default=2400)
    ap.add_argument("--blur", action="store_true")
    ap.add_argument("--cutout", action="store_true")
    ap.add_argument("--sheet")
    a = ap.parse_args()

    if a.sheet:
        files = sorted(p for p in Path(a.sheet).iterdir() if p.suffix.lower() in EXTS)
        raw = [(p.name, ImageOps.exif_transpose(Image.open(p)).convert("RGB")) for p in files]
        portrait = sum(im.height > im.width for _, im in raw) > len(raw) / 2
        cols, tw, th = (6, 300, 534) if portrait else (4, 480, 320)   # 세로 이미지는 세로로 크게 보여 준다
        ims = []
        for n, im in raw:
            im.thumbnail((tw, th))
            ims.append((n, im))
        rows = (len(ims) + cols - 1) // cols
        th = max(im.height for _, im in ims)   # 행 높이를 실제 썸네일 높이에 맞춘다(16:9 썸네일 아래가 비지 않게)
        sheet = Image.new("RGB", (cols * (tw + 8), rows * (th + 30)), (24, 24, 26))
        from PIL import ImageDraw
        d = ImageDraw.Draw(sheet)
        for i, (n, im) in enumerate(ims):
            x, y = (i % cols) * (tw + 8) + 4, (i // cols) * (th + 30) + 4
            sheet.paste(im, (x, y + 22))
            d.text((x, y + 4), f"{n} {im.width}x{im.height}", fill=(255, 214, 90))
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        sheet.save(a.out)
        print(a.out)
        return

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    files = []
    for i in a.inputs:
        p = Path(i)
        files += sorted(x for x in p.iterdir() if x.suffix.lower() in EXTS) if p.is_dir() else [p]
    cb = cutout_bin() if a.cutout and platform.system() == "Darwin" else None
    if a.cutout and not cb:
        print("누끼는 macOS 에서만 기본 지원 — 다른 환경이면 rembg 등을 쓴다")
    for p in files:
        im = ImageOps.exif_transpose(Image.open(p))
        alpha = im.mode in ("RGBA", "LA") and im.getextrema()[-1][0] < 255
        im = im.convert("RGBA" if alpha else "RGB")
        im, (t, b) = letterbox(im) if not alpha else (im, (0, 0))
        scale = a.width / max(im.size)
        if abs(scale - 1) > 0.02:
            im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
            if scale > 1:
                im = im.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))
        dst = out / (p.stem + (".png" if alpha else ".jpg"))
        im.save(dst, **({} if alpha else {"quality": 92}))
        note = f" 레터박스 제거(위 {t}px·아래 {b}px)" if t or b else ""
        print(f"{dst} {im.width}x{im.height}{note}")
        if a.blur and not alpha:
            im.resize((im.width // 2, im.height // 2)).filter(ImageFilter.GaussianBlur(14)).save(out / f"{p.stem}_blur.jpg", quality=85)
        if cb:
            r = subprocess.run([str(cb), str(dst), str(out / f"{p.stem}_cut.png")], capture_output=True, text=True)
            print("  누끼: " + (r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-200:]))


if __name__ == "__main__":
    main()
