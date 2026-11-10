#!/usr/bin/env python3
"""컷으로 나눈 영상을 고치고 판을 쌓는 명령. 수정 요청이 여러 번 오가는 시연·설명 영상에 쓴다.

폴더 구조
  <root>/project/    작업 소스 한 벌(판마다 복사하지 않는다). cuts.json 이 시각의 단일 원천
  <root>/cache/      컷 클립·믹스 결과·검수 시트. 지워도 다시 만들 수 있다
  <root>/versions/   판마다 납품 파일과 기록만(mp4, srt, storyboard.md, feedback.md, manifest.json)
  <root>/.store/     판의 소스를 내용 해시로 저장한다. 같은 파일은 판이 몇 개든 한 번만 저장된다

명령
  reel.py init <root>                     새 구조를 만든다
  reel.py add-cut c01 --length 12         컷 페이지·장면 파일을 만들고 cuts.json 에 더한다
  reel.py build [--tts]                   cuts.json → timeline_<컷>.js, timeline.json, 자막, 음악 스펙
  reel.py render [컷…] [--draft] [--adopt] 바뀐 컷만 렌더(컷을 적으면 그 컷만 강제로). 컷마다 cache/preview/ 에 소리를 입힌 클립을 바로 만든다
  reel.py deps                            컷마다 실제로 읽는 에셋을 기록한다(render 가 컷을 렌더한 뒤 자동으로 부른다)
  reel.py mix                             오디오 믹스
  reel.py join [--draft]                  컷을 잇고 오디오를 입혀 납품 파일을 만든다
  reel.py preview [--draft]               지금 있는 클립과 mix.wav 를 검사 없이 바로 잇는다(몇 초)
  reel.py qa [--stt]                      qa_check.py + 컷 경계·효과음·자막 검사
  reel.py all [--draft] [--full]          build → mix → render → join. --full 이면 공유용 인코딩·qa 까지
  reel.py where 03:56 [--version v12]     영상 시각 → 컷·컷 안 시각·자막·큐·효과음·장면 파일
  reel.py release v13 --note "…"          지금 결과를 판으로 남긴다(공유용 파일·검수가 없거나 낡았으면 먼저 한다)
  reel.py restore v12 --to <빈 폴더>       그 판의 소스를 꺼낸다
  reel.py versions                        판 목록

cuts.json 시각 표현
  숫자 | 큐 이름 | "큐+1.2" | "큐-0.5"
  컷 안에서: cues 의 이름, length, line.<줄 id>, line.<줄 id>.end
  컷 밖(음악 스펙 등)에서: <컷>, <컷>.end, <컷>.<큐>, total
  큐끼리 이어 두면 앞을 옮길 때 뒤의 장면·자막·효과음·음악이 함께 움직인다.
  화면에서는 REEL.cue('이름') 으로 읽는다. 자세한 내용은 references/revision.md.
"""
import _utf8  # noqa: F401
import argparse
import datetime
import fnmatch
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent / "assets" / "engine"
SNAPSHOT_EXCLUDE = [".env", "**/.env", "reel/timeline_*.js", "timeline.json", "audio/mix*.wav", "**/__pycache__/*", "**/*.parts/*"]
EXPRESSION = re.compile(r"^\s*([^\s+]+?)\s*(?:([+-])\s*([\d.]+))?\s*$")
ROUND_DIGITS = 3


class BuildError(Exception):
    pass


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def stamp(seconds):
    milliseconds = round(seconds * 1000)
    return f"{milliseconds // 3600000:02d}:{milliseconds // 60000 % 60:02d}:{milliseconds // 1000 % 60:02d},{milliseconds % 1000:03d}"


def parse_time(text):
    """'03:56', '1:02:03', '236.5' → 초"""
    seconds = 0.0
    for part in str(text).split(":"):
        seconds = (seconds * 60) + float(part)
    return seconds


def clock(seconds):
    return f"{int(seconds // 60):02d}:{seconds % 60:05.2f}"


