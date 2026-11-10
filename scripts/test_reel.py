#!/usr/bin/env python3
"""reel.py 시각 계산·판 저장소·cut_checks 판정 시험. 렌더·브라우저 없이 돈다.

사용법: python scripts/test_reel.py
"""
import _utf8  # noqa: F401
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import numpy as np

import cut_checks
import reel


def timing_of(cuts, lines=None, durations=None):
    return reel.Timing({"cuts": cuts, "lines": lines or [], "narration": {"tail": 0.35, "pad": 0.2}}, durations or {})


class TimingTest(unittest.TestCase):
    def test_cues_follow_each_other(self):
        timing = timing_of([{"id": "a", "length": "end+1", "cues": {"start": 2, "middle": "start+3", "end": "middle+0.5"}}])
        self.assertEqual(timing.local("a", "middle"), 5)
        self.assertEqual(timing.local("a", "end"), 5.5)
        self.assertEqual(timing.local("a", "length"), 6.5)

    def test_negative_offset_and_number_expressions(self):
        timing = timing_of([{"id": "a", "length": 10, "cues": {"late": 8, "early": "late-1.5", "text_number": "3.5", "shifted": "3+1"}}])
        self.assertEqual(timing.local("a", "early"), 6.5)
        self.assertEqual(timing.local("a", "text_number"), 3.5)
        self.assertEqual(timing.local("a", "shifted"), 4)

    def test_narration_length_pushes_cues_and_later_cuts(self):
        cuts = [{"id": "a", "length": "after+1", "cues": {"after": "line.a_1.end+0.5"}}, {"id": "b", "length": 3, "cues": {"hit": 1}}]
        lines = [{"id": "a_1", "start": 1}]
        short = timing_of(cuts, lines, {"a_1": 4.0})
        long = timing_of(cuts, lines, {"a_1": 5.0})
        self.assertEqual(short.local("a", "line.a_1.end"), 4.85)
        self.assertEqual(long.local("a", "after") - short.local("a", "after"), 1)
        self.assertAlmostEqual(long.offsets["b"] - short.offsets["b"], 1)
        self.assertAlmostEqual(long.absolute("b.hit") - short.absolute("b.hit"), 1)
        self.assertAlmostEqual(long.total - short.total, 1)

    def test_lengths_snap_to_frames_and_do_not_drift(self):
        timing = timing_of([{"id": f"c{index}", "length": 1.05, "cues": {}} for index in range(30)])
        self.assertEqual(timing.frames["c0"], 32)
        self.assertEqual(timing.total_frames, 960)
        self.assertEqual(timing.total, 32.0)
        self.assertEqual(timing.offsets["c29"], round((29 * 32) / 30, 3))
        self.assertEqual(timing.cut_end("c29"), 32.0)

    def test_frame_lengths_stay_exact(self):
        timing = timing_of([{"id": "a", "length": 48.1, "cues": {}}, {"id": "b", "length": 9.5, "cues": {}}])
        self.assertEqual(timing.local("a", "length"), 48.1)
        self.assertEqual(timing.frames, {"a": 1443, "b": 285})
        self.assertEqual(timing.total, 57.6)

    def test_absolute_expressions(self):
        timing = timing_of([{"id": "a", "length": 10, "cues": {}}, {"id": "b", "length": 5, "cues": {"x": 2}}])
        self.assertEqual(timing.absolute("b"), 10)
        self.assertEqual(timing.absolute("b+1.5"), 11.5)
        self.assertEqual(timing.absolute("a.end"), 10)
        self.assertEqual(timing.absolute("b.x-0.5"), 11.5)
        self.assertEqual(timing.absolute("total"), 15)
        self.assertEqual(timing.absolute(7), 7)

    def test_cycle_is_reported(self):
        with self.assertRaisesRegex(reel.BuildError, "서로를 참조"):
            timing_of([{"id": "a", "length": "x", "cues": {"x": "y", "y": "x"}}])

    def test_self_reference_is_reported(self):
        with self.assertRaisesRegex(reel.BuildError, "서로를 참조"):
            timing_of([{"id": "a", "length": "length+1", "cues": {}}])

    def test_unknown_names_are_reported(self):
        with self.assertRaisesRegex(reel.BuildError, "'missing'"):
            timing_of([{"id": "a", "length": "missing", "cues": {}}])
        timing = timing_of([{"id": "a", "length": 1, "cues": {}}])
        with self.assertRaisesRegex(reel.BuildError, "컷 'nope'"):
            timing.absolute("nope.x")

    def test_missing_narration_is_reported(self):
        with self.assertRaisesRegex(reel.BuildError, "나레이션 길이"):
            timing_of([{"id": "a", "length": "line.a_1.end", "cues": {}}], [{"id": "a_1", "start": 0}])

    def test_bad_expression_is_reported(self):
        with self.assertRaisesRegex(reel.BuildError, "읽을 수 없다"):
            timing_of([{"id": "a", "length": "x y z", "cues": {}}])


