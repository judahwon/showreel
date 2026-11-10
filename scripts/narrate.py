#!/usr/bin/env python3
"""script.json 의 나레이션 줄을 ElevenLabs 로 생성하고(글자 단위 타임스탬프 포함), STT 로 발음을 검증한다.

사용법
  narrate.py <project>/script.json [--stt] [--only n03,n07] [--force] [--workers 2]

script.json (나레이션·큐의 단일 원천 — timeline.py 도 이 파일을 읽는다)
{
  "duration": 60,
  "voice": "ibCGc01503OQd2R6i1n1", "model": "eleven_multilingual_v2",
  "settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.4, "speed": 1.0},
  "lines": [
    {"id": "n01", "text": "1993년, KBS 예능 PD.",          ← 화면·자막 표기
              "tts":  "천구백구십삼 년, 케이비에스 예능 피디.",  ← 발음 표기(숫자·약어를 한글로). 없으면 text
              "at": 3.2,                                    ← 말이 시작될 시각(초). 없으면 앞 줄 끝 + gap
              "cues": {"kbs": "케이비에스"},                  ← tts 안의 부분 문자열 → 그 단어가 발음되는 시각이 큐가 된다
              "takes": 3, "seed": 11, "model": "...", "settings": {...},  ← 선택: 고유명사 줄은 takes 로 여러 번 뽑아 STT 로 고른다
              "voice": "4JJwo477JUAx3HV0T7n7"}                        ← 선택: 이 줄만 다른 보이스(보이스 교차)
  ],
  "sfx": [...], "music": {...}   ← timeline.py·mix.py 가 쓴다
}

출력: <project>/audio/vo/<id>.mp3, <id>.align.json, report.json
  --stt   줄마다 Scribe 로 받아써서 기대 문장(text·tts 중 가까운 쪽)과 유사도를 낸다. 0.85 미만은 ✗ 로 표시 → 문구·끊어 읽기를 고친다.
  takes   N 개 시드로 생성해 STT 유사도가 가장 높은 테이크를 <id>.mp3 로 쓴다(나머지는 <id>.t2.mp3 …로 남김).
바뀐 줄만 다시 만든다(텍스트·보이스·모델·설정·시드가 같으면 건너뜀). 크레딧을 아끼려면 --only 를 쓴다.
"""
import _utf8  # noqa: F401  (Windows 에서 UTF-8 모드로 다시 실행)
import argparse
import difflib
import hashlib
import itertools
import json
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eleven  # noqa: E402


def norm(s):
    s = s.lower()
    s = re.sub(r"\[[^\]]*\]", "", s)              # v3/v4 오디오 태그 제거
    s = re.sub(r"[\s\.,!?·…'\"“”‘’()\-–—:;~]", "", s)
    return s


def similarity(stt_text, line):
    """STT 는 '쇼릴 2026'·'AI 에이전트'처럼 발음 표기와 화면 표기를 섞어 적는다.
    화면 표기와 발음 표기의 어절을 정렬해 서로 다른 덩어리마다 둘 중 하나를 고른 조합까지 후보로 비교한다."""
    text, say = line.get("text", ""), line.get("tts", "") or line.get("text", "")
    cands = [text, say] + line.get("alt", [])
    tw, sw = text.split(), say.split()
    blocks = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, [norm(w) for w in tw], [norm(w) for w in sw]).get_opcodes():
        if op == "equal":
            blocks.append((" ".join(tw[i1:i2]),))
        elif i2 - i1 == j2 - j1:   # 어절 수가 같으면 어절마다 따로 고른다('쇼릴 2026')
            blocks += [(tw[i], sw[j]) for i, j in zip(range(i1, i2), range(j1, j2))]
        else:
            blocks.append((" ".join(tw[i1:i2]), " ".join(sw[j1:j2])))
    if 1 < len(blocks) <= 14:
        cands += [" ".join(c) for c in itertools.islice(itertools.product(*blocks), 4096)]
    got = norm(stt_text)
    return max(difflib.SequenceMatcher(None, got, norm(c)).ratio() for c in cands if c)