class Timing:
    """cuts.json 의 시각 표현을 푼다. 큐끼리 참조할 수 있고, 순환은 오류로 알린다."""

    def __init__(self, spec, durations):
        self.spec = spec
        self.durations = durations
        self.tail = float(spec.get("narration", {}).get("tail", 0.35))
        self.pad = float(spec.get("narration", {}).get("pad", 0.2))
        self.cuts = {cut["id"]: cut for cut in spec["cuts"]}
        self.lines = {line["id"]: line for line in spec.get("lines", [])}
        self.memo = {}
        self.stack = []
        self.fps = int(spec.get("fps", 30))
        self.offsets = {}
        self.start_frames = {}
        self.frames = {}
        # 렌더러는 컷마다 프레임 수로 끊으므로, 시각도 프레임 수를 더해 계산해야 컷이 많아져도 소리와 어긋나지 않는다
        total_frames = 0
        for cut in spec["cuts"]:
            cut_id = cut["id"]
            self.start_frames[cut_id] = total_frames
            self.offsets[cut_id] = round(total_frames / self.fps, ROUND_DIGITS)
            self.frames[cut_id] = round(self.local(cut_id, "length") * self.fps)
            total_frames += self.frames[cut_id]
        self.total_frames = total_frames
        self.total = round(total_frames / self.fps, ROUND_DIGITS)

    def cut_end(self, cut_id):
        return round((self.start_frames[cut_id] + self.frames[cut_id]) / self.fps, ROUND_DIGITS)

    def line_cut(self, line):
        return line.get("cut") or line["id"].rsplit("_", 1)[0]

    def speech_end(self, line_id):
        line = self.lines[line_id]
        cut = self.line_cut(line)
        if line_id not in self.durations:
            raise BuildError(f"{line_id}: 나레이션 길이가 없다(reel.py build --tts 또는 durations.json 확인)")
        return round(self.expression(cut, line["start"]) + self.durations[line_id] - self.tail + self.pad, 2)

    def local(self, cut_id, name):
        """컷 안의 이름 하나를 초로 푼다"""
        key = (cut_id, name)
        if key in self.memo:
            return self.memo[key]
        if key in self.stack:
            raise BuildError("큐가 서로를 참조한다: " + " → ".join(f"{c}.{n}" for c, n in [*self.stack, key]))
        self.stack.append(key)
        try:
            cut = self.cuts[cut_id]
            if name == "length":
                value = math.floor((self.expression(cut_id, cut["length"]) * self.fps) + 0.5) / self.fps   # 가장 가까운 프레임
            elif name in (cut.get("cues") or {}):
                value = self.expression(cut_id, cut["cues"][name])
            elif name.startswith("line.") and name.endswith(".end") and name[5:-4] in self.lines:
                value = self.speech_end(name[5:-4])
            elif name.startswith("line.") and name[5:] in self.lines:
                value = self.expression(self.line_cut(self.lines[name[5:]]), self.lines[name[5:]]["start"])
            else:
                raise BuildError(f"{cut_id}: 큐 '{name}' 가 없다")
        finally:
            self.stack.pop()
        self.memo[key] = round(value, ROUND_DIGITS)
        return self.memo[key]

    def split(self, expression):
        if isinstance(expression, (int, float)):
            return None, float(expression)
        match = EXPRESSION.match(str(expression))
        if not match:
            raise BuildError(f"시각 표현을 읽을 수 없다: '{expression}'")
        name, sign, amount = match.groups()
        shift = (float(amount) * (1 if sign == "+" else -1)) if amount else 0.0
        try:
            return None, float(name) + shift
        except ValueError:
            return name, shift

    def expression(self, cut_id, expression):
        """컷 안의 표현 → 컷 안 시각"""
        name, shift = self.split(expression)
        return shift if name is None else self.local(cut_id, name) + shift

    def absolute(self, expression):
        """컷 밖의 표현 → 영상 전체 시각. <컷>, <컷>.end, <컷>.<큐>, total"""
        name, shift = self.split(expression)
        if name is None:
            return round(shift, ROUND_DIGITS)
        if name == "total":
            return round(self.total + shift, ROUND_DIGITS)
        cut_id, _, rest = name.partition(".")
        if cut_id not in self.cuts:
            raise BuildError(f"컷 '{cut_id}' 가 없다: '{expression}'")
        if not rest:
            base = self.offsets[cut_id]
        elif rest == "end":
            base = self.cut_end(cut_id)
        else:
            base = self.offsets[cut_id] + self.local(cut_id, rest)
        return round(base + shift, ROUND_DIGITS)


class Project:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.work = self.root / "project"
        self.cache = self.root / "cache"
        self.versions = self.root / "versions"
        self.store = self.root / ".store"
        self.spec_path = self.work / "cuts.json"
        if not self.spec_path.exists():
            raise SystemExit(f"{self.spec_path} 가 없다. reel.py init <root> 로 만들거나 --root 를 지정한다")
        self.spec = read_json(self.spec_path)

    @staticmethod
    def find(explicit):
        if explicit:
            return Project(explicit)
        here = Path.cwd().resolve()
        for folder in [here, *here.parents]:
            if (folder / "project" / "cuts.json").exists():
                return Project(folder)
        raise SystemExit("project/cuts.json 을 찾지 못했다. --root 를 지정한다")

    def narration(self):
        conf = self.spec.get("narration") or {}
        durations, script = {}, {}
        if conf.get("durations") and (self.work / conf["durations"]).exists():
            for line_id, item in read_json(self.work / conf["durations"]).items():
                durations[line_id] = item["seconds"] if isinstance(item, dict) else float(item)
        if conf.get("script") and (self.work / conf["script"]).exists():
            script = {line["id"]: line for line in read_json(self.work / conf["script"])["lines"]}
        return durations, script

    def timing(self):
        return Timing(self.spec, self.narration()[0])

    def clip(self, cut_id, draft=False):
        return self.cache / "clips" / f"{cut_id}_{'draft' if draft else 'final'}.mp4"

    def final_name(self, draft=False):
        return f"{self.spec.get('title', self.root.name)}{'_draft' if draft else ''}"

    def page_files(self, cut):
        """컷 페이지가 불러오는 파일(html 자신 + script·link). 다른 컷의 timeline 은 들어오지 않는다"""
        reel = self.work / "reel"
        page = reel / cut.get("page", f"{cut['id']}.html")
        found = [page]
        for ref in re.findall(r'(?:src|href)="([^"]+)"', page.read_text(encoding="utf-8")):
            path = (reel / ref).resolve()
            if path.is_file():
                found.append(path)
        return found

    @staticmethod
    def dep_patterns(cut):
        """deps 는 사람이 적는 목록, deps_auto 는 reel.py deps 가 적는 목록이다. 둘 다 없으면(아직 기록 전) 모든 에셋을 본다"""
        if "deps" not in cut and "deps_auto" not in cut:
            return ["assets/**/*"]
        return [*cut.get("deps", []), *cut.get("deps_auto", [])]

    def matching_files(self, pattern):
        # Python 3.13 전의 glob 은 끝이 ** 면 폴더만 돌려준다
        expanded = f"{pattern}/*" if pattern.endswith("**") else pattern
        return [path for path in self.work.glob(expanded) if path.is_file()]

    def cut_hash(self, cut, draft=False):
        """컷 화면을 결정하는 파일의 해시. 같으면 다시 렌더하지 않는다"""
        digest = hashlib.sha256(f"{'draft' if draft else 'final'}".encode())
        files = set(self.page_files(cut))
        for pattern in self.dep_patterns(cut):
            files.update(self.matching_files(pattern))
        for path in sorted(files):
            digest.update(path.relative_to(self.work).as_posix().encode())
            digest.update(sha256_file(path).encode())
        return digest.hexdigest()[:24]

    def renders(self):
        path = self.cache / "renders.json"
        return read_json(path) if path.exists() else {}


