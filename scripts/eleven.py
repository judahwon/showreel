#!/usr/bin/env python3
"""ElevenLabs 헬퍼 (TTS·STT·효과음·음악·보이스 조회·키 점검). 다른 스크립트가 import 해서도 쓴다.

왜 curl 인가: python.org 판 Python 의 urllib 은 인증서 저장소 문제로 TLS 검증에 자주 실패한다.
검증을 끄지 않고, 시스템 인증서·프록시 설정을 그대로 따르는 curl 을 호출한다.
키는 curl 표준 입력으로만 넘긴다(파일·argv·임시 폴더에 남지 않는다).

사용법
  eleven.py check [--voice ibCGc01503OQd2R6i1n1]        키·보이스·모델·요금제 점검 (가장 먼저)
  eleven.py voices [--lang ko] [--library] [--q 검색어]   쓸 수 있는 보이스 목록 (--library: 공개 라이브러리 검색)
  eleven.py tts "텍스트" out.mp3 [--voice ID] [--model eleven_multilingual_v2] [--stability .45] [--similarity .8]
                [--style .4] [--speed 1.0] [--seed N] [--timestamps]
  eleven.py stt audio.mp3 [--lang kor] [--json out.json]          Scribe 받아쓰기
  eleven.py sfx "prompt" out.mp3 [--dur 2.0] [--influence 0.5] [--loop]
  eleven.py sfx-kit <outdir> --names impact,whoosh,riser [--presets presets.json]   프리셋 효과음 일괄(없는 것만)
  eleven.py music out.mp3 (--plan plan.json | --prompt "..." --length 60) [--model music_v1] [--seed N]

종료 코드: 0 성공 · 2 키 없음 · 3 키 무효/권한 부족 · 4 유료 플랜 필요 · 5 요청 오류 · 6 네트워크
"""
import _utf8  # noqa: F401  (Windows 에서 UTF-8 모드로 다시 실행)
import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

API = "https://api.elevenlabs.io"
DEFAULT_VOICE = "ibCGc01503OQd2R6i1n1"   # Marcus — 깊은 바리톤 한국어. 아래 설정을 낭독용으로 검증한 보이스(script.json 에 voice 가 없을 때 쓰는 값)
DEFAULT_MODEL = "eleven_multilingual_v2"
DEFAULT_SETTINGS = {"stability": 0.45, "similarity_boost": 0.8, "style": 0.4, "use_speaker_boost": True, "speed": 1.0}
SKILL = Path(__file__).resolve().parent.parent

KEY_HELP = """ElevenLabs API 키가 필요하다.
  1) https://elevenlabs.io 가입·로그인 → 왼쪽 아래 Developers(또는 프로필) → API Keys → Create API Key
  2) 권한: Text to Speech, Speech to Text, Sound Effects, Music, Voices(Read), User(Read) 를 켠다
     (User Read 가 있어야 남은 크레딧을 확인할 수 있다). 키는 한 번만 보이므로 바로 복사한다.
  3) 셸 설정에 저장:  echo 'export ELEVENLABS_API_KEY="sk_..."' >> ~/.zshrc && source ~/.zshrc
     (또는 프로젝트 폴더의 .env 파일에 ELEVENLABS_API_KEY=sk_... 한 줄)
  요금제: Free 는 월 1만 크레딧·상업적 이용 불가·라이브러리 보이스(Marcus 등)·Music API 사용 불가.
          Starter($6) 이상이면 라이브러리 보이스와 상업적 이용, Music 은 유료 플랜에서 가능."""


class ElevenError(Exception):
    def __init__(self, msg, code=5, status=None, body=None):
        super().__init__(msg)
        self.code, self.status, self.body = code, status, body


