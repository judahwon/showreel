#!/usr/bin/env python3
"""판(버전) 관리: 납품할 때마다 그 판의 최종 영상을 다시 만드는 데 필요한 것만 남긴다.

기본 절차(new_project → render → mix → finish)로 만든 프로젝트에 쓴다. 컷으로 나눈 수정 워크플로(reel.py)는 reel.py release 를 쓴다.

폴더 구조(프로젝트 폴더 아래)
  shared/              판마다 중복되는 무거운 파일을 한 벌만 둔다(이미지·폰트·라이브러리·음악·효과음·나레이션·믹스).
                       같은 경로라도 내용이 바뀌면 다른 이름으로 하나 더 저장한다: shared/<경로>/<이름>.<해시 10자리><확장자>
  out/versions/vN/
    <제목>_vN.mp4, _공유용.mp4, _포스터.jpg, .srt     그 판의 납품 파일. release 가 out/ 의 finish.py 결과를 이리로 옮긴다
    src/               그 판의 레시피(장면 코드·script.json·timeline·음악 스펙·데이터·문서). 작은 텍스트 파일만
    manifest.json      판에 필요한 모든 파일의 경로·해시·저장 위치(src/ 또는 shared/), 납품 파일 해시, 이전 판과 바뀐 파일
    변경.md             메모와 이전 판 대비 바뀐 파일(사람이 읽는 요약)
  초안·스틸·검수 시트·무음 렌더(out/draft*, out/chk_*, out/video.mp4, qa/)는 다시 만들 수 있으므로 판에 넣지 않고 out/ 에 그대로 둔다.
  예전 위치(<프로젝트>/versions/vN)에 남긴 판도 list·show·diff·restore 로 읽는다.

판에 넣는 파일
  화면: reel/index.html 을 렌더러와 같은 방식으로 열어 처음부터 끝까지 넘기며 실제로 읽은 파일(version_deps.mjs)
  소리: timeline.json 이 가리키는 음악·효과음·나레이션 파일과 audio/mix.wav(finish.py 의 입력)
  레시피: script.json, timeline.json, project.json, audio/music/*.json, data/**, 프로젝트 루트의 *.md, --include 로 더한 파일

사용법
  version.py release [vN] [--note "메모"] [--name 제목] [--include "패턴"…] [--force]
  version.py list
  version.py show vN
  version.py diff vA vB
  version.py restore vN --to <빈 폴더>        그 판을 다시 렌더·마감할 수 있는 프로젝트 폴더로 꺼낸다
프로젝트 폴더 안 어디서 실행해도 project.json 을 찾아 올라간다. 다른 곳에서는 --project <폴더>.
"""
import _utf8  # noqa: F401
import argparse
import datetime
import fnmatch
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
TEXT_EXT = {".js", ".mjs", ".html", ".htm", ".css", ".json", ".md", ".txt", ".csv", ".tsv", ".srt", ".py", ".sh", ".yaml", ".yml"}
SRC_MAX = 256 * 1024
SHARED_ALWAYS = ["reel/engine.js", "reel/fx.js", "reel/fonts/*", "reel/fonts/**", "reel/lib/*", "reel/lib/**"]
RECIPE = ["script.json", "timeline.json", "project.json", "audio/music/*.json", "data/*", "data/**", "*.md"]
NEVER = [".env", "**/.env", "**/__pycache__/*", "versions/*", "versions/**", "shared/*", "shared/**", "out/*", "out/**", "qa/*", "qa/**"]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def match(rel, patterns):
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def find_project(start):
    p = Path(start).resolve()
    for d in [p, *p.parents]:
        if (d / "project.json").exists():
            if (d / "project" / "cuts.json").exists() or (d / "cuts.json").exists():
                raise SystemExit(f"{d} 는 컷 워크플로(reel.py) 프로젝트다. reel.py release 를 쓴다")
            return d
        if (d / "project" / "cuts.json").exists():
            raise SystemExit(f"{d} 는 컷 워크플로(reel.py) 프로젝트다. reel.py release 를 쓴다")
    raise SystemExit(f"{p} 위로 project.json 이 없다. --project 로 프로젝트 폴더를 지정한다")


VERSIONS = Path("out") / "versions"
LEGACY_VERSIONS = Path("versions")   # 예전 위치. 읽기만 한다


def version_dir(root, version):
    """그 판의 폴더. 새 위치(out/versions)에 없으면 예전 위치(versions)를 본다"""
    for base in (VERSIONS, LEGACY_VERSIONS):
        d = root / base / version
        if (d / "manifest.json").exists():
            return d
    return None