def collect_lines(project, timing, script, durations):
    errors, rows = [], []
    lines = project.spec.get("lines", [])
    for index, line in enumerate(lines):
        cut = timing.line_cut(line)
        start = timing.expression(cut, line["start"])
        end = timing.speech_end(line["id"])
        following = lines[index + 1] if index + 1 < len(lines) and timing.line_cut(lines[index + 1]) == cut else None
        limit = timing.expression(cut, following["start"]) if following else timing.local(cut, "length")
        text = (script.get(line["id"]) or {}).get("text", line.get("text", ""))
        if end > limit + 0.01:
            errors.append(f"{line['id']}: 자막이 {end:.2f}s 에 끝나 다음 시작 {limit:.2f}s 를 넘긴다 — 큐를 늦추거나 문장을 줄인다")
        rows.append({"id": line["id"], "cut": cut, "start": start, "end": end, "text": text, "seconds": durations.get(line["id"], 0.0),
                     "gain_db": line.get("gain_db", 0), "absolute": round(timing.offsets[cut] + start, ROUND_DIGITS)})
    return rows, errors


def collect_sfx(project, timing):
    errors, rows = [], []
    for item in project.spec.get("sfx", []):
        cut = item["cut"]
        at = timing.expression(cut, item["at"])
        if at < 0 or at > timing.local(cut, "length"):
            errors.append(f"효과음 {item['name']}: {cut} 의 {at:.2f}s 는 컷 밖이다")
        relative = project.work / "audio" / "sfx"
        file = next((f"audio/sfx/{item['name']}{suffix}" for suffix in (".wav", ".mp3") if (relative / f"{item['name']}{suffix}").exists()),
                    f"audio/sfx/{item['name']}.wav")
        rows.append({"name": item["name"], "file": file, "t": round(timing.offsets[cut] + at, ROUND_DIGITS), "cut": cut, "at": at,
                     "cue": item["at"] if isinstance(item["at"], str) else None, "gain_db": item.get("gain_db", -6),
                     "align": item.get("align", "start"), "ambient": bool(item.get("ambient"))})
    return rows, errors


def build_music(project, timing):
    """음악 스펙의 큐 시각을 숫자로 풀어 쓴다. 풀린 스펙이 바뀌었을 때만 합성 음악을 다시 만든다"""
    conf = project.spec.get("music") or {}
    if not conf.get("spec"):
        return conf
    spec = read_json(project.work / conf["spec"])
    if isinstance(spec.get("duration"), str):
        spec["duration"] = timing.absolute(spec["duration"])
    for section in spec.get("sections", []):
        section["start"] = timing.absolute(section["start"])
    for event in spec.get("events", []):
        event["t"] = timing.absolute(event["t"])
    resolved = project.work / Path(conf["spec"]).with_suffix(".resolved.json")
    write_json(resolved, spec)
    music = project.work / conf["file"]
    stamp_path = music.with_suffix(".spec_hash")
    current = sha256_file(resolved)
    previous = stamp_path.read_text(encoding="utf-8").strip() if stamp_path.exists() else None
    if previous == current and music.exists():
        return conf
    if conf.get("engine") == "synth":
        print("음악 스펙이 바뀌어 합성 음악을 다시 만든다")
        subprocess.run([sys.executable, str(SCRIPTS / "synth_music.py"), "music", str(resolved), str(music)], check=True)
        stamp_path.write_text(current + "\n", encoding="utf-8")
    else:
        print(f"△ 음악 스펙이 바뀌었다: {resolved} 로 음악을 다시 만들고 {stamp_path.name} 에 해시를 적는다")
    return conf