def digits_ok(stt_text, line):
    """숫자는 한 글자만 틀려도 뜻이 바뀐다(2026→2025). STT 가 숫자로 적었다면 화면 표기의 숫자와 정확히 같아야 한다."""
    got = re.findall(r"\d+", stt_text.replace(",", ""))
    if not got:
        return True   # STT 가 한글로 받아 적은 경우는 유사도로만 판단
    want = re.findall(r"\d+", (line.get("text", "") + " " + " ".join(line.get("alt", []))).replace(",", ""))
    return all(g in want for g in got)


def judge(stt_text, line):
    sim = similarity(stt_text, line)
    return sim, (sim >= 0.85 and digits_ok(stt_text, line))


def speech_span(al, dur):
    chars = al.get("characters") or []
    st = al.get("character_start_times_seconds") or []
    en = al.get("character_end_times_seconds") or []
    # 마침표·쉼표 글자는 뒤따르는 무음까지 길게 잡히므로 글자(한글·영숫자)만으로 발화 구간을 정한다
    idx = [i for i, c in enumerate(chars) if c.isalnum()]
    if not idx:
        return 0.0, dur
    return float(st[idx[0]]), float(en[idx[-1]])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("script")
    ap.add_argument("--stt", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args()

    sp = Path(a.script).resolve()
    proj = sp.parent
    cfg = json.loads(sp.read_text())
    lines = cfg["lines"]
    out = proj / "audio" / "vo"
    out.mkdir(parents=True, exist_ok=True)
    only = set(a.only.split(",")) if a.only else None
    ids = [l["id"] for l in lines]
    if len(ids) != len(set(ids)):
        sys.exit("✗ 줄 id 가 중복됐다")
    for l in lines:
        if not (l.get("tts") or l.get("text", "")).strip():
            sys.exit(f"✗ {l['id']}: 텍스트가 비어 있다")
    rep_path = out / "report.json"
    report = json.loads(rep_path.read_text()) if rep_path.exists() else {}

    def job(i):
        l = lines[i]
        lid = l["id"]
        say = (l.get("tts") or l["text"]).strip()
        voice = l.get("voice", cfg.get("voice", eleven.DEFAULT_VOICE))
        model = l.get("model", cfg.get("model", eleven.DEFAULT_MODEL))
        settings = {**cfg.get("settings", {}), **l.get("settings", {})}
        seed = l.get("seed", cfg.get("seed"))
        takes = int(l.get("takes", 1))
        # 앞뒤 문맥은 같은 보이스의 줄만 넘긴다(보이스를 교차할 때 다른 화자의 억양이 섞이지 않게)
        same = [x for x in lines if x.get("voice", cfg.get("voice", eleven.DEFAULT_VOICE)) == voice]
        k = next(j for j, x in enumerate(same) if x["id"] == lid)
        prev_t = " ".join((x.get("tts") or x["text"]) for x in same[max(0, k - 2):k]) or None
        next_t = (same[k + 1].get("tts") or same[k + 1]["text"]) if k + 1 < len(same) else None
        sig = hashlib.sha1(json.dumps([say, voice, model, settings, seed, takes, a.stt], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
        mp3 = out / f"{lid}.mp3"
        if only is not None and lid not in only and mp3.exists():
            return lid, report.get(lid, {}), "skip"
        if not a.force and mp3.exists() and report.get(lid, {}).get("sig") == sig:
            r = report[lid]
            alf = mp3.with_suffix(".align.json")
            if alf.exists():   # 정렬 해석 규칙이 바뀌어도 크레딧 없이 다시 계산되게
                s0, s1 = speech_span(json.loads(alf.read_text()), r.get("dur", 0))
                r.update({"speech_start": round(s0, 3), "speech_end": round(s1, 3),
                          "rate": round(r.get("chars", 0) / max(0.1, s1 - s0), 2)})
                if r.get("stt"):
                    sim, okv = judge(r["stt"], l)
                    r["sim"], r["ok"] = round(sim, 3), okv
            return lid, r, "skip"
        cands = []
        for k in range(takes):
            f = mp3 if k == 0 else out / f"{lid}.t{k + 1}.mp3"
            sd = (seed if seed is not None else 1000 + i * 17) + k if (takes > 1 or seed is not None) else None
            al = eleven.tts(say, f, voice=voice, model=model, settings=settings, seed=sd, previous_text=prev_t, next_text=next_t)
            dur = eleven.duration(f)
            s0, s1 = speech_span(al, dur)
            info = {"file": f.name, "seed": sd, "dur": round(dur, 3), "speech_start": round(s0, 3), "speech_end": round(s1, 3)}
            if a.stt or takes > 1:
                try:
                    st = eleven.stt(str(f))
                    info["stt"] = st.get("text", "")
                    sim, okv = judge(info["stt"], l)
                    info["sim"], info["ok"] = round(sim, 3), okv
                except eleven.ElevenError as e:
                    info["stt"], info["sim"] = f"(STT 실패: {e})", None
            cands.append((f, info))
        best_f, best = max(cands, key=lambda c: (bool(c[1].get("ok")), c[1].get("sim") or 0))
        if best_f != mp3:   # 가장 정확한 테이크를 본 파일로
            shutil.copy2(mp3, out / f"{lid}.t1.mp3")
            shutil.copy2(mp3.with_suffix(".align.json"), out / f"{lid}.t1.align.json")
            shutil.copy2(best_f, mp3)
            shutil.copy2(best_f.with_suffix(".align.json"), mp3.with_suffix(".align.json"))
            best = {**best, "file": mp3.name, "picked": best_f.name}
            cands[0] = (out / f"{lid}.t1.mp3", {**cands[0][1], "file": f"{lid}.t1.mp3"})   # 첫 테이크는 t1 로 옮겨졌다
        chars = len(re.findall(r"[0-9A-Za-z가-힣]", re.sub(r"\[[^\]]*\]", "", say)))   # 글자만 센다(문장부호·공백 제외)
        best = dict(best)   # 후보 목록을 담을 때 자기 자신을 참조하지 않도록 복사
        best.update({"sig": sig, "text": l["text"], "tts": say, "chars": chars,
                     "rate": round(chars / max(0.1, best["speech_end"] - best["speech_start"]), 2),
                     "takes": [dict(c[1]) for c in cands] if takes > 1 else None})
        return lid, best, "new"

    todo = list(range(len(lines)))
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:   # TTS 동시 요청은 플랜별 2~5 — 2가 안전
        results = list(ex.map(job, todo))
    bad = []
    total_speech = 0.0
    total_chars = 0
    print(f"{'id':6s} {'상태':4s} {'길이':>6s} {'발화':>6s} {'자/초':>5s} {'STT':>5s}  텍스트")
    for lid, r, state in results:
        report[lid] = r
        sp_len = r.get("speech_end", 0) - r.get("speech_start", 0)
        total_speech += sp_len
        total_chars += r.get("chars", 0)
        sim = r.get("sim")
        okv = r.get("ok", sim is not None and sim >= 0.85)
        mark = "" if sim is None else ("✓" if okv else "✗")
        if sim is not None and not okv:
            bad.append(lid)
        print(f"{lid:6s} {state:4s} {r.get('dur', 0):6.2f} {sp_len:6.2f} {r.get('rate', 0):5.1f} {('' if sim is None else f'{sim:.2f}'):>5s}{mark} {r.get('text', '')}")
        if sim is not None and not okv:
            print(f"       STT: {r.get('stt')}")
    rep_path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"\n발화 합계 {total_speech:.1f}s · {total_chars}자(글자만, 공백·문장부호 제외) · 평균 {total_chars / max(0.1, total_speech):.1f}자/초")
    if cfg.get("duration"):
        d = float(cfg["duration"])
        print(f"목표 길이 {d:g}s 대비 발화 비율 {total_speech / d * 100:.0f}% (쇼릴 권장 65~75%, 교육용 85~90%)")
    if bad:
        print(f"✗ 발음 확인 필요: {', '.join(bad)} → 끊어 읽기(쉼표·말줄임표), 숫자·약어 한글 표기, 문구 변경, takes 늘리기, 모델 교체(v2↔v3) 순으로 시도")
    print(f"→ 다음: python3 {Path(__file__).parent}/timeline.py {sp}")


if __name__ == "__main__":
    main()