def api_key():
    k = os.environ.get("ELEVENLABS_API_KEY")
    if k:
        return k.strip()
    for envf in (Path.cwd() / ".env", Path.cwd().parent / ".env", Path.home() / ".config/showreel/.env"):
        if envf.exists():
            for line in envf.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("ELEVENLABS_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _explain(status, body):
    try:
        d = json.loads(body)
        det = d.get("detail", d)
        if isinstance(det, dict):
            st, msg = det.get("status", ""), det.get("message", "")
        else:
            st, msg = "", str(det)
    except Exception:
        st, msg = "", body[:300].decode("utf-8", "replace") if isinstance(body, bytes) else str(body)[:300]
    if status == 401:
        if "missing_permissions" in st or "permission" in msg:
            return ElevenError(f"API 키에 권한이 없다({msg}). ElevenLabs 대시보드에서 키 권한을 켜거나 새 키를 만든다.", 3, status, body)
        return ElevenError("API 키가 유효하지 않다.\n" + KEY_HELP, 3, status, body)
    if status == 402:
        return ElevenError(f"유료 플랜이 필요한 기능이다({msg or st}). 라이브러리 보이스·Music API 는 Free 에서 막힌다.", 4, status, body)
    if status == 403:
        return ElevenError(f"요금제 제한({msg or st}). 예: 192kbps 출력은 Creator 이상.", 4, status, body)
    return ElevenError(f"HTTP {status}: {st} {msg}", 5, status, body)


def request(method, path, body=None, form=None, accept_audio=False, timeout=300, retries=6):
    """ElevenLabs 호출. 성공 시 응답 바이트를 돌려준다. 429·5xx·네트워크 오류는 점점 길게 기다리며 다시 시도한다."""
    key = api_key()
    if not key:
        raise ElevenError(KEY_HELP, 2)
    # 키는 파일이나 argv 에 남기지 않고 curl 표준 입력(-H @-)으로만 넘긴다. 본문(비밀 아님)은 인자로 넘기므로 임시 파일이 없다.
    cmd = ["curl", "-sS", "-X", method, API + path, "-H", "@-", "--max-time", str(timeout), "-w", "\n%{http_code}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "--data-binary", json.dumps(body, ensure_ascii=False)]
    for k, v in (form or {}).items():
        cmd += ["-F", f"{k}={v}"]
    hdr = f"xi-api-key: {key}\n".encode()
    last = None
    for attempt in range(retries):
        r = subprocess.run(cmd, input=hdr, capture_output=True)
        if r.returncode != 0:
            last = ElevenError(f"네트워크 오류: {r.stderr.decode('utf-8', 'replace')[:300]}", 6)
            time.sleep(min(30, 3 * (attempt + 1)))
            continue
        out, _, code = r.stdout.rpartition(b"\n")
        status = int(code or 0)
        if status == 200:
            if accept_audio and (out[:1] == b"{" or len(out) < 800):
                # 오류 JSON 이 오디오 파일로 저장되는 사고 방지 (빈 텍스트 TTS 는 200 + 무음 mp3 를 준다)
                raise ElevenError(f"오디오 응답이 비정상이다({len(out)}B): {out[:200]!r}", 5, status, out)
            return out
        if status == 429 or status >= 500:
            last = _explain(status, out)
            wait = min(60, 4 * 2 ** attempt)
            print(f"  … {status} 재시도 {attempt + 1}/{retries} ({wait}s 대기)", file=sys.stderr)
            time.sleep(wait)
            continue
        raise _explain(status, out)
    raise last or ElevenError("재시도 초과", 5)


# ───────── 기능 ─────────
def tts(text, out, voice=DEFAULT_VOICE, model=DEFAULT_MODEL, settings=None, seed=None,
        previous_text=None, next_text=None, timestamps=True, language_code="ko", fmt="mp3_44100_192"):
    """mp3 저장. timestamps=True 면 out 옆에 <이름>.align.json(글자 단위 정렬: alignment) 저장. 정렬 dict 를 돌려준다."""
    if not text or not text.strip():
        raise ElevenError("빈 텍스트는 보내지 않는다(ElevenLabs 는 200 과 무음 mp3 를 돌려준다).", 5)
    vs = {**DEFAULT_SETTINGS, **(settings or {})}
    if model.startswith("eleven_v3") or model.startswith("eleven_v4"):
        vs = {k: v for k, v in vs.items() if k in ("stability", "similarity_boost")}   # v3·v4 는 style·speed 미지원
    body = {"text": text, "model_id": model, "voice_settings": vs}
    if language_code:
        body["language_code"] = language_code
    if seed is not None:
        body["seed"] = int(seed)
    if not model.startswith("eleven_v3"):   # v3 는 previous/next_text 를 받지 않는다
        if previous_text:
            body["previous_text"] = previous_text
        if next_text:
            body["next_text"] = next_text
    ep = "/with-timestamps" if timestamps else ""
    for f in (fmt, "mp3_44100_128"):
        try:
            raw = request("POST", f"/v1/text-to-speech/{voice}{ep}?output_format={f}", body=body, accept_audio=not timestamps)
            break
        except ElevenError as e:
            if e.status == 403 and f != "mp3_44100_128":
                print("  … 192kbps 가 막힌 요금제라 128kbps 로 다시 요청", file=sys.stderr)
                continue
            raise
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    if timestamps:
        d = json.loads(raw)
        audio = base64.b64decode(d["audio_base64"])
        if len(audio) < 800:
            raise ElevenError("오디오가 비어 있다", 5)
        Path(out).write_bytes(audio)
        al = d.get("alignment") or d.get("normalized_alignment") or {}   # normalized_alignment 는 한국어를 로마자로 준다 → alignment 우선
        Path(out).with_suffix(".align.json").write_text(json.dumps(al, ensure_ascii=False), encoding="utf-8")
        return al
    Path(out).write_bytes(raw)
    return None


def stt(path, lang="kor", words=True, model="scribe_v1"):
    form = {"model_id": model, "file": f"@{path}"}
    if lang:
        form["language_code"] = lang
    if words:
        form["timestamps_granularity"] = "word"
    return json.loads(request("POST", "/v1/speech-to-text", form=form, timeout=300))


def sfx(text, out, dur=None, influence=0.5, loop=False, fmt="mp3_44100_128"):
    body = {"text": text, "model_id": "eleven_text_to_sound_v2", "prompt_influence": influence}
    if dur is not None:
        body["duration_seconds"] = max(0.5, min(30.0, float(dur)))   # 허용 범위 0.5~30초
    if loop:
        body["loop"] = True
    raw = request("POST", f"/v1/sound-generation?output_format={fmt}", body=body, accept_audio=True)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_bytes(raw)


def music(out, plan=None, prompt=None, length_s=None, model="music_v1", seed=None, fmt="mp3_44100_192"):
    if plan is not None:
        body = {"composition_plan": plan, "model_id": model}
    else:
        body = {"prompt": prompt, "music_length_ms": int(max(3, length_s) * 1000), "model_id": model, "force_instrumental": True}
    if seed is not None and plan is None:   # --prompt 와 seed 를 함께 보내면 422 가 났다(2026-10 비교 실험)
        print("  ! --seed 는 --plan 과 함께만 쓴다. --prompt 생성에서는 seed 를 빼고 보낸다", file=sys.stderr)
        seed = None
    if seed is not None:
        body["seed"] = int(seed)
    for f in (fmt, "mp3_44100_128"):
        try:
            raw = request("POST", f"/v1/music?output_format={f}", body=body, accept_audio=True, timeout=900, retries=5)
            break
        except ElevenError as e:
            if e.status == 403 and f != "mp3_44100_128":
                continue
            if e.status in (400, 422) and e.body:
                try:
                    det = json.loads(e.body).get("detail", {})
                    sug = (det.get("data") or {}).get("prompt_suggestion") or (det.get("data") or {}).get("composition_plan_suggestion")
                    if sug:
                        print("거부됨(아티스트·곡명 등 저작권 표현). 제안:\n" + json.dumps(sug, ensure_ascii=False, indent=1), file=sys.stderr)
                except Exception:
                    pass
            raise
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_bytes(raw)


def peak_db(path):
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "max_volume" in line:
            return float(line.split("max_volume:")[1].split("dB")[0])
    return -99.0


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


# ───────── CLI ─────────
def cmd_check(a):
    key = api_key()
    if not key:
        print("✗ ELEVENLABS_API_KEY 가 없다.\n" + KEY_HELP)
        return 2
    rep = {"key": "있음"}
    try:
        models = json.loads(request("GET", "/v1/models", retries=2))
        ko = [m["model_id"] for m in models if any(l.get("language_id") == "ko" for l in m.get("languages", [])) and m.get("can_do_text_to_speech")]
        rep["models_ko"] = ko
    except ElevenError as e:
        print("✗ 키 점검 실패: " + str(e))
        return e.code
    try:
        v = json.loads(request("GET", f"/v1/voices/{a.voice}", retries=2))
        rep["voice"] = {"id": a.voice, "name": v.get("name"), "category": v.get("category"), "labels": v.get("labels"),
                        "fine_tuned_models": [k for k, s in (v.get("fine_tuning", {}).get("state") or {}).items() if s == "fine_tuned"]}
    except ElevenError as e:
        rep["voice"] = f"조회 실패: {e}"
    try:
        s = json.loads(request("GET", "/v1/user/subscription", retries=1))
        rep["plan"] = {"tier": s.get("tier"), "used": s.get("character_count"), "limit": s.get("character_limit"),
                       "remaining": (s.get("character_limit") or 0) - (s.get("character_count") or 0)}
    except ElevenError as e:
        rep["plan"] = "확인 불가(키에 User Read 권한 없음) — 크레딧은 대시보드에서 확인" if e.status == 401 else str(e)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    if isinstance(rep["voice"], str):
        print("※ 보이스를 쓸 수 없다. Free 플랜이면 라이브러리 보이스(Marcus 등)가 막힌다 → 플랜 업그레이드 또는 `eleven.py voices` 로 다른 보이스 선택.")
        return 4
    return 0


def cmd_voices(a):
    if a.library:
        q = f"/v1/shared-voices?page_size=50&language={a.lang or 'ko'}" + (f"&search={a.q}" if a.q else "")
        d = json.loads(request("GET", q))
        for v in d.get("voices", []):
            print(f"{v.get('voice_id')}  {v.get('name')[:40]:40s} {v.get('gender','')}/{v.get('age','')} {v.get('accent','')} {v.get('use_case','')}  {str(v.get('descriptive',''))}")
        return 0
    d = json.loads(request("GET", "/v2/voices?page_size=100" + (f"&search={a.q}" if a.q else "")))
    for v in d.get("voices", []):
        lab = v.get("labels") or {}
        langs = {lab.get("language", "")} | {x.get("language") for x in (v.get("verified_languages") or [])}
        if a.lang and a.lang not in langs:
            continue
        print(f"{v['voice_id']}  {v['name'][:44]:44s} {v.get('category',''):12s} {lab.get('gender','')}/{lab.get('age','')} {lab.get('accent','')} {lab.get('descriptive','')}")
    return 0


def cmd_tts(a):
    st = {k: v for k, v in {"stability": a.stability, "similarity_boost": a.similarity, "style": a.style, "speed": a.speed}.items() if v is not None}
    tts(a.text, a.out, voice=a.voice, model=a.model, settings=st, seed=a.seed, timestamps=a.timestamps)
    print(f"{a.out} {duration(a.out):.2f}s")
    return 0


def cmd_stt(a):
    d = stt(a.file, lang=a.lang)
    if a.json:
        Path(a.json).write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    print(d.get("text", ""))
    return 0


def load_presets(path=None):
    p = Path(path) if path else SKILL / "assets" / "sfx_presets.json"
    return {x["name"]: x for x in json.loads(p.read_text(encoding="utf-8"))}


def cmd_sfx(a):
    sfx(a.text, a.out, dur=a.dur, influence=a.influence, loop=a.loop)
    pk = peak_db(a.out)
    print(f"{a.out} {duration(a.out):.2f}s peak {pk:.1f}dB")
    if pk <= -24:   # 거의 무음으로 생성되는 경우가 있다(sfx-kit 과 같은 기준)
        print(f"✗ 거의 무음(peak {pk:.1f}dB) — 프롬프트를 바꿔 다시 만들 것", file=sys.stderr)
        return 2
    return 0


def cmd_sfx_kit(a):
    from concurrent.futures import ThreadPoolExecutor
    presets = load_presets(a.presets)
    names = [n.strip() for n in a.names.split(",")] if a.names else list(presets)
    unknown = [n for n in names if n not in presets]
    if unknown:
        print("프리셋에 없는 이름: " + ", ".join(unknown) + "\n가능: " + ", ".join(presets))
        return 5
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)

    def one(n):
        p = presets[n]
        f = out / f"{n}.mp3"
        if f.exists() and f.stat().st_size > 2000:
            return f"{n}: 있음"
        for attempt in range(3):
            sfx(p["prompt"] + (", close microphone, punchy, loud" if attempt else ""), f, dur=p.get("dur"),
                influence=min(0.9, p.get("influence", 0.5) + 0.15 * attempt), loop=p.get("loop", False))
            pk = peak_db(f)
            if pk > -24:          # 거의 무음으로 생성되는 경우가 있다 → 다시 만든다
                return f"{n}: {duration(f):.2f}s peak {pk:.1f}dB"
        return f"{n}: 너무 조용함(peak {pk:.1f}dB) — 믹스에서 게인을 올릴 것"

    with ThreadPoolExecutor(max_workers=3) as ex:   # 동시 요청 한도(플랜별 2~5)를 넘지 않게
        for line in ex.map(one, names):
            print(line)
    return 0


def cmd_music(a):
    plan = json.loads(Path(a.plan).read_text(encoding="utf-8")) if a.plan else None
    if plan and "composition_plan" in plan:
        model = plan.get("model_id", a.model)
        plan = plan["composition_plan"]
    else:
        model = a.model
    music(a.out, plan=plan, prompt=a.prompt, length_s=a.length, model=model, seed=a.seed)
    print(f"{a.out} {duration(a.out):.2f}s")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("check"); p.add_argument("--voice", default=DEFAULT_VOICE); p.set_defaults(fn=cmd_check)
    p = sp.add_parser("voices"); p.add_argument("--lang"); p.add_argument("--q"); p.add_argument("--library", action="store_true"); p.set_defaults(fn=cmd_voices)
    p = sp.add_parser("tts"); p.add_argument("text"); p.add_argument("out"); p.add_argument("--voice", default=DEFAULT_VOICE)
    p.add_argument("--model", default=DEFAULT_MODEL); p.add_argument("--stability", type=float); p.add_argument("--similarity", type=float)
    p.add_argument("--style", type=float); p.add_argument("--speed", type=float); p.add_argument("--seed", type=int)
    p.add_argument("--timestamps", action="store_true"); p.set_defaults(fn=cmd_tts)
    p = sp.add_parser("stt"); p.add_argument("file"); p.add_argument("--lang", default="kor"); p.add_argument("--json"); p.set_defaults(fn=cmd_stt)
    p = sp.add_parser("sfx"); p.add_argument("text"); p.add_argument("out"); p.add_argument("--dur", type=float)
    p.add_argument("--influence", type=float, default=0.5); p.add_argument("--loop", action="store_true"); p.set_defaults(fn=cmd_sfx)
    p = sp.add_parser("sfx-kit"); p.add_argument("outdir"); p.add_argument("--names"); p.add_argument("--presets"); p.set_defaults(fn=cmd_sfx_kit)
    p = sp.add_parser("music"); p.add_argument("out"); p.add_argument("--plan"); p.add_argument("--prompt"); p.add_argument("--length", type=float, default=60)
    p.add_argument("--model", default="music_v1"); p.add_argument("--seed", type=int); p.set_defaults(fn=cmd_music)
    a = ap.parse_args()
    try:
        sys.exit(a.fn(a))
    except ElevenError as e:
        print("✗ " + str(e), file=sys.stderr)
        sys.exit(e.code)


if __name__ == "__main__":
    main()