def command_build(project, args):
    conf = project.spec.get("narration") or {}
    if args.tts and conf.get("make"):
        subprocess.run([sys.executable, str(project.work / conf["make"])], cwd=project.work, check=True)
    durations, script = project.narration()
    timing = Timing(project.spec, durations)
    lines, errors = collect_lines(project, timing, script, durations)
    sfx, sfx_errors = collect_sfx(project, timing)
    errors += sfx_errors
    reel = project.work / "reel"
    for cut in project.spec["cuts"]:
        cut_id = cut["id"]
        length = timing.local(cut_id, "length")
        cues = {"length": length, **{name: timing.local(cut_id, name) for name in (cut.get("cues") or {})}}
        own = [line for line in lines if line["cut"] == cut_id]
        for line in own:
            cues[f"line.{line['id']}"] = line["start"]
            cues[f"line.{line['id']}.end"] = line["end"]
        page_timeline = {"duration": length, "cues": cues, "scenes": [], "vo": [
            {"id": line["id"], "text": line["text"], "sub": line["text"], "nosub": False, "start": line["start"], "end": line["end"], "words": []} for line in own]}
        narration = {cut_id: [[line["start"], min(line["end"], round(length - 0.1, ROUND_DIGITS)), line["text"]] for line in own]}
        (reel / f"timeline_{cut_id}.js").write_text(
            f"// reel.py build 가 만든다. 직접 고치지 말고 cuts.json 을 고친다\nwindow.TL = {json.dumps(page_timeline, ensure_ascii=False)};\n"
            f"const NARRATION = {json.dumps(narration, ensure_ascii=False)};\n", encoding="utf-8")
        page = reel / cut.get("page", f"{cut_id}.html")
        if not page.exists():
            errors.append(f"{cut_id}: 페이지 {page.name} 가 없다(reel.py add-cut)")
        elif f"timeline_{cut_id}.js" not in page.read_text(encoding="utf-8"):
            errors.append(f"{cut_id}: {page.name} 가 engine.js 앞에서 timeline_{cut_id}.js 를 불러오지 않는다")
        for pattern in cut.get("deps", []):
            if not project.matching_files(pattern):
                print(f"△ {cut_id}: deps 의 '{pattern}' 에 맞는 파일이 없다")
    music = build_music(project, timing)
    cue_table = {}
    for cut in project.spec["cuts"]:
        cue_table[cut["id"]] = timing.offsets[cut["id"]]
        for name in (cut.get("cues") or {}):
            cue_table[f"{cut['id']}.{name}"] = timing.absolute(f"{cut['id']}.{name}")
    music_entry = {key: value for key, value in music.items() if key not in ("spec", "engine")}
    timeline = {
        "duration": timing.total, "fps": project.spec.get("fps", 30), "cues": cue_table,
        "vo": [{"id": line["id"], "text": line["text"], "sub": line["text"], "start": line["absolute"], "end": round(timing.offsets[line["cut"]] + line["end"], ROUND_DIGITS),
                "clip": f"{conf.get('dir', 'audio/vo')}/{line['id']}.mp3", "clip_start": line["absolute"], "gain_db": line["gain_db"]} for line in lines],
        "scenes": [{"id": cut["id"], "start": timing.offsets[cut["id"]], "end": timing.cut_end(cut["id"])} for cut in project.spec["cuts"]],
        "sfx": sfx, "music": music_entry}
    write_json(project.work / "timeline.json", timeline)
    srt = [f"{index + 1}\n{stamp(line['absolute'])} --> {stamp(timing.offsets[line['cut']] + line['end'])}\n{line['text']}\n" for index, line in enumerate(lines)]
    project.cache.mkdir(parents=True, exist_ok=True)
    (project.cache / "captions.srt").write_text("\n".join(srt), encoding="utf-8")
    print(f"컷 {len(project.spec['cuts'])}개 · 자막 {len(lines)}줄 · 효과음 {len(sfx)}개 · 전체 {timing.total:.2f}초 ({clock(timing.total)})")
    limit = project.spec.get("max_duration")
    if limit and timing.total > limit:
        print(f"△ 목표 길이 {limit}초를 {timing.total - limit:.1f}초 넘겼다")
    for error in errors:
        print("✗ " + error)
    if errors:
        raise SystemExit(1)
    return timing


def cut_preview(project, timing, cut_id, draft=False):
    """컷 클립에 그 구간의 소리를 입혀 컷이 끝나자마자 볼 수 있게 한다. 영상은 복사만 하므로 1~2초면 된다"""
    clip = project.clip(cut_id, draft)
    out = project.cache / "preview" / clip.name
    out.parent.mkdir(parents=True, exist_ok=True)
    mix = project.work / "audio" / "mix.wav"
    if not mix.exists():
        shutil.copy2(clip, out)
        return out
    seconds = timing.frames[cut_id] / timing.fps
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(clip), "-ss", f"{timing.offsets[cut_id]:.3f}", "-t", f"{seconds:.3f}", "-i", str(mix),
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(out)], check=True)
    return out


def command_render(project, args):
    timing = command_build(project, argparse.Namespace(tts=False))
    state = project.renders()
    mode = "draft" if args.draft else "final"
    table = state.setdefault(mode, {})
    forced = set(args.cuts or [])
    unknown = forced - {cut["id"] for cut in project.spec["cuts"]}
    if unknown:
        raise SystemExit(f"없는 컷: {', '.join(sorted(unknown))}")
    render_js = SCRIPTS / "render.mjs"
    rendered = []
    has_mix = (project.work / "audio" / "mix.wav").exists()
    sound_note = "  (소리 없음: reel.py mix 전)" if not has_mix else "  (소리는 지난 믹스)" if mix_problems(project) else ""
    for cut in project.spec["cuts"]:
        cut_id = cut["id"]
        if forced and cut_id not in forced:
            continue
        current = project.cut_hash(cut, args.draft)
        clip = project.clip(cut_id, args.draft)
        if args.adopt:
            if not clip.exists():
                raise SystemExit(f"{clip} 가 없어 받아들일 수 없다")
            table[cut_id] = current
            continue
        if not forced and table.get(cut_id) == current and clip.exists():
            continue
        clip.parent.mkdir(parents=True, exist_ok=True)
        print(f"== {cut_id} ({mode})")
        command = ["node", str(render_js), "--dir", str(project.work / "reel"), "--page", cut.get("page", f"{cut_id}.html"), "--out", str(clip)]
        command += ["--scale", "0.5"] if args.draft else ["--sub", "4", "--format", "png"]
        if args.workers:
            command += ["--workers", str(args.workers)]
        if subprocess.run(command).returncode != 0:
            raise SystemExit(f"{cut_id} 렌더 실패(페이지 오류면 로그를 확인한다)")
        table[cut_id] = current
        rendered.append(cut_id)
        write_json(project.cache / "renders.json", state)
        print(f"   → {cut_preview(project, timing, cut_id, args.draft)}{sound_note}")
    if rendered:
        # 장면이 새 에셋을 쓰기 시작했을 수 있어 렌더한 컷의 목록만 다시 기록한다. 렌더하지 않은 컷의 목록을 바꾸면 그 컷의 해시가 바뀌어 다음에 괜히 다시 렌더된다
        command_deps(project, args, only=rendered)
        for cut in project.spec["cuts"]:
            if cut["id"] in rendered:
                table[cut["id"]] = project.cut_hash(cut, args.draft)
    write_json(project.cache / "renders.json", state)
    print("받아들임: 지금 클립을 현재 소스의 렌더로 기록했다" if args.adopt else f"렌더: {', '.join(rendered) or '바뀐 컷 없음'}")


