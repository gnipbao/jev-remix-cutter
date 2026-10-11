"""Synthetic CPU media evidence; no external footage, uploads or paid calls."""
from __future__ import annotations
import copy
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import cv2

from jev_remix.common import (ROOT, RemixError, fingerprint, read_json,
                              read_jsonl, snapshot, write_json)
from jev_remix.media import ingest, music, music_edit, onset_grid, probe, safe_output
from jev_remix.rendering import (_loudness, _mix_audio, _pcm, _qa_timeline,
    _transform_frame, _validate_timeline, generate_ass, qa, render, source_times,
    velocity_integral)

spec = importlib.util.spec_from_file_location("remix_make_demo", ROOT / "examples" / "make_demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class TimelineUnits(unittest.TestCase):
    def test_piecewise_speed_integral_and_frame_mapping(self):
        points = [[0, .5], [.5, 1.5], [1, .5]]
        self.assertAlmostEqual(velocity_integral(points), 1)
        self.assertAlmostEqual(velocity_integral(points, .25), .1875)
        item = {"out": {"start_f": 0, "end_f": 60}, "src": {"in_us": 0, "out_us": 2_000_000},
                "retime": {"points": points}}
        times = source_times(item, 30)
        self.assertEqual(len(times), 60)
        self.assertAlmostEqual(times[30], 1_000_000)
        self.assertTrue(np.all(np.diff(times) > 0))
        self.assertLess(times[-1], 2_000_000)
        item["retime"]["reverse"] = True
        reverse = source_times(item, 30)
        self.assertTrue(np.all(np.diff(reverse) < 0))
        self.assertTrue(np.all((0 <= reverse) & (reverse < 2_000_000)))

    def test_velocity_invalid_curves_fail(self):
        for curve in ([], [[0, 1]], [[0, 1], [0, 2], [1, 1]], [[0, 0], [1, 1]], [[0, 1], [.5, 1]]):
            with self.subTest(curve=curve), self.assertRaises(RemixError):
                velocity_integral(curve)
        with self.assertRaises(RemixError):
            velocity_integral([[0, 1], [1, 1]], float("nan"))

    def test_flash_effect_frame_and_strength(self):
        frame = np.full((48, 64, 3), 85, dtype=np.uint8)
        item = {"fx": [{"type": "flash_white", "at_f": 3, "frames": 2}]}
        self.assertTrue(np.array_equal(_transform_frame(frame, item, 2), frame))
        self.assertTrue(np.all(_transform_frame(frame, item, 3) == 255))
        self.assertTrue(np.array_equal(_transform_frame(frame, item, 4), frame))
        self.assertTrue(np.array_equal(_transform_frame(frame, item, 5), frame))
        item["fx"][0]["type"] = "flash_black"
        self.assertTrue(np.all(_transform_frame(frame, item, 3) == 0))

    def test_flash_frequency_guard(self):
        timeline = {"fps": 30, "items": [{"slot": "s", "out": {"start_f": 0, "end_f": 60},
            "src": {"source": "src01"}, "fx": [{"type": "flash_white", "at_f": i, "frames": 1} for i in (0, 5, 10, 15)]}]}
        self.assertTrue(any(x["metric"] == "flash_frequency" and x["level"] == "error" for x in _qa_timeline(timeline)))

    def test_subpixel_letterbox_does_not_black_entire_frame(self):
        frame = np.full((48, 64, 3), 85, dtype=np.uint8)
        self.assertTrue(np.array_equal(_transform_frame(frame, {"look": {"letterbox": .001}}, 0), frame))

    def test_ass_escapes_literal_instructions_and_burns_opinion(self):
        timeline = {"fps": 30, "width": 320, "height": 180, "duration_frames": 60,
            "subtitles": [{"start_f": 0, "end_f": 30, "text": "literal {\\pos(0,0)}\nnext"}],
            "items": [{"out": {"start_f": 30, "end_f": 60}, "overlays": [{"text": "Our analysis", "authored": True}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"captions.ass"
            generate_ass(timeline, path)
            text = path.read_text()
        self.assertIn("literal \\{\\\\pos(0,0)\\}\\Nnext", text)
        self.assertIn("0:00:01.00,0:00:02.00", text)
        self.assertIn("Our analysis", text)

    def test_ass_center_caption_honors_bounded_style_and_rejects_injection(self):
        overlay = {"text":"existing final lyric", "position":"center", "font_size":72,
                   "font_family":"Arial", "authored":False, "contribution":"caption"}
        timeline = {"fps":30,"width":320,"height":180,"duration_frames":60,"items":[
            {"out":{"start_f":0,"end_f":60},"overlays":[overlay]}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"center.ass"
            generate_ass(timeline,path)
            self.assertIn("{\\an5\\pos(160,90)\\fs72\\fnArial}existing final lyric",path.read_text())
            for invalid in ({"position":"top"},{"font_size":129},{"font_size":True},
                            {"font_family":"Arial\\pos(0,0)"},{"font_family":"Arial,Injected"}):
                changed = copy.deepcopy(timeline)
                changed["items"][0]["overlays"][0].update(invalid)
                with self.subTest(invalid=invalid), self.assertRaises(RemixError):
                    generate_ass(changed,path)

    def test_silence_has_no_invented_beats(self):
        beats, confidence, bpm, _, _ = onset_grid(np.zeros(24_000), 24_000)
        self.assertEqual(beats, [])
        self.assertEqual((confidence, bpm), (0, 0))
        with self.assertRaises(RemixError):
            onset_grid(np.array([np.nan]), 24000)

    def test_artifact_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = root/"source.mp4"; target.write_bytes(b"source")
            link = root/"output.mp4"; link.symlink_to(target)
            with self.assertRaises(RemixError):
                safe_output(root, link)
            self.assertEqual(target.read_bytes(), b"source")


class SyntheticMedia(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="remix-media-tests-")
        cls.root = Path(cls.tmp.name)
        cls.truth = demo.make_demo(cls.root/"inputs", 8)
        cls.run_dir = cls.root/"run"; cls.run_dir.mkdir()
        write_json(cls.run_dir/"brief.json", {"fps": 30, "music_license": "own"})
        cls.before = {name: fingerprint(cls.truth[name]) for name in ("source", "music")}
        ingest(cls.run_dir, [cls.truth["source"]], license="own", self_shot=True)
        cls.music = music(cls.run_dir, cls.truth["music"])
        cls.sources = read_json(cls.run_dir/"sources.json")["items"]
        cls.base = {"version": 2, "fps": 30, "width": 320, "height": 180, "duration_frames": 240,
            "items": [{"slot": f"s{i}", "shot_id": f"src01#s{i+1:04d}", "out": {"start_f": i*60, "end_f": (i+1)*60},
              "src": {"source": "src01", "in_us": i*2_000_000, "out_us": (i+1)*2_000_000},
              "retime": {"speed": 1, "interp": "nearest"}, "transition_in": {"type": "hard_cut", "frames": 0}}
              for i in range(4)],
            "audio": {"path": cls.truth["music"], "fingerprint": cls.before["music"], "license": "own", "replaces_source_audio": True},
            "dependencies": snapshot(cls.run_dir, ["brief.json", "sources.json", "music.json"]), "mock": True}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_scene_recall_action_peaks_and_contact_sheets(self):
        shots = read_jsonl(self.run_dir/"shots.jsonl")
        self.assertEqual(len(shots), 4)
        self.assertEqual([round(s["start_us"]*30/1e6) for s in shots[1:]], self.truth["scene_boundaries_f"])
        self.assertEqual([round(s["features"]["peak_us"]*30/1e6) for s in shots], self.truth["action_peak_frames"])
        self.assertTrue(all((self.run_dir/s["contact"]).is_file() for s in shots))

    def test_observed_120bpm_grid_within_one_frame(self):
        detected = [b["frame"] for b in self.music["beats"]]
        self.assertEqual(len(detected), len(self.truth["beat_frames"]))
        self.assertTrue(all(abs(a-b) <= 1 for a, b in zip(detected, self.truth["beat_frames"])))
        self.assertAlmostEqual(self.music["bpm"], 120, places=1)
        self.assertEqual(self.music["downbeat_confidence"], 0)
        self.assertTrue(all(b["downbeat_assumed"] for b in self.music["beats"]))

    def test_end_to_end_precise_frames_beats_motion_and_loudness(self):
        timeline = copy.deepcopy(self.base)
        # Symmetric nonlinear ramp keeps the known action peak on the 1-second beat.
        timeline["items"][0]["retime"] = {"points": [[0, .5], [.5, 1.5], [1, .5]], "interp": "nearest"}
        write_json(self.run_dir/"timeline.json", timeline)
        result = render(self.run_dir, preview=True, allow_mock=True)
        verification = read_json(result["verification"])
        self.assertTrue(verification["ok"], verification)
        self.assertEqual(verification["frames"], 240)
        self.assertTrue(all(c["error_frames"] <= 1 and c["planned_frame"] in self.truth["beat_frames"] for c in verification["cut_evidence"]))
        self.assertLessEqual(abs(verification["loudness"]["input_i"]+14), 1)
        # The first ramp's source action at 1 s maps exactly to output frame 30.
        self.assertEqual(source_times(timeline["items"][0], 30)[30], 1_000_000)
        cap = cv2.VideoCapture(result["path"])
        cap.set(cv2.CAP_PROP_POS_FRAMES, 28)
        decoded = [cap.read()[1] for _ in range(3)]
        cap.release()
        before = float(np.abs(decoded[1][45:125, :120].astype(float)-decoded[0][45:125, :120]).mean())
        action = float(np.abs(decoded[2][45:125, :120].astype(float)-decoded[1][45:125, :120]).mean())
        self.assertGreater(action, max(2, before*3), "Decoded square action must happen on output beat frame 30")
        self.assertIsNotNone(qa(self.run_dir)["render_verification"])
        self.assertEqual({name: fingerprint(self.truth[name]) for name in self.before}, self.before)

    def test_mock_delivery_and_missing_fingerprint_guards(self):
        write_json(self.run_dir/"timeline.json", self.base)
        with self.assertRaises(RemixError) as caught:
            render(self.run_dir, preview=True)
        self.assertEqual(caught.exception.code, 4)
        missing = copy.deepcopy(self.base); missing["audio"].pop("fingerprint")
        with self.assertRaises(RemixError) as caught:
            _validate_timeline(missing, self.sources)
        self.assertEqual(caught.exception.code, 3)

    def test_unsupported_quality_backend_never_silently_falls_back(self):
        timeline = copy.deepcopy(self.base); timeline["items"][0]["retime"]["interp"] = "rife"
        with self.assertRaises(RemixError) as caught:
            _validate_timeline(timeline, self.sources)
        self.assertEqual(caught.exception.code, 5)
        with self.assertRaises(RemixError):
            music(self.run_dir, self.truth["music"], stems=True)

    def test_music_default_numba_cache_is_writable_inside_run_before_analysis(self):
        run = self.root/"default-cache-run"; run.mkdir()
        write_json(run/"brief.json", {"fps": 30})
        expected = run/"cache"/"numba"
        def analyze(samples, sample_rate, fps):
            self.assertEqual(os.environ["NUMBA_CACHE_DIR"], str(expected.resolve()))
            self.assertTrue(expected.is_dir())
            (expected/"cache-write-test").write_text("writable")
            return onset_grid(samples, sample_rate, fps) + ("test-onsets",)
        with patch.dict(os.environ):
            os.environ.pop("NUMBA_CACHE_DIR", None)
            with patch("jev_remix.media._musical_onsets", side_effect=analyze):
                music(run, self.truth["music"])

    def test_music_preserves_explicit_numba_cache_configuration(self):
        for value in (str(self.root/"caller-numba-cache"), ""):
            run = self.root/("explicit-cache-run" if value else "empty-cache-run"); run.mkdir()
            write_json(run/"brief.json", {"fps": 30})
            def analyze(samples, sample_rate, fps):
                self.assertEqual(os.environ["NUMBA_CACHE_DIR"], value)
                return onset_grid(samples, sample_rate, fps) + ("test-onsets",)
            with self.subTest(value=value), patch.dict(os.environ, {"NUMBA_CACHE_DIR": value}):
                with patch("jev_remix.media._musical_onsets", side_effect=analyze):
                    music(run, self.truth["music"])
            self.assertFalse((run/"cache"/"numba").exists())

    def test_nonfinite_effect_and_invalid_speed_guard(self):
        for change in ({"look": {"brightness": float("nan")}}, {"retime": {"speed": 2}},
                       {"fx": [{"type": "shake", "px": float("inf"), "frames": 2}]}):
            timeline = copy.deepcopy(self.base); timeline["items"][0].update(change)
            with self.subTest(change=change), self.assertRaises(RemixError):
                _validate_timeline(timeline, self.sources)

    def test_source_audio_provenance_mapping_guards(self):
        timeline = copy.deepcopy(self.base)
        timeline["audio"]["replaces_source_audio"] = False
        mapping = {"source":"src01","in_us":0,"out_us":8_000_000,
                   "start_f":0,"end_f":240,"source_audio":True,"unaltered":True}
        timeline["audio"]["items"] = [mapping]
        sources = copy.deepcopy(self.sources)
        sources[0]["media"]["audio"] = True
        _validate_timeline(timeline,sources)
        for change in ({"source":"missing"},{"source":[]},{"in_us":-1},{"out_us":9_000_000},
                       {"start_f":True},{"end_f":241},{"unaltered":"true"},{"out_us":7_000_000}):
            altered = copy.deepcopy(timeline)
            altered["audio"]["items"][0].update(change)
            with self.subTest(change=change), self.assertRaises(RemixError):
                _validate_timeline(altered,sources)
        for change in ({"items":[]},{"items":[mapping,mapping]},{"replaces_source_audio":True}):
            altered = copy.deepcopy(timeline)
            altered["audio"].update(change)
            with self.subTest(change=change), self.assertRaises(RemixError):
                _validate_timeline(altered,sources)
        sources[0]["media"]["audio"] = False
        with self.assertRaisesRegex(RemixError,"no audio"):
            _validate_timeline(timeline,sources)
        sources[0]["media"]["audio"] = True
        timeline["items"][0]["source_audio"] = True
        with self.assertRaisesRegex(RemixError,"per-shot"):
            _validate_timeline(timeline,sources)

    def test_source_audio_provenance_does_not_create_another_mix_track(self):
        timeline = copy.deepcopy(self.base)
        before = self.root/"mix-without-provenance.wav"
        after = self.root/"mix-with-provenance.wav"
        sources = {source["id"]:source for source in self.sources}
        _mix_audio(before,timeline,sources)
        timeline["audio"].update(replaces_source_audio=False,items=[
            {"source":"src01","in_us":0,"out_us":8_000_000,"start_f":0,"end_f":240,
             "source_audio":True,"unaltered":True}])
        _mix_audio(after,timeline,sources)
        self.assertEqual(before.read_bytes(),after.read_bytes())

    def test_caption_style_is_validated_before_render(self):
        timeline = copy.deepcopy(self.base)
        timeline["items"][0]["overlays"] = [{"text":"caption","position":"center","font_size":72,
                                              "font_family":"Noto Sans CJK SC","contribution":"caption"}]
        _validate_timeline(timeline,self.sources)
        timeline["items"][0]["overlays"][0]["font_family"] = "Arial}\\p1{"
        with self.assertRaises(RemixError):
            _validate_timeline(timeline,self.sources)

    def test_stereo_delivery_and_caption_preview_scale_survive_aac_encoding(self):
        import wave
        t = np.arange(48000)/48000
        values = np.column_stack([.2*np.sin(2*np.pi*440*t),.3*np.sin(2*np.pi*880*t)])
        audio = self.root/"stereo-delivery.wav"
        with wave.open(str(audio),"wb") as out:
            out.setnchannels(2); out.setsampwidth(2); out.setframerate(48000)
            out.writeframes((values*32767).astype("<i2").tobytes())
        timeline = copy.deepcopy(self.base)
        timeline.update(width=1920,height=1080,duration_frames=30)
        timeline["items"] = [timeline["items"][0]]
        timeline["items"][0]["out"]["end_f"] = 30
        timeline["items"][0]["src"]["out_us"] = 1_000_000
        timeline["items"][0]["overlays"] = [{"text":"existing lyric","position":"center","font_size":76}]
        timeline["audio"].update(path=str(audio),fingerprint=fingerprint(audio))
        write_json(self.run_dir/"timeline.json",timeline)
        result = render(self.run_dir,preview=True,allow_mock=True)
        verification = read_json(result["verification"])
        self.assertTrue(verification["ok"],verification)
        self.assertEqual((verification["audio_channels"],verification["audio_pipeline"]["channels"]),(2,2))
        ass = (Path(result["path"]).parent/"subtitles.ass").read_text()
        self.assertIn("{\\an5\\pos(360,202)\\fs28}existing lyric",ass)
        decoded = _pcm(result["path"],48000,channels=2)[:48000]
        for channel,correct,other in ((0,0,1),(1,1,0)):
            own = abs(np.dot(decoded[:,channel],values[:len(decoded),correct]))
            leakage = abs(np.dot(decoded[:,channel],values[:len(decoded),other]))
            self.assertGreater(own,40*max(leakage,1e-9))

    def test_unreachable_music_edit_reports_lengths(self):
        with self.assertRaises(RemixError) as caught:
            music_edit(self.run_dir, 6)
        self.assertIn(8, caught.exception.details["nearest_lengths_s"])

    def test_phrase_prefix_edit_preserves_input_and_avoids_speed_changes(self):
        import wave
        music_source = self.root/"music-16s.wav"
        with wave.open(self.truth["music"], "rb") as inp:
            parameters, data = inp.getparams(), inp.readframes(inp.getnframes())
        with wave.open(str(music_source), "wb") as out:
            out.setparams(parameters); out.writeframes(data+data)
        original = fingerprint(music_source)
        run = self.root/"edit-run"; run.mkdir(exist_ok=True)
        write_json(run/"brief.json", {"fps": 30})
        music(run, music_source)
        with self.assertRaises(RemixError) as caught:
            music_edit(run, 10)
        self.assertEqual(caught.exception.details["nearest_lengths_s"], [8, 16])
        edited = music_edit(run, 8)
        self.assertEqual(edited["duration_frames"], 240)
        self.assertEqual(edited["edit"]["speed"], 1)
        self.assertTrue(edited["edit"]["phase_assumed"])
        self.assertEqual(fingerprint(music_source), original)
        self.assertNotEqual(edited["path"], str(music_source))

    def test_narration_is_mixed_and_declared_duration_verified(self):
        timeline = copy.deepcopy(self.base)
        timeline["audio"] = {"narration": {"path": self.truth["music"], "fingerprint": self.before["music"],
            "start_f": 30, "end_f": 90, "authored": True, "authored_by": "user"}}
        _validate_timeline(timeline, self.sources)
        path = self.root/"narration-mix.wav"
        _mix_audio(path, timeline, {x["id"]: x for x in self.sources})
        import wave
        with wave.open(str(path)) as wav:
            pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        self.assertFalse(np.any(pcm[:48000]))
        self.assertTrue(np.any(pcm[48000:3*48000]))
        self.assertFalse(np.any(pcm[3*48000:]))


class StereoAudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="remix-stereo-")
        self.root = Path(self.tmp.name)
        self.sr = 48000
        t = np.arange(self.sr)/self.sr
        self.left, self.right = .2*np.sin(2*np.pi*440*t), .3*np.sin(2*np.pi*880*t)
        self.stereo = self.write_audio("stereo.wav",np.column_stack([self.left,self.right]))
        self.mono = self.write_audio("mono.wav",.04*np.sin(2*np.pi*220*t))
        self.timeline = {"fps":30,"duration_frames":30,"items":[
            {"out":{"start_f":0,"end_f":30},"src":{"source":"source","in_us":0,"out_us":1_000_000}}],
            "audio":{"path":str(self.stereo),"fingerprint":fingerprint(self.stereo),"duck_music":False}}

    def tearDown(self):
        self.tmp.cleanup()

    def write_audio(self,name,values):
        import wave
        path = self.root/name
        with wave.open(str(path),"wb") as out:
            out.setnchannels(1 if values.ndim == 1 else values.shape[1])
            out.setsampwidth(2)
            out.setframerate(self.sr)
            out.writeframes((values*32767).astype("<i2").tobytes())
        return path

    def test_music_stereo_channels_remain_distinct_without_crosstalk(self):
        self.assertEqual(probe(self.stereo,video=False)["audio_channels"],2)
        output = self.root/"music-mix.wav"
        audit = _mix_audio(output,self.timeline,{})
        result = _pcm(output,self.sr,channels=2)
        self.assertEqual((audit["channels"],audit["samples"],result.shape),(2,self.sr,(self.sr,2)))
        self.assertTrue(np.allclose(result[:,0],self.left,atol=1e-4))
        self.assertTrue(np.allclose(result[:,1],self.right,atol=1e-4))
        self.assertLess(abs(np.dot(result[:,0],self.right)/np.dot(self.right,self.right)),1e-5)
        self.assertLess(abs(np.dot(result[:,1],self.left)/np.dot(self.left,self.left)),1e-5)
        self.assertEqual(_pcm(output,self.sr).shape,(self.sr,))

    def test_source_stereo_retime_preserves_silent_right_channel(self):
        source = self.write_audio("left-only.wav",np.column_stack([self.left,np.zeros(self.sr)]))
        timeline = copy.deepcopy(self.timeline)
        timeline["audio"] = {}
        timeline["duration_frames"] = 15
        timeline["items"][0].update(source_audio=True,retime={"speed":2})
        timeline["items"][0]["out"]["end_f"] = 15
        sources = {"source":{"path":str(source),"media":{"audio":True,"audio_channels":2}}}
        output = self.root/"source-mix.wav"
        audit = _mix_audio(output,timeline,sources)
        result = _pcm(output,self.sr,channels=2)
        expected = np.interp(np.linspace(0,self.sr-1,self.sr//2),np.arange(self.sr),self.left)
        self.assertEqual(audit["source_input_channels"],{"source":2})
        self.assertEqual(result.shape,(self.sr//2,2))
        self.assertTrue(np.allclose(result[:,0],expected,atol=1e-4))
        self.assertFalse(np.any(result[:,1]))

    def test_mono_narration_source_and_effect_are_centered_in_stereo_mix(self):
        baseline = self.root/"baseline.wav"
        _mix_audio(baseline,self.timeline,{})
        timeline = copy.deepcopy(self.timeline)
        timeline["audio"]["narration"] = {"path":str(self.mono),"start_f":0,"end_f":30,"gain":1}
        timeline["items"][0].update(source_audio=True,sfx=[{"type":"impact","at_f":0,"gain":.05}])
        sources = {"source":{"path":str(self.mono),"media":{"audio":True,"audio_channels":1}}}
        output = self.root/"centered.wav"
        audit = _mix_audio(output,timeline,sources)
        added = _pcm(output,self.sr,channels=2)-_pcm(baseline,self.sr,channels=2)
        self.assertEqual(audit["channels"],2)
        self.assertEqual(audit["headroom_gain"],1)
        self.assertTrue(np.any(np.abs(added)>1e-3))
        self.assertTrue(np.allclose(added[:,0],added[:,1],atol=1e-4))

    def test_mono_only_mix_and_duration_remain_mono(self):
        timeline = copy.deepcopy(self.timeline)
        timeline["audio"].update(path=str(self.mono),fingerprint=fingerprint(self.mono))
        output = self.root/"mono-mix.wav"
        audit = _mix_audio(output,timeline,{})
        self.assertEqual(audit["channels"],1)
        self.assertEqual(probe(output,video=False)["audio_channels"],1)
        self.assertEqual(len(_pcm(output,self.sr)),self.sr)
        with self.assertRaises(RemixError):
            _pcm(output,self.sr,channels=3)


if __name__ == "__main__":
    unittest.main()
