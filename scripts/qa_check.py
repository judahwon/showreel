#!/usr/bin/env python3
"""완성 영상 자동 검수 → qa/qa_report.md + 간격 콘택트 시트. 시트는 반드시 Read 로 직접 열어 본다.

사용법
  qa_check.py out/<name>.mp4 [--timeline timeline.json] [--script script.json] [--stt] [--qa qa]

검사 항목
  규격     해상도·fps·프레임 수·길이·pix_fmt(yuv420p)·color_range(tv)·오디오 유무
  음량     Integrated -14±1 LUFS, true peak ≤ -1 dBFS, LRA
  화면     빈 어두운 구간(평균 휘도<12 이고 밝은 요소도 없는 프레임이 0.5초 이상 — 의도한 암전이면 무시),
           섬광(화면 10% 이상이 밝기 0.1 넘게 오르내리는 변화가 1초에 3회 초과), 하얗게 날아간 프레임
  움직임   멈춘 화면 비율·1초 넘는 멈춤·큰 움직임(motion_check.py). 그레인이 있어도 멈춤을 잡는다
  나레이션 --stt: 완성 믹스를 Scribe 로 받아써서 줄마다 기대 문장과 비교(음악·효과음에 묻힌 줄을 찾는다).
           한 번 틀린 줄은 STT 의 변덕일 수 있으니 한 번 더 돌려 두 번 다 틀릴 때만 고친다.
  시트     길이에 맞춰 0.5~3초 간격 콘택트 시트(qa/final_sheet*.png)
"""
import _utf8  # noqa: F401  (Windows 에서 UTF-8 모드로 다시 실행)
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--timeline")
    ap.add_argument("--script")
    ap.add_argument("--stt", action="store_true")
    ap.add_argument("--qa", default=None)
    a = ap.parse_args()
    vid = Path(a.video).resolve()
    proj = vid.parent.parent if vid.parent.name == "out" else vid.parent
    qa = Path(a.qa) if a.qa else proj / "qa"
    qa.mkdir(parents=True, exist_ok=True)
    rep, issues = [], []

    # 규격
    d = json.loads(sh(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                       "stream=codec_type,codec_name,width,height,pix_fmt,color_range,r_frame_rate,nb_read_frames,sample_rate,channels:format=duration,size",
                       "-of", "json", str(vid)]).stdout)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    au = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    dur = float(d["format"]["duration"])
    num, den = v["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    rep.append(f"- 규격: {v['width']}x{v['height']} {fps:g}fps {v.get('nb_read_frames')}프레임 {dur:.2f}초 {v.get('pix_fmt')}/{v.get('color_range')} "
               f"{'· 오디오 ' + au['codec_name'] + ' ' + au['sample_rate'] + 'Hz' if au else '· 오디오 없음'} · {int(d['format']['size']) / 1e6:.1f}MB")
    if v.get("pix_fmt") != "yuv420p":
        issues.append(f"pix_fmt {v.get('pix_fmt')} — yuv420p 가 아니면 일부 플레이어·SNS 에서 색이 틀어진다")
    if v.get("color_range") == "pc":
        issues.append("color_range=pc(풀레인지) — render.mjs 로 다시 렌더하거나 finish.py 를 거칠 것")
    if not au:
        issues.append("오디오 트랙 없음")

    # 음량
    if au:
        r = sh(["ffmpeg", "-hide_banner", "-nostats", "-i", str(vid), "-af", "ebur128=peak=true", "-f", "null", "-"]).stderr
        s = r[r.rfind("Summary:"):]
        g = lambda k: float(re.search(k + r":\s+(-?[\d.]+)", s).group(1))
        I, LRA, TP = g("I"), g("LRA"), g("Peak")
        rep.append(f"- 음량: I {I} LUFS · LRA {LRA} · true peak {TP} dBFS")
        if not -15 <= I <= -13:
            issues.append(f"음량 {I} LUFS — 목표 -14±1 (mix.py 로 다시 마스터링)")
        if TP > -1.0:
            issues.append(f"true peak {TP} dBFS — -1 이하로(mix.py 기본 --tp -2.0 으로 다시 마스터링 → finish.py)")

    # 화면: 휘도(어두운 구간·흰 프레임)와 섬광(면적 기준)
    import numpy as np
    W_, H_ = 480, 270
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(vid), "-vf", f"scale={W_}:{H_},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    fr = np.frombuffer(raw, dtype=np.uint8)
    nfr = len(fr) // (W_ * H_)
    if nfr > 1:
        v = fr[: nfr * W_ * H_].reshape(nfr, H_, W_).astype(np.float32) / 255.0
        lin = np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)
        yavg = v.mean(axis=(1, 2)) * 255
        # 어두운 '빈' 화면: 평균이 어둡고 눈에 띄는 밝은 요소(글자·그래픽)도 거의 없는 프레임. 어두운 무드의 정상 장면은 빼기 위해서다
        lit = (v > 0.25).mean(axis=(1, 2))
        empty = (yavg < 12) & (lit < 0.002)
        dark, run0 = [], None
        for i, val in enumerate(list(empty) + [False]):
            if val and run0 is None:
                run0 = i
            elif not val and run0 is not None:
                if (i - run0) / fps >= 0.5:
                    dark.append((run0 / fps, i / fps))
                run0 = None
        # 흰 프레임: 어두운 화면 사이에 0.3초 안쪽으로 튀는 밝은 프레임(과한 플래시)만 센다.
        # 밝은 바탕으로 디자인한 구간(오래 이어지거나 서서히 밝아진 구간)은 결함이 아니므로 참고로만 적는다
        white, light_runs, run0 = 0, [], None
        bright = yavg > 225
        for i, val in enumerate(list(bright) + [False]):
            if val and run0 is None:
                run0 = i
            elif not val and run0 is not None:
                seg = float(yavg[run0:i].mean())
                before = float(yavg[run0 - 1]) if run0 > 0 else seg
                after = float(yavg[i]) if i < nfr else seg
                if (i - run0) / fps < 0.3 and seg - before > 60 and seg - after > 60:
                    white += i - run0
                else:
                    light_runs.append((run0 / fps, i / fps))
                run0 = None
        # 인접 프레임에서 밝기가 0.1 넘게 오른(내린) 화소가 화면 10% 이상이면 전이. 반대 방향 전이 두 번 = 섬광 1회
        events = []
        for i in range(1, nfr):
            dd = lin[i] - lin[i - 1]
            up, dn = float((dd >= 0.1).mean()), float((dd <= -0.1).mean())
            if up >= 0.10:
                events.append((i, 1))
            if dn >= 0.10:
                events.append((i, -1))
        flashes, last = [], None
        for f, sgn in events:
            if last is not None and sgn != last:
                flashes.append(f)
                last = None
            else:
                last = sgn
        bad = sorted({f for f in flashes if sum(1 for x in flashes if f <= x < f + fps) > 3})
        rep.append(f"- 화면: 평균 밝기 {yavg.mean():.0f}, 어두운 구간 {len(dark)}개, 큰 밝기 전이 {len(events)}회, 섬광 {len(flashes)}회, 흰 프레임 {white}개")
        for s0, s1 in dark:
            rep.append(f"  - 어두운 구간 {s0:.2f}~{s1:.2f}s (의도한 암전·콜드 오픈이 아니면 확인)")
        for s0, s1 in light_runs[:6]:
            rep.append(f"  - 밝은 구간 {s0:.2f}~{s1:.2f}s (밝은 바탕으로 디자인한 구간이면 정상)")
        if bad:
            issues.append(f"섬광 과다: {bad[0] / fps:.2f}s 부근 1초에 섬광 3회 초과(화면 10% 이상이 밝기 0.1 넘게 오르내림) — 광과민성 위험. "
                          "동시에 바뀌는 면적을 나누거나 색 반전 대신 위치·크기로 박을 표현할 것")
        if white > fps * 0.3:
            issues.append(f"하얗게 튀는 프레임 {white}개(어두운 화면 사이 0.3초 안쪽) — 플래시 세기를 0.15 이하로")
    # 움직임: 멈춘 화면 비율·1초 넘는 멈춤·큰 움직임(그레인을 걸러 낸 64×36 프레임 기준 — motion_check.py 와 같다)
    import motion_check
    tl_json = json.loads(Path(a.timeline).read_text()) if a.timeline and Path(a.timeline).exists() else None
    mm = motion_check.measure(str(vid))
    rep.append(f"- 움직임: 멈춘 화면 {mm['still_ratio'] * 100:.0f}% · 큰 움직임 p90 {mm['p90']:.1f} · 1초 넘는 멈춤 {len(mm['still_runs'])}곳 (마지막 3초 제외)")
    rep.append("  - 초별: " + motion_check.spark(mm["per_second"]))
    for x in motion_check.verdict(mm):
        for s0, s1 in mm["still_runs"]:
            if x.startswith(f"{s0:.1f}~") and motion_check.scene_at(tl_json, s0):
                x += f" [장면 {motion_check.scene_at(tl_json, s0)}]"
        issues.append(x)

    # STT
    if a.stt and au and a.timeline:
        import eleven
        import narrate
        tl = json.loads(Path(a.timeline).read_text())
        src = {l["id"]: l for l in json.loads(Path(a.script).read_text())["lines"]} if a.script else {}
        mp3 = qa / "final_audio.mp3"
        sh(["ffmpeg", "-v", "error", "-y", "-i", str(vid), "-vn", "-ac", "1", "-ar", "22050", "-b:a", "96k", str(mp3)])
        res = eleven.stt(str(mp3))
        words = [w for w in res.get("words", []) if w.get("type") == "word"]
        rep.append("- 완성 믹스 STT (줄별 유사도 — 0.85 미만이거나 숫자가 다르면 ✗):")
        for line in tl.get("vo", []):
            got = " ".join(w["text"] for w in words if line["start"] - 0.35 <= w["start"] <= line["end"] + 0.2)
            ref = src.get(line["id"], {"text": line["text"], "tts": line.get("text")})
            sim, okv = narrate.judge(got, ref)
            mark = "✓" if okv else "✗"
            rep.append(f"  - {line['id']} {sim:.2f}{mark} 기대「{line['text']}」 들림「{got}」")
            if not okv:
                issues.append(f"나레이션 {line['id']} 이 믹스에서 다르게 들림(STT「{got}」) — 같은 결과가 두 번 나오면: 그 구간 효과음을 줄이거나 비키고, 줄 gain_db +2~3, 또는 다시 녹음")

    # 시트
    step = 0.5 if dur <= 20 else 1 if dur <= 75 else 2 if dur <= 150 else 3
    cs = sh([sys.executable, str(HERE / "contact_sheet.py"), "--video", str(vid), "--every", str(step), "--out", str(qa / "final_sheet.png"),
             "--cols", "6", "--thumb", "320", "--per", "36"] + (["--timeline", a.timeline] if a.timeline else []))
    sheets = [x for x in cs.stdout.split() if x.endswith(".png")]
    if not sheets:
        issues.append("콘택트 시트를 만들지 못했다: " + (cs.stderr.strip().splitlines() or ["원인 불명"])[-1][:160])
    rep.append(f"- 콘택트 시트({step}초 간격): " + ", ".join(sheets))

    md = [f"# 자동 검수 — {vid.name}", "", *rep, "", "## 고칠 것" if issues else "## 자동 검사 통과", *[f"- [ ] {x}" for x in issues],
          "", "## 사람이 볼 것(자동 검사로 못 잡음)",
          "- [ ] 시트에서: 글자 잘림·겹침, 자막과 그래픽 겹침, 빈 화면, 장면 누수, 한글 폰트 대체(기본 고딕), 오탈자·숫자",
          "- [ ] 첫 3초의 훅, 컷이 음악 히트에 맞는지, 엔드카드·CTA 가 충분히 크고 오래 보이는지",
          "- [ ] 소리는 Claude 가 들을 수 없다 — 사용자에게 스피커·이어폰으로 들어 봐 달라고 요청"]
    (qa / "qa_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    print(f"\n→ {qa / 'qa_report.md'}")


if __name__ == "__main__":
    main()