def command_deps(project, _args, only=None):
    project.cache.mkdir(parents=True, exist_ok=True)
    result_path = project.cache / "deps.json"
    subprocess.run(["node", str(SCRIPTS / "record_deps.mjs"), str(project.work), str(result_path), *(only or [])], check=True, stdout=subprocess.DEVNULL)
    recorded = read_json(result_path)
    for cut in project.spec["cuts"]:
        if cut["id"] in recorded:
            cut["deps_auto"] = recorded[cut["id"]]
    write_json(project.spec_path, project.spec)
    print("에셋 기록: " + " · ".join(f"{cut_id} {len(files)}개" for cut_id, files in recorded.items()))


def mix_script(project):
    conf = project.spec.get("mix") or {}
    return project.work / conf["script"] if conf.get("script") else SCRIPTS / "mix.py"


def audio_fingerprint(project):
    """믹스 결과를 결정하는 입력의 해시: 시각표, 믹스 스크립트, 나레이션·효과음·음악 파일"""
    timeline_path = project.work / "timeline.json"
    timeline = read_json(timeline_path)
    files = {line["clip"] for line in timeline.get("vo", [])} | {item["file"] for item in timeline.get("sfx", [])}
    if (timeline.get("music") or {}).get("file"):
        files.add(timeline["music"]["file"])
    digest = hashlib.sha256(sha256_file(timeline_path).encode())
    digest.update(sha256_file(mix_script(project)).encode())
    for relative in sorted(files):
        path = project.work / relative
        digest.update(relative.encode())
        digest.update((sha256_file(path) if path.exists() else "missing").encode())
    return digest.hexdigest()


def write_mix_stamp(project):
    write_json(project.cache / "mix_stamp.json", {"inputs": audio_fingerprint(project), "output": sha256_file(project.work / "audio" / "mix.wav")})


def mix_problems(project):
    """지금 mix.wav 가 지금 입력으로 만든 것인지 본다"""
    stamp_path = project.cache / "mix_stamp.json"
    mix = project.work / "audio" / "mix.wav"
    if not stamp_path.exists() or not mix.exists():
        return ["믹스 기록이 없다"]
    stamp = read_json(stamp_path)
    problems = []
    if stamp["inputs"] != audio_fingerprint(project):
        problems.append("믹스 뒤에 시각표·효과음·음악·나레이션·믹스 스크립트가 바뀌었다")
    if stamp["output"] != sha256_file(mix):
        problems.append("mix.wav 가 믹스 기록과 다르다")
    return problems


def command_mix(project, _args):
    subprocess.run([sys.executable, str(mix_script(project)), str(project.work / "timeline.json")], cwd=project.work, check=True)
    write_mix_stamp(project)


def count_frames(video):
    output = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(video)],
                            capture_output=True, text=True, check=True).stdout
    return int(output.strip().strip(","))


def join_stamp_path(project, draft=False):
    return project.cache / f"join_{'draft' if draft else 'final'}.json"


def join_inputs(project, draft=False):
    """이어 붙이기 결과를 결정하는 입력: 컷 순서, 클립, 믹스, 시각표"""
    return {"order": [cut["id"] for cut in project.spec["cuts"]],
            "clips": {cut["id"]: sha256_file(project.clip(cut["id"], draft)) for cut in project.spec["cuts"]},
            "mix": sha256_file(project.work / "audio" / "mix.wav"), "timeline": sha256_file(project.work / "timeline.json")}


def write_join_stamp(project, draft=False):
    master = project.cache / "final" / f"{project.final_name(draft)}.mp4"
    write_json(join_stamp_path(project, draft), {**join_inputs(project, draft), "master": sha256_file(master)})


def join_problems(project):
    """최종 영상이 지금 컷 순서·클립·믹스·시각표로 만든 것인지 본다"""
    stamp_path = join_stamp_path(project)
    master = project.cache / "final" / f"{project.final_name()}.mp4"
    if not stamp_path.exists() or not master.exists():
        return ["이어 붙이기 기록이 없다"]
    stamp = read_json(stamp_path)
    current = join_inputs(project)
    labels = {"order": "컷 순서", "clips": "컷 클립", "mix": "mix.wav", "timeline": "시각표"}
    problems = [f"이어 붙인 뒤에 {label}이(가) 바뀌었다" for key, label in labels.items() if stamp.get(key) != current[key]]
    if stamp.get("master") != sha256_file(master):
        problems.append("최종 영상이 이어 붙이기 기록과 다르다")
    return problems


def command_join(project, args):
    timing = project.timing()
    problems = mix_problems(project)
    if problems:
        raise SystemExit("믹스를 다시 해야 한다: " + " · ".join(problems) + ". reel.py mix 를 먼저 실행한다")
    clips = []
    for cut in project.spec["cuts"]:
        clip = project.clip(cut["id"], args.draft)
        if not clip.exists():
            raise SystemExit(f"{clip} 가 없다. reel.py render 를 먼저 실행한다")
        frames = count_frames(clip)
        if frames != timing.frames[cut["id"]]:
            raise SystemExit(f"{cut['id']}: 클립 {frames}프레임, 시각표 {timing.frames[cut['id']]}프레임. reel.py render {cut['id']} 로 다시 렌더한다")
        clips.append(clip)
    output = project.cache / "final"
    output.mkdir(parents=True, exist_ok=True)
    listing = project.cache / "concat.txt"
    listing.write_text("".join(f"file '{clip.as_posix()}'\n" for clip in clips), encoding="utf-8")
    joined = project.cache / "joined.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(joined)], check=True)
    name = project.final_name(args.draft)
    command = [sys.executable, str(SCRIPTS / "finish.py"), "--video", str(joined), "--audio", str(project.work / "audio" / "mix.wav"), "--name", name, "--outdir", str(output)]
    if args.draft or not getattr(args, "web", True):
        command.append("--no-web")
    subprocess.run(command, check=True)
    shutil.copy2(project.cache / "captions.srt", output / f"{name}.srt")
    master_frames = count_frames(output / f"{name}.mp4")
    if master_frames != timing.total_frames:
        raise SystemExit(f"최종 영상 {master_frames}프레임, 시각표 {timing.total_frames}프레임. 영상과 소리가 어긋난다")
    write_join_stamp(project, args.draft)
    print(f"전체 {timing.total:.2f}초 → {output / (name + '.mp4')}")