class HelpersTest(unittest.TestCase):
    def test_parse_time(self):
        self.assertEqual(reel.parse_time("03:56"), 236)
        self.assertEqual(reel.parse_time("1:02:03"), 3723)
        self.assertEqual(reel.parse_time("236.5"), 236.5)

    def test_stamp(self):
        self.assertEqual(reel.stamp(292.7), "00:04:52,700")


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.work = self.root / "project"
        (self.work / "reel").mkdir(parents=True)
        (self.work / "assets").mkdir()
        (self.work / "audio" / "narration").mkdir(parents=True)
        (self.work / "assets" / "one.png").write_bytes(b"one")
        (self.work / "assets" / "two.png").write_bytes(b"two")
        (self.work / "assets" / "secret.json").write_text("{}", encoding="utf-8")
        (self.work / "audio" / "narration" / "script.json").write_text(json.dumps({"lines": [{"id": "a_1", "text": "안녕하세요"}, {"id": "a_2", "text": "둘째"}]}, ensure_ascii=False), encoding="utf-8")
        (self.work / "audio" / "narration" / "durations.json").write_text(json.dumps({"a_1": {"seconds": 2.0}, "a_2": {"seconds": 1.0}}), encoding="utf-8")
        for cut_id in ("a", "b"):
            (self.work / "reel" / f"{cut_id}.html").write_text(
                f'<script src="timeline_{cut_id}.js"></script><script src="common.js"></script><script src="scenes_{cut_id}.js"></script>', encoding="utf-8")
            (self.work / "reel" / f"scenes_{cut_id}.js").write_text("// 장면", encoding="utf-8")
        (self.work / "reel" / "common.js").write_text("// 공통", encoding="utf-8")
        self.write_spec({
            "title": "시험", "fps": 30,
            "narration": {"dir": "audio/narration", "durations": "audio/narration/durations.json", "script": "audio/narration/script.json"},
            "snapshot_exclude": ["assets/secret.json"],
            "cuts": [{"id": "a", "page": "a.html", "length": "typing+3", "cues": {"typing": "line.a_1.end+0.5"}, "deps": ["assets/one.png"]},
                     {"id": "b", "page": "b.html", "length": 5, "cues": {}, "deps": ["assets/two.png"]}],
            "lines": [{"id": "a_1", "start": 0.5}, {"id": "a_2", "start": "typing"}],
            "sfx": [{"cut": "a", "at": "typing", "name": "typing", "gain_db": -19}, {"cut": "b", "at": 1, "name": "pop"}],
            "music": {},
        })

    def write_spec(self, spec):
        (self.work / "cuts.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    def project(self):
        return reel.Project(self.root)

    def build(self):
        reel.command_build(self.project(), Namespace(tts=False))
        return json.loads((self.work / "timeline.json").read_text(encoding="utf-8"))


class BuildTest(ProjectCase):
    def test_timeline_and_page_files(self):
        timeline = self.build()
        self.assertEqual(timeline["duration"], round((176 + 150) / 30, 3))
        self.assertEqual([scene["id"] for scene in timeline["scenes"]], ["a", "b"])
        self.assertEqual(timeline["sfx"][0]["t"], 2.85)
        self.assertEqual(timeline["sfx"][1]["t"], round((176 / 30) + 1, 3))
        page = (self.work / "reel" / "timeline_a.js").read_text(encoding="utf-8")
        self.assertIn('"typing": 2.85', page)
        self.assertIn('const NARRATION = {"a"', page)
        self.assertNotIn('"b"', page.split("const NARRATION")[1])
        self.assertIn("안녕하세요", (self.root / "cache" / "captions.srt").read_text(encoding="utf-8"))

    def test_overlapping_line_fails(self):
        spec = json.loads((self.work / "cuts.json").read_text(encoding="utf-8"))
        spec["lines"][1]["start"] = 1.0
        self.write_spec(spec)
        with self.assertRaises(SystemExit):
            self.build()

    def test_page_without_timeline_fails(self):
        (self.work / "reel" / "b.html").write_text('<script src="scenes_b.js"></script>', encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.build()

    def test_sfx_outside_cut_fails(self):
        spec = json.loads((self.work / "cuts.json").read_text(encoding="utf-8"))
        spec["sfx"][1]["at"] = 50
        self.write_spec(spec)
        with self.assertRaises(SystemExit):
            self.build()


class HashTest(ProjectCase):
    def hashes(self):
        self.build()
        project = self.project()
        return {cut["id"]: project.cut_hash(cut) for cut in project.spec["cuts"]}

    def test_only_affected_cut_changes(self):
        base = self.hashes()
        self.assertEqual(base, self.hashes())
        (self.work / "assets" / "two.png").write_bytes(b"changed")
        after_asset = self.hashes()
        self.assertEqual(after_asset["a"], base["a"])
        self.assertNotEqual(after_asset["b"], base["b"])
        (self.work / "reel" / "scenes_a.js").write_text("// 바꿈", encoding="utf-8")
        after_scene = self.hashes()
        self.assertNotEqual(after_scene["a"], after_asset["a"])
        self.assertEqual(after_scene["b"], after_asset["b"])

    def test_shared_file_changes_all(self):
        base = self.hashes()
        (self.work / "reel" / "common.js").write_text("// 바꿈", encoding="utf-8")
        after = self.hashes()
        self.assertNotEqual(after["a"], base["a"])
        self.assertNotEqual(after["b"], base["b"])

    def test_timing_change_affects_only_that_cut(self):
        base = self.hashes()
        spec = json.loads((self.work / "cuts.json").read_text(encoding="utf-8"))
        spec["cuts"][0]["cues"]["typing"] = "line.a_1.end+1.5"
        self.write_spec(spec)
        after = self.hashes()
        self.assertNotEqual(after["a"], base["a"])
        self.assertEqual(after["b"], base["b"])

    def test_draft_and_final_are_separate(self):
        self.build()
        project = self.project()
        cut = project.spec["cuts"][0]
        self.assertNotEqual(project.cut_hash(cut, False), project.cut_hash(cut, True))


class DependencyTest(ProjectCase):
    def run_deps(self, recorded):
        def fake_run(command, **_kwargs):
            Path(command[-1]).write_text(json.dumps(recorded), encoding="utf-8")
            return reel.subprocess.CompletedProcess(command, 0)
        project = self.project()
        with mock.patch.object(reel.subprocess, "run", fake_run):
            reel.command_deps(project, None)

    def test_recording_keeps_manual_entries_and_both_affect_hash(self):
        (self.work / "assets" / "data.bin").write_bytes(b"data")
        spec = json.loads((self.work / "cuts.json").read_text(encoding="utf-8"))
        spec["cuts"][0]["deps"] = ["assets/*.bin"]
        self.write_spec(spec)
        self.build()
        self.run_deps({"a": ["assets/one.png"], "b": ["assets/two.png"]})
        cut = self.project().spec["cuts"][0]
        self.assertEqual(cut["deps"], ["assets/*.bin"])
        self.assertEqual(cut["deps_auto"], ["assets/one.png"])
        base = self.project().cut_hash(cut)
        (self.work / "assets" / "data.bin").write_bytes(b"changed")
        manual = self.project().cut_hash(cut)
        self.assertNotEqual(manual, base)
        (self.work / "assets" / "one.png").write_bytes(b"changed")
        self.assertNotEqual(self.project().cut_hash(cut), manual)

    def test_unrecorded_cut_watches_all_assets(self):
        spec = json.loads((self.work / "cuts.json").read_text(encoding="utf-8"))
        del spec["cuts"][0]["deps"]
        self.write_spec(spec)
        self.build()
        project = self.project()
        cut = project.spec["cuts"][0]
        base = project.cut_hash(cut)
        (self.work / "assets" / "two.png").write_bytes(b"changed")
        self.assertNotEqual(project.cut_hash(cut), base)

    def test_trailing_double_star_matches_files(self):
        (self.work / "assets" / "deep").mkdir()
        (self.work / "assets" / "deep" / "three.png").write_bytes(b"three")
        names = {path.name for path in self.project().matching_files("assets/**")}
        self.assertEqual(names, {"one.png", "two.png", "secret.json", "three.png"})

    def test_auto_list_replaces_previous_auto_list(self):
        self.build()
        self.run_deps({"a": ["assets/one.png"], "b": []})
        self.run_deps({"a": ["assets/two.png"], "b": []})
        self.assertEqual(self.project().spec["cuts"][0]["deps_auto"], ["assets/two.png"])

    def test_unmatched_manual_pattern_warns(self):
        output = io.StringIO()
        spec = json.loads((self.work / "cuts.json").read_text(encoding="utf-8"))
        spec["cuts"][0]["deps"] = ["assets/none_*.bin"]
        self.write_spec(spec)
        with contextlib.redirect_stdout(output):
            self.build()
        self.assertIn("none_*.bin", output.getvalue())


class SnapshotTest(ProjectCase):
    def make_final(self):
        reel.command_build(self.project(), Namespace(tts=False))
        project = self.project()
        clips = self.root / "cache" / "clips"
        clips.mkdir(parents=True)
        for cut in project.spec["cuts"]:
            (clips / f"{cut['id']}_final.mp4").write_bytes(b"clip")
        reel.write_json(self.root / "cache" / "renders.json", {"final": {cut["id"]: project.cut_hash(cut) for cut in project.spec["cuts"]}})
        final = self.root / "cache" / "final"
        final.mkdir()
        (final / "시험.mp4").write_bytes(b"video")
        (final / "시험.srt").write_text("자막", encoding="utf-8")
        (final / "시험_draft.mp4").write_bytes(b"draft")
        (self.work / "audio" / "mix.wav").write_bytes(b"mix")
        reel.write_mix_stamp(project)
        reel.write_join_stamp(project)
        self.write_release_extras()

    def write_release_extras(self):
        """공유용 파일과 검수 보고가 최종 영상보다 새것이면 release 가 다시 만들지 않는다"""
        master = self.root / "cache" / "final" / "시험.mp4"
        (master.parent / "시험_공유용.mp4").write_bytes(b"web")
        (self.root / "cache" / "qa").mkdir(parents=True, exist_ok=True)
        (self.root / "cache" / "qa" / "qa_report.md").write_text("# 검수", encoding="utf-8")
        later = master.stat().st_mtime + 1
        for path in (master.parent / "시험_공유용.mp4", self.root / "cache" / "qa" / "qa_report.md"):
            os.utime(path, (later, later))

    def assert_release_refused(self, reason):
        with self.assertRaisesRegex(SystemExit, reason):
            self.release("v9")
        self.assertFalse((self.root / "versions" / "v9").exists())

    def edit_spec(self, change):
        spec = json.loads((self.work / "cuts.json").read_text(encoding="utf-8"))
        change(spec)
        self.write_spec(spec)

    def test_sfx_change_needs_new_mix_and_join(self):
        self.make_final()
        self.edit_spec(lambda spec: spec["sfx"][1].update(at=2))
        self.assert_release_refused("최종 영상이 지금 소스와 다르다")

    def test_cut_order_change_needs_new_join(self):
        self.make_final()
        self.edit_spec(lambda spec: spec["cuts"].reverse())
        self.assert_release_refused("컷 순서")

    def test_mix_file_change_needs_new_join(self):
        self.make_final()
        (self.work / "audio" / "mix.wav").write_bytes(b"louder")
        self.assert_release_refused("mix.wav")

    def test_sound_file_change_needs_new_mix(self):
        self.make_final()
        (self.work / "audio" / "sfx").mkdir(exist_ok=True)
        (self.work / "audio" / "sfx" / "pop.wav").write_bytes(b"new sound")
        self.build()
        self.assert_release_refused("믹스 뒤에")

    def test_clip_change_needs_new_join(self):
        self.make_final()
        (self.root / "cache" / "clips" / "a_final.mp4").write_bytes(b"other")
        self.assert_release_refused("컷 클립")

    def test_replaced_master_is_refused(self):
        self.make_final()
        (self.root / "cache" / "final" / "시험.mp4").write_bytes(b"other video")
        self.assert_release_refused("최종 영상이 이어 붙이기 기록과 다르다")

    def test_missing_records_are_refused(self):
        self.make_final()
        (self.root / "cache" / "join_final.json").unlink()
        self.assert_release_refused("이어 붙이기 기록이 없다")

    def test_force_overrides_and_redo_passes(self):
        self.make_final()
        self.edit_spec(lambda spec: spec["sfx"][1].update(at=2))
        self.release("v2", force=True)
        self.assertTrue((self.root / "versions" / "v2").exists())
        project = self.project()
        reel.write_mix_stamp(project)
        reel.write_join_stamp(project)
        self.release("v3")
        self.assertTrue((self.root / "versions" / "v3").exists())

    def test_prepared_folder_can_be_released_once(self):
        self.make_final()
        prepared = self.root / "versions" / "v2"
        prepared.mkdir(parents=True)
        (prepared / "script.json").write_text("{}", encoding="utf-8")
        self.release("v2")
        self.assertTrue((prepared / "manifest.json").exists())
        self.assertTrue((prepared / "script.json").exists())
        with self.assertRaisesRegex(SystemExit, "이미 있다"):
            self.release("v2")

    def test_env_file_is_not_stored(self):
        self.make_final()
        (self.work / ".env").write_text("SECRET=1", encoding="utf-8")
        (self.work / "audio" / ".env").write_text("SECRET=2", encoding="utf-8")
        self.release("v2")
        files = json.loads((self.root / "versions" / "v2" / "manifest.json").read_text(encoding="utf-8"))["files"]
        self.assertFalse([name for name in files if name.endswith(".env")])

    def test_document_change_does_not_need_new_join(self):
        self.make_final()
        (self.work / "storyboard.md").write_text("수정", encoding="utf-8")
        self.release("v2")
        self.assertTrue((self.root / "versions" / "v2").exists())

    def release(self, version, force=False):
        reel.command_release(self.project(), Namespace(version=version, note="시험", force=force))

    def test_release_makes_missing_web_and_qa(self):
        self.make_final()
        (self.root / "cache" / "final" / "시험_공유용.mp4").unlink()
        (self.root / "cache" / "qa" / "qa_report.md").unlink()
        done = []
        join = mock.patch.object(reel, "command_join", lambda _project, args: done.append(("join", args.web)))
        qa = mock.patch.object(reel, "command_qa", lambda _project, _args: done.append(("qa", None)))
        with join, qa:
            self.release("v5")
        self.assertEqual(done, [("join", True), ("qa", None)])

    def test_fresh_web_and_qa_are_kept(self):
        self.make_final()
        with mock.patch.object(reel, "command_join", side_effect=AssertionError), mock.patch.object(reel, "command_qa", side_effect=AssertionError):
            self.release("v5")

    def test_release_restore_round_trip(self):
        self.make_final()
        self.release("v1")
        manifest = json.loads((self.root / "versions" / "v1" / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("assets/secret.json", manifest["files"])
        self.assertNotIn("timeline.json", manifest["files"])
        self.assertTrue((self.root / "versions" / "v1" / "시험_v1.mp4").exists())
        self.assertFalse(list((self.root / "versions" / "v1").glob("*draft*")))
        target = self.root / "out_v1"
        reel.command_restore(self.project(), Namespace(version="v1", to=str(target)))
        self.assertEqual((target / "assets" / "one.png").read_bytes(), b"one")
        self.assertFalse((target / "assets" / "secret.json").exists())

    def test_restore_returns_old_content_after_edit(self):
        self.make_final()
        self.release("v1")
        (self.work / "assets" / "one.png").write_bytes(b"edited")
        target = self.root / "out_v1"
        reel.command_restore(self.project(), Namespace(version="v1", to=str(target)))
        self.assertEqual((target / "assets" / "one.png").read_bytes(), b"one")

    def test_same_content_is_stored_once(self):
        self.make_final()
        self.release("v1")
        count = sum(1 for path in (self.root / ".store").rglob("*") if path.is_file())
        (self.work / "feedback.md").write_text("수정", encoding="utf-8")
        self.release("v2")
        self.assertEqual(sum(1 for path in (self.root / ".store").rglob("*") if path.is_file()), count + 1)

    def test_existing_version_and_stale_cut_are_refused(self):
        self.make_final()
        self.release("v1")
        with self.assertRaises(SystemExit):
            self.release("v1")
        (self.work / "reel" / "scenes_a.js").write_text("// 바꿈", encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "소스와 클립이 다른 컷: a"):
            self.release("v2")
        self.assertFalse((self.root / "versions" / "v2").exists())

    def test_restore_refuses_non_empty_folder(self):
        self.make_final()
        self.release("v1")
        target = self.root / "busy"
        target.mkdir()
        (target / "keep.txt").write_text("남긴다", encoding="utf-8")
        with self.assertRaises(SystemExit):
            reel.command_restore(self.project(), Namespace(version="v1", to=str(target)))
        self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "남긴다")

    def test_where_uses_release_layout_after_source_moves(self):
        self.make_final()
        self.release("v1")
        spec = json.loads((self.work / "cuts.json").read_text(encoding="utf-8"))
        spec["cuts"][0]["cues"]["typing"] = "line.a_1.end+5"
        self.write_spec(spec)
        layout = json.loads((self.root / "versions" / "v1" / "manifest.json").read_text(encoding="utf-8"))["layout"]
        self.assertEqual(layout["cuts"][0]["cues"]["typing"], 2.85)
        self.assertEqual(reel.latest_version(self.project()), "v1")


class WorkflowTest(ProjectCase):
    """렌더·ffmpeg 를 흉내 내고 명령의 순서와 범위만 본다"""

    def fake_tools(self, calls):
        def fake_run(command, **_kwargs):
            calls.append([str(part) for part in command])
            if command[0] == "node" and command[1].endswith("render.mjs"):
                Path(command[command.index("--out") + 1]).write_bytes(b"clip")
            elif command[0] == "node":
                only = command[4:] or ["a", "b"]
                Path(command[3]).write_text(json.dumps({cut_id: [f"assets/{cut_id}.png"] for cut_id in only}), encoding="utf-8")
            elif command[0] == "ffmpeg":
                Path(command[-1]).write_bytes(b"preview")
            return reel.subprocess.CompletedProcess(command, 0)
        return mock.patch.object(reel.subprocess, "run", fake_run)

    def render(self, *cuts, adopt=False):
        calls, output = [], io.StringIO()
        with self.fake_tools(calls), contextlib.redirect_stdout(output):
            reel.command_render(self.project(), Namespace(cuts=list(cuts), draft=True, adopt=adopt, workers=None))
        return calls, output.getvalue()

    def test_each_rendered_cut_gets_a_preview_right_away(self):
        (self.work / "audio" / "mix.wav").write_bytes(b"mix")
        calls, output = self.render()
        previews = self.root / "cache" / "preview"
        self.assertTrue((previews / "a_draft.mp4").exists())
        self.assertTrue((previews / "b_draft.mp4").exists())
        self.assertIn(str(previews / "a_draft.mp4"), output)
        audio = next(call for call in calls if call[0] == "ffmpeg" and call[-1].endswith("b_draft.mp4"))
        self.assertEqual(audio[audio.index("-ss") + 1], f"{self.project().timing().offsets['b']:.3f}")

    def test_deps_are_recorded_only_for_rendered_cuts(self):
        self.render()
        before = self.project().spec["cuts"][1]["deps_auto"]
        calls, _ = self.render("a")
        record = next(call for call in calls if call[0] == "node" and call[1].endswith("record_deps.mjs"))
        self.assertEqual(record[4:], ["a"])
        self.assertEqual(self.project().spec["cuts"][1]["deps_auto"], before)

    def test_adopt_only_takes_named_cuts(self):
        clips = self.root / "cache" / "clips"
        clips.mkdir(parents=True)
        (clips / "a_draft.mp4").write_bytes(b"clip")
        self.render("a", adopt=True)
        self.assertEqual(set(self.project().renders()["draft"]), {"a"})

    def run_all(self, full):
        order = []
        names = ("command_build", "command_mix", "command_render", "command_join", "command_qa")
        with contextlib.ExitStack() as stack:
            for name in names:
                stack.enter_context(mock.patch.object(reel, name, lambda *args, name=name: order.append((name, args[1]))))
            reel.command_all(self.project(), Namespace(draft=False, full=full, workers=None))
        return order

    def test_all_mixes_before_render_and_skips_heavy_steps(self):
        order = self.run_all(full=False)
        self.assertEqual([name for name, _ in order], ["command_build", "command_mix", "command_render", "command_join"])
        self.assertFalse(order[-1][1].web)

    def test_all_full_adds_web_and_qa(self):
        order = self.run_all(full=True)
        self.assertEqual(order[-1][0], "command_qa")
        self.assertTrue(order[-2][1].web)


class CutChecksTest(unittest.TestCase):
    def frames(self, levels):
        return np.stack([np.full((cut_checks.HEIGHT, cut_checks.WIDTH), float(level), np.float32) for level in levels])

    def test_white_gap_between_dark_screens_is_found(self):
        levels = [20.0] * 60 + [235.0] * 6 + [20.0] * 60
        findings = cut_checks.check_seams(self.frames(levels), [("a→b", 63 / cut_checks.FPS)])
        self.assertTrue(any("밝은 순간" in item for item in findings), findings)

    def test_smooth_fade_is_not_found(self):
        levels = list(np.linspace(200, 20, 120))
        self.assertEqual(cut_checks.check_seams(self.frames(levels), [("a→b", 60 / cut_checks.FPS)]), [])

    def test_hard_cut_is_found(self):
        levels = [240.0] * 60 + [20.0] * 60
        findings = cut_checks.check_seams(self.frames(levels), [("a→b", 60 / cut_checks.FPS)])
        self.assertTrue(any("튄다" in item for item in findings), findings)

    def test_boundary_too_close_to_edges_is_skipped(self):
        self.assertEqual(cut_checks.check_seams(self.frames([20.0] * 10), [("a→b", 0.1)]), [])

    def test_sfx_on_still_screen_is_found_unless_ambient(self):
        frames = self.frames([100.0] * 90)
        self.assertEqual(len(cut_checks.check_sfx(frames, [{"name": "pop", "t": 1.0}])), 1)
        self.assertEqual(cut_checks.check_sfx(frames, [{"name": "drone", "t": 1.0, "ambient": True}]), [])

    def test_sfx_on_change_passes(self):
        frames = self.frames([100.0] * 30 + [110.0] * 60)
        self.assertEqual(cut_checks.check_sfx(frames, [{"name": "pop", "t": 1.0}]), [])

    def test_sfx_reports_nearby_event(self):
        frames = self.frames([100.0] * 75 + [200.0] * 15)
        findings = cut_checks.check_sfx(frames, [{"name": "typing", "t": 1.0}])
        self.assertIn("1.5초 뒤", findings[0])

    def test_syncs(self):
        row = {"label": "x", "cut": "a", "expected": 10, "actual": 11, "tolerance": 0.5}
        self.assertEqual(len(cut_checks.check_syncs([row])), 1)
        self.assertEqual(cut_checks.check_syncs([{**row, "tolerance": 1.5}]), [])

    def test_captions(self):
        two = "첫 화면에서는 주문 목록을 열고 상태를 확인합니다. 다음 화면에서는 배송 기록을 찾아 비교합니다."
        self.assertIn("문장 2개", cut_checks.check_captions([{"id": "a", "start": 0, "text": two}], 70)[0])
        split = two.replace(". ", ".\n")
        self.assertEqual(cut_checks.check_captions([{"id": "a", "start": 0, "text": split}], 70), [])
        self.assertEqual(cut_checks.check_captions([{"id": "a", "start": 0, "text": "짧다. 정말."}], 70), [])
        self.assertIn("기준 70", cut_checks.check_captions([{"id": "a", "start": 0, "text": "가" * 71}], 70)[0])


if __name__ == "__main__":
    unittest.main()