def versions_of(root):
    rows = {}
    for base in (LEGACY_VERSIONS, VERSIONS):   # 같은 이름이면 새 위치가 이긴다
        vd = root / base
        if vd.exists():
            for p in vd.iterdir():
                if (p / "manifest.json").exists():
                    rows[p.name] = read_json(p / "manifest.json")
    return sorted(rows.values(), key=lambda r: r["created"])


def next_name(root):
    nums = [int(r["version"][1:]) for r in versions_of(root) if r["version"][1:].isdigit()]
    return f"v{max(nums, default=0) + 1}"


def deliverable_name(root, name):
    out = root / "out"
    if name:
        if not (out / f"{name}.mp4").exists():
            raise SystemExit(f"{out / (name + '.mp4')} 가 없다. finish.py 를 먼저 실행한다")
        return name
    webs = sorted(out.glob("*_공유용.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    for w in webs:
        stem = w.name[: -len("_공유용.mp4")]
        if (out / f"{stem}.mp4").exists():
            return stem
    raise SystemExit(f"{out} 에 finish.py 납품 파일(<제목>.mp4 + <제목>_공유용.mp4)이 없다. finish.py 를 먼저 실행하거나 --name 을 준다")


def render_deps(root):
    tmp = root / "out" / ".version_deps.json"
    r = subprocess.run(["node", str(SCRIPTS / "version_deps.mjs"), str(root), str(tmp)], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("화면이 읽는 파일을 기록하지 못했다:\n" + (r.stderr or r.stdout)[-800:])
    data = read_json(tmp)
    tmp.unlink(missing_ok=True)
    if data.get("errors"):
        print("△ 페이지 오류: " + " | ".join(data["errors"][:3]))
    return data["files"], data.get("duration")


def audio_deps(root):
    files = set()
    tl = root / "timeline.json"
    if tl.exists():
        def walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    if k in ("file", "analysis") and isinstance(v, str):
                        files.add(v.replace("\\", "/"))
                    else:
                        walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(read_json(tl))
    if (root / "audio" / "mix.wav").exists():
        files.add("audio/mix.wav")
    return sorted(f for f in files if (root / f).is_file())


def recipe_files(root, extra):
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if match(rel, NEVER):
            continue
        if match(rel, [*RECIPE, *extra]):
            # 루트 *.md 는 루트에 있는 것만(하위 폴더의 md 는 --include 로)
            if fnmatch.fnmatch(rel, "*.md") and "/" in rel and not match(rel, extra) and not match(rel, ["data/*", "data/**"]):
                continue
            out.append(rel)
    return out


def store_of(rel, digest, size):
    if match(rel, SHARED_ALWAYS):
        return "shared"
    return "src" if Path(rel).suffix.lower() in TEXT_EXT and size <= SRC_MAX else "shared"


def shared_path(rel, digest):
    p = Path(rel)
    return (Path("shared") / p.parent / f"{p.stem}.{digest[:10]}{p.suffix}").as_posix()


def command_release(root, args):
    name = deliverable_name(root, args.name)
    version = args.version or next_name(root)
    existing = version_dir(root, version)
    if existing:
        raise SystemExit(f"{existing} 가 이미 있다. 납품한 판은 덮어쓰지 않는다")
    target = root / VERSIONS / version
    master = root / "out" / f"{name}.mp4"

    print("화면이 읽는 파일을 기록한다…")
    render, duration = render_deps(root)
    audio = audio_deps(root)
    recipe = recipe_files(root, args.include or [])
    roles = {}
    for rel in recipe:
        roles[rel] = "recipe"
    for rel in audio:
        roles[rel] = "audio"
    for rel in render:
        roles.setdefault(rel, "render")
    roles = {rel: role for rel, role in roles.items() if not match(rel, NEVER) and (root / rel).is_file()}

    # 최종 영상이 지금 소스보다 오래됐으면 이 판의 레시피로 그 영상을 다시 만들 수 없다
    mt = master.stat().st_mtime
    newer = [rel for rel, role in roles.items() if role in ("render", "audio") and (root / rel).stat().st_mtime > mt + 1]
    mix = root / "audio" / "mix.wav"
    mix_old = [rel for rel, role in roles.items() if role == "audio" and rel != "audio/mix.wav" and mix.exists() and (root / rel).stat().st_mtime > mix.stat().st_mtime + 1]
    tl = root / "timeline.json"
    if tl.exists() and mix.exists() and tl.stat().st_mtime > mix.stat().st_mtime + 1:
        mix_old.append("timeline.json")
    problems = []
    if newer:
        problems.append(f"최종 영상보다 새로 고친 파일 {len(newer)}개: {', '.join(newer[:5])}{' …' if len(newer) > 5 else ''} → 다시 렌더하고 finish.py")
    if mix_old:
        problems.append(f"mix.wav 보다 새로 고친 소리 입력: {', '.join(mix_old[:5])} → mix.py 와 finish.py")
    if problems and not args.force:
        raise SystemExit("판을 남기지 않았다.\n  " + "\n  ".join(problems) + "\n(지금 납품 파일 그대로 남기려면 --force)")
    for p in problems:
        print("△ --force: " + p)

    target.mkdir(parents=True, exist_ok=True)
    files, new_shared, new_bytes = {}, 0, 0
    for rel, role in sorted(roles.items()):
        src = root / rel
        digest, size = sha256(src), src.stat().st_size
        kind = store_of(rel, digest, size)
        if kind == "src":
            dst = target / "src" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            where = f"src/{rel}"
        else:
            where = shared_path(rel, digest)
            dst = root / where
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                new_shared += 1
                new_bytes += size
        files[rel] = {"sha256": digest, "size": size, "role": role, "at": where}

    # 납품 파일은 복사하지 않고 옮긴다. out/ 에는 다음 판을 만들 때 다시 생긴다
    outputs = []
    for suffix in (".mp4", "_공유용.mp4", "_포스터.jpg", ".srt"):
        src = root / "out" / f"{name}{suffix}"
        if src.exists():
            dst = target / f"{name}_{version}{suffix}"
            shutil.move(str(src), str(dst))
            outputs.append({"file": dst.name, "sha256": sha256(dst), "size": dst.stat().st_size})

    prev = versions_of(root)
    prev = prev[-1] if prev else None
    changes = {}
    if prev:
        pf = prev["files"]
        changes = {"from": prev["version"],
                   "added": sorted(set(files) - set(pf)),
                   "removed": sorted(set(pf) - set(files)),
                   "changed": sorted(r for r in files if r in pf and pf[r]["sha256"] != files[r]["sha256"])}
    manifest = {"version": version, "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "note": args.note or "", "name": name, "duration": duration, "outputs": outputs, "changes": changes, "files": files,
                "forced": problems if problems else []}
    write_json(target / "manifest.json", manifest)

    lines = [f"# {version} — {name}", "", f"- 만든 시각: {manifest['created']}", f"- 메모: {manifest['note'] or '(없음)'}", ""]
    lines += ["## 납품 파일", *[f"- {o['file']} ({o['size'] / 1e6:.1f}MB)" for o in outputs], ""]
    if changes:
        lines.append(f"## {changes['from']} 대비 바뀐 파일")
        for label, key in (("바뀜", "changed"), ("추가", "added"), ("빠짐", "removed")):
            if changes[key]:
                lines.append(f"- {label}: " + ", ".join(changes[key]))
        if not any(changes[k] for k in ("changed", "added", "removed")):
            lines.append("- 없음(소스가 같다)")
        lines.append("")
    n_src = sum(1 for f in files.values() if f["at"].startswith("src/"))
    lines += ["## 다시 만들기", f"`version.py restore {version} --to <빈 폴더>` 로 꺼내 `render.mjs` → `finish.py`(소리를 고쳤다면 `mix.py` 먼저).", "",
              f"레시피 {n_src}개는 `src/`, 무거운 파일 {len(files) - n_src}개는 `shared/`에 있다."]
    if problems:
        lines += ["", "## --force 로 남김", *[f"- {p}" for p in problems]]
    (target / "변경.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"판 {version}: {target}")
    print(f"  out/ 의 납품 파일 {len(outputs)}개를 이 폴더로 옮겼다")
    print(f"  납품 파일 {len(outputs)}개 · 레시피(src/) {n_src}개 · 공유 파일 {len(files) - n_src}개(새로 저장 {new_shared}개, {new_bytes / 1e6:.1f}MB)")
    if changes:
        print(f"  {changes['from']} 대비: 바뀜 {len(changes['changed'])} · 추가 {len(changes['added'])} · 빠짐 {len(changes['removed'])}")


def command_list(root, _args):
    rows = versions_of(root)
    if not rows:
        print("판 없음")
        return
    for r in rows:
        vdir = version_dir(root, r["version"])
        size = sum(p.stat().st_size for p in vdir.rglob("*") if p.is_file())
        dur = f"{r['duration']:.1f}s" if r.get("duration") else "-"
        where = "" if vdir.parent == root / VERSIONS else "  (예전 위치 versions/)"
        print(f"{r['version']:5s} {r['created'][:16]}  {dur:>7s}  {size / 1e6:7.1f}MB  {r['name']}  {r['note']}{where}")
    shared = root / "shared"
    if shared.exists():
        size = sum(p.stat().st_size for p in shared.rglob("*") if p.is_file())
        print(f"shared/ {size / 1e6:.1f}MB (모든 판이 함께 쓴다)")


def load(root, version):
    vdir = version_dir(root, version)
    if not vdir:
        raise SystemExit(f"판 {version} 이 없다")
    return read_json(vdir / "manifest.json")


def command_show(root, args):
    m = load(root, args.version)
    print(f"{m['version']}  {m['created'][:16]}  {m['name']}  {m['note']}")
    for o in m["outputs"]:
        print(f"  납품 {o['file']} {o['size'] / 1e6:.1f}MB")
    by = {}
    for rel, f in m["files"].items():
        by.setdefault(f["role"], []).append((rel, f))
    for role in ("recipe", "render", "audio"):
        items = by.get(role, [])
        print(f"  {role} {len(items)}개")
        for rel, f in items:
            print(f"    {rel:48s} {f['size'] / 1e3:9.1f}KB  {f['at']}")


def command_diff(root, args):
    a, b = load(root, args.a)["files"], load(root, args.b)["files"]
    for rel in sorted(set(a) | set(b)):
        if rel not in a:
            print(f"+ {rel}")
        elif rel not in b:
            print(f"- {rel}")
        elif a[rel]["sha256"] != b[rel]["sha256"]:
            print(f"~ {rel}")
    if args.text:
        for rel in sorted(set(a) & set(b)):
            if a[rel]["sha256"] != b[rel]["sha256"] and a[rel]["at"].startswith("src/") and b[rel]["at"].startswith("src/"):
                pa = version_dir(root, args.a) / a[rel]["at"]
                pb = version_dir(root, args.b) / b[rel]["at"]
                subprocess.run(["git", "--no-pager", "diff", "--no-index", "--stat", str(pa), str(pb)])


def command_restore(root, args):
    m = load(root, args.version)
    to = Path(args.to).resolve()
    if to.exists() and any(to.iterdir()):
        raise SystemExit(f"{to} 가 비어 있지 않다. 작업 폴더를 덮어쓰지 않도록 빈 폴더만 받는다")
    vdir = version_dir(root, args.version)
    missing = []
    for rel, f in m["files"].items():
        src = (vdir / f["at"]) if f["at"].startswith("src/") else (root / f["at"])
        if not src.exists():
            missing.append(rel)
            continue
        dst = to / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    (to / "out").mkdir(parents=True, exist_ok=True)
    for o in m["outputs"]:
        shutil.copy2(vdir / o["file"], to / "out" / o["file"])
    if missing:
        print(f"✗ 없는 파일 {len(missing)}개: {', '.join(missing[:8])}")
    print(f"판 {args.version} → {to} (파일 {len(m['files']) - len(missing)}개, 납품 파일 {len(m['outputs'])}개)")
    print(f"다시 만들기: node {SCRIPTS / 'render.mjs'} --dir {to / 'reel'} --out {to / 'out' / 'video.mp4'} --sub 4 --format png"
          f" && python {SCRIPTS / 'finish.py'} --video {to / 'out' / 'video.mp4'} --audio {to / 'audio' / 'mix.wav'} --name {m['name']} --outdir {to / 'out'}")


def main():
    ap = argparse.ArgumentParser(description="판(버전) 관리")
    ap.add_argument("--project", default=".")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("release")
    r.add_argument("version", nargs="?")
    r.add_argument("--note")
    r.add_argument("--name", help="납품 파일 제목(out/<제목>.mp4). 없으면 가장 최근 finish.py 결과")
    r.add_argument("--include", action="append", help="레시피에 더할 파일 패턴(프로젝트 기준, 여러 번 쓸 수 있다)")
    r.add_argument("--force", action="store_true")
    sub.add_parser("list")
    s = sub.add_parser("show")
    s.add_argument("version")
    d = sub.add_parser("diff")
    d.add_argument("a")
    d.add_argument("b")
    d.add_argument("--text", action="store_true", help="바뀐 텍스트 파일의 줄 수 변화도 보여 준다")
    rs = sub.add_parser("restore")
    rs.add_argument("version")
    rs.add_argument("--to", required=True)
    args = ap.parse_args()
    root = find_project(args.project)
    {"release": command_release, "list": command_list, "show": command_show, "diff": command_diff, "restore": command_restore}[args.cmd](root, args)


if __name__ == "__main__":
    main()