def command_qa(project, args):
    import cut_checks
    video = project.cache / "final" / f"{project.final_name(args.draft)}.mp4"
    if not video.exists():
        raise SystemExit(f"{video} 가 없다. reel.py join 을 먼저 실행한다")
    timeline = project.work / "timeline.json"
    qa_dir = project.cache / "qa"
    command = [sys.executable, str(SCRIPTS / "qa_check.py"), str(video), "--timeline", str(timeline), "--qa", str(qa_dir)]
    if args.stt:
        command += ["--script", str(project.work / project.spec["narration"]["script"]), "--stt"]
    subprocess.run(command, check=True)
    timing = project.timing()
    findings = cut_checks.run(str(video), read_json(timeline), syncs=resolve_syncs(project, timing), max_chars=project.spec.get("caption_max_chars", cut_checks.MAX_CHARS))
    report = ["", "## 컷 검사(cut_checks.py)", *(f"- [ ] {item}" for item in findings)] if findings else ["", "## 컷 검사(cut_checks.py) 통과"]
    print("\n".join(report))
    with open(qa_dir / "qa_report.md", "a", encoding="utf-8") as stream:
        stream.write("\n".join(report) + "\n")


def resolve_syncs(project, timing):
    rows = []
    for item in project.spec.get("syncs", []):
        line = timing.lines[item["line"]]
        cut = timing.line_cut(line)
        rows.append({"label": f"{item['line']} ↔ {item['cue']}", "cut": cut, "expected": timing.expression(cut, line["start"]) + float(item.get("offset", 0)),
                     "actual": timing.local(cut, item["cue"]), "tolerance": float(item.get("tolerance", 0.5))})
    return rows


