#!/usr/bin/env python3
"""제작 전 환경 점검. 무엇이 있고 무엇이 없는지, 없으면 어떻게 설치하는지 한 번에 보여 준다.

사용법: preflight.py [--voice ibCGc01503OQd2R6i1n1] [--json]
결과의 '쓸 수 있는 경로'를 콘셉트에 반영한다(예: 나레이션 불가 → 음악·화면만으로 전하는 콘셉트, Music 불가 → 합성 음악).
"""
import _utf8  # noqa: F401  (Windows 에서 UTF-8 모드로 다시 실행)
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILLS = Path.home() / ".claude" / "skills"
REPO = "https://github.com/revfactory/skills"


WINDOWS = os.name == "nt"


def find_chrome():
    if os.environ.get("REEL_CHROME_PATH"):
        return True
    if Path("/Applications/Google Chrome.app").exists() or shutil.which("google-chrome") or shutil.which("chrome"):
        return True
    roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    return any(Path(r, "Google", "Chrome", "Application", "chrome.exe").exists() for r in roots if r)


def run(cmd, timeout=20):
    try:
        cmd = [shutil.which(cmd[0]) or cmd[0], *cmd[1:]]   # Windows 의 npm·codex 는 .cmd 라 경로를 찾아 넘긴다
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 127, str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", default="ibCGc01503OQd2R6i1n1")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rows, fix, caps = [], [], {}

    def row(name, ok, info, how=None):
        rows.append(("✓" if ok else "✗", name, info))
        if not ok and how:
            fix.append(f"{name}: {how}")

    # 필수 도구
    code, out = run(["node", "--version"])
    row("node", code == 0, out.splitlines()[0] if code == 0 else "없음", "https://nodejs.org 에서 LTS 설치" + ("" if WINDOWS else " 또는 brew install node"))
    code, out = run(["ffmpeg", "-hide_banner", "-filters"])
    has_ff = code == 0
    flt = {f: (f" {f} " in out) for f in ("loudnorm", "ebur128", "tmix", "freezedetect", "signalstats")}
    row("ffmpeg", has_ff and all(flt.values()), "필터 " + ", ".join(k for k, v in flt.items() if v) if has_ff else "없음", "winget install Gyan.FFmpeg" if WINDOWS else "brew install ffmpeg")
    chrome = find_chrome()
    row("Chrome", chrome, "시스템 Chrome 사용" if chrome else "없음", "Google Chrome 설치 또는 REEL_CHROME_PATH=<chromium 실행 파일>")
    pw = (HERE / "node_modules" / "playwright-core").exists()
    row("playwright-core", pw, "설치됨" if pw else "없음", f"cd {HERE} && npm install (new_project.py 가 자동으로 한다)")
    try:
        import numpy  # noqa
        import PIL  # noqa
        row("python numpy·Pillow", True, f"python {sys.version.split()[0]}")
    except ImportError as e:
        row("python numpy·Pillow", False, str(e), f"{sys.executable} -m pip install numpy pillow")
    font_dirs = (Path.home() / "Library/Fonts", Path("/Library/Fonts"), Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts", Path(os.environ.get("LOCALAPPDATA", ".")) / "Microsoft/Windows/Fonts")
    fonts = [p for d in font_dirs if d.exists() for p in d.glob("Pretendard*")]
    row("한글 폰트(Pretendard)", True, "로컬 있음" if fonts else "없음 → new_project.py 가 내려받음")
    swift = bool(shutil.which("swiftc"))
    # 누끼는 macOS 전용 선택 기능이라 다른 OS 에서는 고칠 것으로 치지 않는다
    if sys.platform == "darwin":
        row("누끼(swiftc, macOS)", swift, "가능" if swift else "불가 — 인물 누끼 없이 진행", "xcode-select --install")
    else:
        rows.append(("–", "누끼(swiftc, macOS)", "macOS 전용 — 인물 누끼 없이 진행"))

    # 이미지 생성
    code, out = run(["codex", "login", "status"])
    codex_ok = code == 0
    row("codex CLI 로그인", codex_ok, out.splitlines()[0][:60] if out else "없음",
        "npm i -g @openai/codex && codex login (ChatGPT 계정)")
    for sk in ("codex-image", "photoreal", "codex-cli"):
        row(f"스킬 {sk}", (SKILLS / sk / "SKILL.md").exists(), "설치됨" if (SKILLS / sk / "SKILL.md").exists() else "없음",
            f"git clone {REPO}.git ~/src/revfactory-skills 후 {sk} 폴더를 ~/.claude/skills/{sk} 로 복사" if WINDOWS else f"git clone {REPO}.git ~/src/revfactory-skills && ln -s ~/src/revfactory-skills/{sk} ~/.claude/skills/{sk}")
    caps["image_gen"] = codex_ok and (SKILLS / "codex-image").exists()

    # ElevenLabs
    sys.path.insert(0, str(HERE))
    import eleven
    key = eleven.api_key()
    if not key:
        row("ElevenLabs API 키", False, "없음", "아래 'ElevenLabs 키 발급' 참고")
        caps.update(narration=False, music_api=None)
    else:
        try:
            json.loads(eleven.request("GET", "/v1/models", retries=1))
            row("ElevenLabs API 키", True, "유효")
            try:
                v = json.loads(eleven.request("GET", f"/v1/voices/{a.voice}", retries=1))
                row("점검 보이스(--voice)", True, v.get("name", a.voice)[:50])
                caps["narration"] = True
            except eleven.ElevenError as e:
                row("점검 보이스(--voice)", False, str(e)[:80], "Free 플랜이면 라이브러리 보이스가 막힌다 → 유료 플랜 또는 eleven.py voices 로 다른 보이스")
                caps["narration"] = "other_voice"
            try:
                s = json.loads(eleven.request("GET", "/v1/user/subscription", retries=1))
                left = (s.get("character_limit") or 0) - (s.get("character_count") or 0)
                row("요금제·크레딧", True, f"{s.get('tier')} · 남은 크레딧 {left:,}")
                caps["music_api"] = s.get("tier") not in (None, "free")
            except eleven.ElevenError:
                row("요금제·크레딧", True, "확인 불가(키에 User Read 권한 없음) — Music 은 첫 생성 시도로 판단")
                caps["music_api"] = None
        except eleven.ElevenError as e:
            row("ElevenLabs API 키", False, str(e).splitlines()[0][:80], "키 권한·유효성 확인")
            caps.update(narration=False, music_api=None)

    if a.json:
        print(json.dumps({"rows": rows, "fix": fix, "caps": caps}, ensure_ascii=False, indent=1))
        return
    for ok, name, info in rows:
        print(f"{ok} {name:24s} {info}")
    print("\n쓸 수 있는 경로:")
    print(f"  렌더링        {'가능' if all(r[0] == '✓' for r in rows[:4]) else '설치 필요'}")
    print(f"  이미지 생성    {'codex-image (+photoreal)' if caps.get('image_gen') else '불가 → 타이포·그래픽·사용자 제공 사진으로 구성'}")
    n = caps.get("narration")
    print(f"  나레이션      {'ElevenLabs (점검 보이스 사용 가능)' if n is True else '다른 보이스 필요' if n == 'other_voice' else '불가 → 음악·자막 버전'}")
    m = caps.get("music_api")
    print(f"  음악          {'ElevenLabs Music' if m else '합성 음악(synth_music.py)' if m is False else 'ElevenLabs Music 시도 → 402 면 합성 음악'}")
    if fix:
        print("\n고칠 것:")
        for f in fix:
            print("  - " + f)
    if not key:
        print("\nElevenLabs 키 발급:\n" + eleven.KEY_HELP)


if __name__ == "__main__":
    main()