def command_preview(project, args):
    """지금 있는 컷 클립을 지금 mix.wav 와 바로 잇는다. 검사·마감 인코딩 없이 몇 초면 된다. 납품 파일이 아니다"""
    timing = project.timing()
    clips = [(cut["id"], project.clip(cut["id"], args.draft)) for cut in project.spec["cuts"]]
    missing = [cut_id for cut_id, clip in clips if not clip.exists()]
    if missing:
        raise SystemExit(f"클립이 없는 컷: {', '.join(missing)}. 렌더한 컷은 {project.cache / 'preview'} 에서 컷별로 볼 수 있다")
    for problem in mix_problems(project):
        print(f"△ {problem}. 소리가 화면과 다를 수 있다")
    output = project.cache / "preview"
    output.mkdir(parents=True, exist_ok=True)
    listing = output / "concat.txt"
    listing.write_text("".join(f"file '{clip.as_posix()}'\n" for _, clip in clips), encoding="utf-8")
    target = output / f"{project.final_name(args.draft)}_preview.mp4"
    command = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing)]
    mix = project.work / "audio" / "mix.wav"
    command += ["-i", str(mix), "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "192k", "-shortest"] if mix.exists() else []
    subprocess.run([*command, "-c:v", "copy", "-movflags", "+faststart", str(target)], check=True)
    print(f"미리보기 {timing.total:.2f}초 → {target}")


def command_all(project, args):
    """수정 확인용. 믹스는 화면과 무관하므로 렌더 앞에 두어 컷마다 소리가 맞는 미리보기를 만든다. 공유용 인코딩과 검수는 --full 이나 release 에서 한다"""
    command_build(project, argparse.Namespace(tts=False))
    command_mix(project, args)
    command_render(project, argparse.Namespace(cuts=None, draft=args.draft, adopt=False, workers=args.workers))
    command_join(project, argparse.Namespace(draft=args.draft, web=args.full))
    if args.full:
        command_qa(project, argparse.Namespace(draft=args.draft, stt=False))


def layout_table(project, timing=None):
    """영상 시각을 찾을 때 쓰는 표. 판의 manifest 에도 그대로 남긴다"""
    timing = timing or project.timing()
    durations, script = project.narration()
    lines, _ = collect_lines(project, timing, script, durations)
    sfx, _ = collect_sfx(project, timing)
    cuts = []
    for cut in project.spec["cuts"]:
        scenes = [path.name for path in project.page_files(cut) if path.name.startswith("scenes_") and path.name != "scenes_common.js"]
        cuts.append({"id": cut["id"], "start": timing.offsets[cut["id"]], "length": timing.local(cut["id"], "length"), "scenes": scenes,
                     "cues": {name: timing.local(cut["id"], name) for name in (cut.get("cues") or {})}})
    return {"total": timing.total, "cuts": cuts, "lines": lines, "sfx": sfx}


def latest_version(project):
    if not project.versions.exists():
        return None
    found = [path for path in project.versions.iterdir() if (path / "manifest.json").exists()]
    return max(found, key=lambda path: read_json(path / "manifest.json")["created"]).name if found else None


def command_where(project, args):
    version = args.version or (None if args.current else latest_version(project))
    if version:
        manifest = project.versions / version / "manifest.json"
        if not manifest.exists():
            raise SystemExit(f"판 {version} 이 없다")
        table, source = read_json(manifest)["layout"], f"판 {version}"
    else:
        table, source = layout_table(project), "현재 소스"
    at = parse_time(args.time)
    print(f"{clock(at)} ({source} 기준, 전체 {clock(table['total'])})")
    cut = next((row for row in table["cuts"] if row["start"] <= at < row["start"] + row["length"]), None)
    if not cut:
        raise SystemExit("영상 밖의 시각이다")
    local = at - cut["start"]
    print(f"컷      {cut['id']} · 컷 안 {local:.2f}초 · 장면 파일 {', '.join(cut['scenes']) or '-'}")
    active = [line for line in table["lines"] if line["cut"] == cut["id"] and line["start"] <= local <= line["end"]]
    for line in active:
        print(f"자막    {line['id']} ({line['start']:.2f}~{line['end']:.2f}) {line['text']}")
    if not active:
        print("자막    없음")
    ordered = sorted(cut["cues"].items(), key=lambda item: item[1])
    before = [item for item in ordered if item[1] <= local][-1:]
    after = [item for item in ordered if item[1] > local][:1]
    if before or after:
        print("큐      " + " · ".join(f"{name} {value:.2f}" for name, value in [*before, *after]))
    near = [item for item in table["sfx"] if item["cut"] == cut["id"] and abs(item["at"] - local) <= 1.5]
    if near:
        print("효과음  " + " · ".join(f"{item['name']} {item['at']:.2f}" + (f"({item['cue']})" if item.get("cue") else "") for item in near))


def snapshot(project):
    """작업 폴더를 내용 해시 저장소에 넣고 {상대 경로: 해시} 를 돌려준다"""
    files, added, added_bytes = {}, 0, 0
    project.store.mkdir(parents=True, exist_ok=True)
    for path in sorted(project.work.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(project.work).as_posix()
        if any(fnmatch.fnmatch(relative, pattern) for pattern in [*SNAPSHOT_EXCLUDE, *project.spec.get("snapshot_exclude", [])]):
            continue
        digest = sha256_file(path)
        files[relative] = digest
        blob = project.store / digest[:2] / digest
        if not blob.exists():
            blob.parent.mkdir(exist_ok=True)
            shutil.copy2(path, blob)
            added += 1
            added_bytes += path.stat().st_size
    return files, added, added_bytes


def stale_cuts(project):
    table = project.renders().get("final", {})
    return [cut["id"] for cut in project.spec["cuts"] if table.get(cut["id"]) != project.cut_hash(cut) or not project.clip(cut["id"]).exists()]


def older(path, than):
    return not path.exists() or path.stat().st_mtime < than.stat().st_mtime


def prepare_release(project, master):
    """수정 중에는 건너뛴 공유용 인코딩과 검수를 판을 남기기 전에 한 번만 한다"""
    web = master.with_name(f"{master.stem}_공유용.mp4")
    if older(web, master):
        print("공유용 파일을 만든다")
        command_join(project, argparse.Namespace(draft=False, web=True))
    report = project.cache / "qa" / "qa_report.md"
    if older(report, master):
        print("검수한다")
        command_qa(project, argparse.Namespace(draft=False, stt=False))


def command_release(project, args):
    target = project.versions / args.version
    # manifest 가 없는 폴더는 납품 전에 원고 등을 받아 두려고 미리 만든 것이다
    if (target / "manifest.json").exists():
        raise SystemExit(f"{target} 가 이미 있다. 납품한 판은 덮어쓰지 않는다")
    command_build(project, argparse.Namespace(tts=False))
    stale = stale_cuts(project)
    if stale and not args.force:
        raise SystemExit(f"소스와 클립이 다른 컷: {', '.join(stale)}. reel.py render 와 join 을 먼저 실행한다(--force 로 무시)")
    final = project.cache / "final"
    name = project.final_name()
    master = final / f"{name}.mp4"
    if not master.exists():
        raise SystemExit(f"{master} 가 없다. reel.py join 을 먼저 실행한다")
    problems = [*mix_problems(project), *join_problems(project)]
    if problems and not args.force:
        raise SystemExit("최종 영상이 지금 소스와 다르다: " + " · ".join(problems) + ". reel.py mix 와 join 을 먼저 실행한다(--force 로 무시)")
    if problems:
        print("△ --force: 공유용 파일과 검수를 새로 만들지 않고 있는 그대로 저장한다")
    else:
        prepare_release(project, master)
    files, added, added_bytes = snapshot(project)
    target.mkdir(parents=True, exist_ok=True)
    for source in final.glob(f"{name}*"):
        if source.name.startswith(project.final_name(draft=True)):
            continue
        stem_tail = source.name[len(name):]
        shutil.copy2(source, target / f"{name}_{args.version}{stem_tail}")
    for document in ("storyboard.md", "feedback.md"):
        if (project.work / document).exists():
            shutil.copy2(project.work / document, target / document)
    qa_report = project.cache / "qa" / "qa_report.md"
    if qa_report.exists():
        shutil.copy2(qa_report, target / "qa_report.md")
    write_json(target / "manifest.json", {
        "version": args.version, "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "note": args.note or "",
        "duration": project.timing().total, "cut_hashes": project.renders().get("final", {}), "layout": layout_table(project), "files": files})
    print(f"판 {args.version}: {target}")
    print(f"소스 {len(files)}개 중 새로 저장 {added}개({added_bytes / 1e6:.1f}MB). 나머지는 이전 판과 같은 내용이라 다시 저장하지 않았다")


def command_restore(project, args):
    manifest = project.versions / args.version / "manifest.json"
    if not manifest.exists():
        raise SystemExit(f"판 {args.version} 이 없다")
    target = Path(args.to).resolve()
    if target.exists() and any(target.iterdir()):
        raise SystemExit(f"{target} 가 비어 있지 않다. 작업 소스를 덮어쓰지 않도록 빈 폴더만 받는다")
    files = read_json(manifest)["files"]
    for relative, digest in files.items():
        blob = project.store / digest[:2] / digest
        if not blob.exists():
            raise SystemExit(f"저장소에 없다: {relative} ({digest[:12]})")
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(blob, path)
    print(f"판 {args.version} 소스 {len(files)}개 → {target}")


def command_versions(project, _args):
    if not project.versions.exists():
        print("판 없음")
        return
    rows = [read_json(path / "manifest.json") for path in project.versions.iterdir() if (path / "manifest.json").exists()]
    for row in sorted(rows, key=lambda item: item["created"]):
        print(f"{row['version']:6s} {row['created'][:16]}  {clock(row['duration'])}  {row['note']}")
    if project.store.exists():
        size = sum(path.stat().st_size for path in project.store.rglob("*") if path.is_file())
        print(f"소스 저장소 {size / 1e6:.0f}MB")


CUTS_TEMPLATE = {"title": "", "fps": 30, "width": 1920, "height": 1080, "narration": {"dir": "audio/vo", "durations": "audio/vo/durations.json", "script": "audio/vo/script.json"},
                 "cuts": [], "lines": [], "sfx": [], "music": {}, "syncs": []}
STORYBOARD = "# 스토리보드\n\n| 컷 | 내용 | 나레이션 |\n| --- | --- | --- |\n"
FEEDBACK = "# 수정 요청\n\n판마다 받은 요청을 영상 시각과 함께 적는다. 시각은 `reel.py where <시각>` 으로 컷을 찾는다.\n"


def command_init(args):
    root = Path(args.root).resolve()
    work = root / "project"
    if (work / "cuts.json").exists():
        raise SystemExit(f"{work / 'cuts.json'} 가 이미 있다")
    subprocess.run([sys.executable, str(SCRIPTS / "new_project.py"), str(work), "--ratio", args.ratio, "--fps", str(args.fps)], check=True)
    for folder in ("cache", "versions"):
        (root / folder).mkdir(exist_ok=True)
    (root / ".gitignore").write_text("cache/\n", encoding="utf-8")
    meta = read_json(work / "project.json")
    write_json(work / "cuts.json", {**CUTS_TEMPLATE, "title": root.name, "fps": args.fps, "width": meta["width"], "height": meta["height"]})
    (work / "storyboard.md").write_text(STORYBOARD, encoding="utf-8")
    (work / "feedback.md").write_text(FEEDBACK, encoding="utf-8")
    print(f"준비됨: {root}\n다음: reel.py --root {root} add-cut c01 --length 10")


def command_add_cut(project, args):
    if any(cut["id"] == args.id for cut in project.spec["cuts"]):
        raise SystemExit(f"컷 {args.id} 가 이미 있다")
    reel = project.work / "reel"
    template = (ENGINE / "index.html").read_text(encoding="utf-8")
    page = re.sub(r'<script src="timeline\.js"></script>[^\n]*', f'<script src="timeline_{args.id}.js"></script>', template)
    page = page.replace('src="scenes.js"', f'src="scenes_{args.id}.js"')
    (reel / f"{args.id}.html").write_text(page, encoding="utf-8")
    scene = reel / f"scenes_{args.id}.js"
    if not scene.exists():
        scene.write_text(
            "const { ease, prog, win, kf, clamp, lerp, css, h, cue } = REEL;\n"
            f"REEL.config({{ duration: cue('length'), fps: {project.spec.get('fps', 30)}, width: {project.spec.get('width', 1920)}, height: {project.spec.get('height', 1080)}, audio: null }});\n",
            encoding="utf-8")
    project.spec["cuts"].append({"id": args.id, "page": f"{args.id}.html", "length": args.length, "cues": {}})
    write_json(project.spec_path, project.spec)
    print(f"컷 {args.id}: reel/{args.id}.html, reel/scenes_{args.id}.js")


def main():
    parser = argparse.ArgumentParser(description="컷 영상 수정·판 관리")
    parser.add_argument("--root", help="project/ 가 있는 폴더. 없으면 현재 폴더에서 위로 찾는다")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("root")
    init.add_argument("--ratio", default="16:9")
    init.add_argument("--fps", type=int, default=30)
    add_cut = commands.add_parser("add-cut")
    add_cut.add_argument("id")
    add_cut.add_argument("--length", type=float, default=10)
    build = commands.add_parser("build")
    build.add_argument("--tts", action="store_true", help="나레이션 생성 스크립트도 실행한다")
    render = commands.add_parser("render")
    render.add_argument("cuts", nargs="*")
    render.add_argument("--draft", action="store_true")
    render.add_argument("--adopt", action="store_true", help="렌더하지 않고 지금 클립을 현재 소스의 결과로 기록한다")
    render.add_argument("--workers", type=int)
    commands.add_parser("deps")
    commands.add_parser("mix")
    join = commands.add_parser("join")
    join.add_argument("--draft", action="store_true")
    quality = commands.add_parser("qa")
    quality.add_argument("--draft", action="store_true")
    quality.add_argument("--stt", action="store_true")
    everything = commands.add_parser("all")
    everything.add_argument("--draft", action="store_true")
    everything.add_argument("--full", action="store_true", help="공유용 인코딩과 검수까지 한다(release 는 빠진 것을 알아서 한다)")
    preview = commands.add_parser("preview")
    preview.add_argument("--draft", action="store_true")
    everything.add_argument("--workers", type=int)
    where = commands.add_parser("where")
    where.add_argument("time")
    where.add_argument("--version")
    where.add_argument("--current", action="store_true", help="최근 판 대신 현재 소스 기준으로 찾는다")
    release = commands.add_parser("release")
    release.add_argument("version")
    release.add_argument("--note")
    release.add_argument("--force", action="store_true")
    restore = commands.add_parser("restore")
    restore.add_argument("version")
    restore.add_argument("--to", required=True)
    commands.add_parser("versions")
    args = parser.parse_args()
    if args.command == "init":
        command_init(args)
        return
    project = Project.find(args.root)
    handlers = {"add-cut": command_add_cut, "build": command_build, "render": command_render, "deps": command_deps, "mix": command_mix, "join": command_join, "preview": command_preview, "qa": command_qa,
                "all": command_all, "where": command_where, "release": command_release, "restore": command_restore, "versions": command_versions}
    try:
        handlers[args.command](project, args)
    except BuildError as error:
        raise SystemExit(f"✗ {error}")


if __name__ == "__main__":
    main()
