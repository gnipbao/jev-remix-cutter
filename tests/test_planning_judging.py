"""Known-answer casting, typed scoring stubs and explicit transfer boundaries."""
from __future__ import annotations

import copy
from fractions import Fraction
import itertools
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jev_remix.common import RemixError, file_hash, fingerprint, read_json, read_jsonl, write_json, write_jsonl
from jev_remix.cards import import_cards
from jev_remix import judging, planning


class RunFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="remix-planning-")
        self.run = Path(self.temp.name)
        self.brief = {"version":1,"revision":1,"fps":30,"format":"16:9","width":320,"height":180,
                      "target_seconds":2,"pack":"amv","mode":"music","music_license":"own"}
        self.intent = {"version":1,"theme":"A clear hero advances", "arc":["introduce","act","resolve"],
                       "hook":"hero appears","avoid":[]}
        write_json(self.run/"brief.json",self.brief)
        write_json(self.run/"intent.json",self.intent)
        source_rows,self.shots,self.cards = [],[],[]
        for i in range(2):
            path = self.run/f"source{i}.mp4"
            path.write_bytes(b"synthetic source fixture"+bytes([i]))
            source = f"src{i}"
            source_rows.append({"id":source,"path":str(path),"fingerprint":fingerprint(path),
                "license":"own","range":{"start_us":0,"end_us":6_000_000},
                "media":{"duration_us":6_000_000,"audio":True,"hdr":False}})
            for j in range(2):
                sid = f"{source}#s{j}"
                contact = f"contact/{source}-s{j}.jpg"
                (self.run/contact).parent.mkdir(exist_ok=True)
                (self.run/contact).write_bytes(b"fixture evidence"+bytes([i,j]))
                self.shots.append({"shot_id":sid,"source":source,"start_us":j*3_000_000,"end_us":(j+1)*3_000_000,
                    "contact":contact,
                    "features":{"motion":i+j,"brightness":100+i*20,"saturation":120,"sharpness":30,
                                "action_peak_us":j*3_000_000+1_000_000}})
                self.cards.append({"shot_id":sid,"words":{"画面":f"hero action {i} {j}","人物":"hero","情绪":"高燃"},
                    "provenance":{"mode":"agent","frames":[contact]}})
        write_json(self.run/"sources.json",{"version":1,"items":source_rows})
        write_jsonl(self.run/"shots.jsonl",self.shots)
        write_jsonl(self.run/"incoming-cards.jsonl",self.cards)
        import_cards(self.run,self.run/"incoming-cards.jsonl")
        self.cards = read_jsonl(self.run/"cards.jsonl")
        audio = self.run/"music.wav"
        audio.write_bytes(b"audio fixture")
        self.music = {"version":1,"path":str(audio),"fingerprint":fingerprint(audio),"fps":30,
                      "duration_us":2_000_000,"duration_frames":60,"low_confidence":False,
                      "beats":[{"frame":f,"strength":1} for f in range(0,60,15)],"warnings":[]}
        write_json(self.run/"music.json",self.music)

    def tearDown(self):
        self.temp.cleanup()

    def full_cast(self):
        planning.slots(self.run)
        judging.score(self.run,mock=True,top_k=4,pair_k=2)
        return planning.cast(self.run)


class SlotAndTimingTests(RunFixture):
    def test_music_integer_boundaries_no_gaps(self):
        planning.slots(self.run)
        rows = read_json(self.run/"slots.json")["items"]
        self.assertEqual([(x["start_f"],x["end_f"]) for x in rows],[(0,15),(15,30),(30,45),(45,60)])
        self.assertEqual([x["beat_anchor_f"] for x in rows],[0,15,30,45])

    def test_music_grid_requires_review_when_uncertain(self):
        self.music["low_confidence"] = True
        write_json(self.run/"music.json",self.music)
        with self.assertRaises(RemixError) as caught:
            planning.slots(self.run)
        self.assertEqual(caught.exception.code,4)
        planning.slots(self.run,allow_low_confidence=True)
        self.assertIn("explicitly accepted", read_json(self.run/"slots.json")["warnings"][0])

    def test_unavailable_compositor_is_explicit(self):
        with self.assertRaises(RemixError) as caught:
            planning.slots(self.run,pack="side-by-side")
        self.assertEqual(caught.exception.code,5)
        self.assertFalse((self.run/"slots.json").exists())

    def test_measured_peak_and_short_source_speed(self):
        shot = self.shots[0]
        slot = {"start_f":0,"end_f":15,"accent_f":0}
        result = planning.solve_in_point(shot,slot,30)
        self.assertEqual(result["in_us"],1_000_000)
        self.assertEqual(result["out_us"],1_500_000)
        self.assertEqual(result["peak_error_frames"],0)
        short = {**shot,"end_us":250_000,"features":{}}
        self.assertEqual(planning.solve_in_point(short,slot,30)["speed"],.5)
        with self.assertRaises(RemixError):
            planning.solve_in_point({**short,"end_us":100_000},slot,30)

    def test_character_unknown_cannot_pass_hard_constraint(self):
        slot = {"start_f":0,"end_f":15,"char_focus":"hero"}
        self.assertTrue(planning.feasible_shot(self.shots[0],self.cards[0],slot,30))
        card = copy.deepcopy(self.cards[0])
        card["words"]["人物"] = "uncertain hero"
        self.assertFalse(planning.feasible_shot(self.shots[0],card,slot,30))

    def test_narration_uses_declared_timestamps_and_words(self):
        self.intent["narration"] = {"path":self.music["path"],"authored":True,"license":"own",
            "sentences":[{"start_f":0,"end_f":30,"text":"hero appears", "words":[{"start_f":0,"end_f":15,"text":"hero"},{"start_f":15,"end_f":30,"text":"appears"}]},
                         {"start_f":30,"end_f":60,"text":"hero wins"}]}
        write_json(self.run/"intent.json",self.intent)
        with patch("jev_remix.media.probe",return_value={"duration_us":2_000_000}):
            planning.slots(self.run,mode="narration")
        slots = read_json(self.run/"slots.json")
        self.assertEqual(len(slots["items"]),2)
        self.assertEqual(slots["subtitles"][0]["words"][1]["end_f"],30)
        self.assertTrue(read_json(self.run/"narration.json")["authored"])
        self.intent["narration"]["sentences"][1]["start_f"] = 31
        write_json(self.run/"intent.json",self.intent)
        with patch("jev_remix.media.probe",return_value={"duration_us":2_000_000}), self.assertRaises(RemixError):
            planning.slots(self.run,mode="narration")


class JudgmentTests(RunFixture):
    def test_prepare_never_calls_provider_or_reads_key(self):
        planning.slots(self.run)
        with patch.object(judging,"read_key",side_effect=AssertionError("no credentials")), \
             patch.object(judging,"call_api",side_effect=AssertionError("no transfer")):
            result = judging.score(self.run,prepare=True,top_k=4,pair_k=2)
        self.assertEqual(result["slot_requests"],16)
        self.assertEqual(result["pair_request_upper_bound"],12)
        preview = read_json(self.run/"transfer-preview.json")
        for req in preview["requests"]:
            self.assertTrue(judging._words_only(req["state"]))
            self.assertNotIn(str(self.run),str(req))

    def test_live_transfer_needs_flag_even_with_key(self):
        planning.slots(self.run)
        with patch.object(judging,"read_key",side_effect=AssertionError("not before consent")), \
             patch.object(judging,"call_api",side_effect=AssertionError("no transfer")):
            with self.assertRaises(RemixError) as caught:
                judging.score(self.run)
        self.assertEqual(caught.exception.code,4)
        self.assertEqual(read_json(self.run/"score-meta.json")["status"],"running")

    def test_mock_cache_is_separate_and_resume_is_deterministic(self):
        planning.slots(self.run)
        with patch.object(judging,"read_key",side_effect=AssertionError("mock key read")), \
             patch.object(judging,"call_api",side_effect=AssertionError("mock transfer")):
            first = judging.score(self.run,mock=True,top_k=4,pair_k=2)
            rows1 = read_jsonl(self.run/"slot-scores.jsonl")
            second = judging.score(self.run,mock=True,top_k=4,pair_k=2)
            rows2 = read_jsonl(self.run/"slot-scores.jsonl")
        self.assertEqual(first["completed"],28)
        self.assertTrue(all(x["mock"] for x in rows2))
        self.assertTrue(all(x["cached"] for x in rows2))
        self.assertEqual([r["answers"] for r in rows1],[r["answers"] for r in rows2])
        self.assertEqual(second["new_usage"],{"input_tokens":0,"output_tokens":0})
        self.assertFalse((self.run/"cache"/"jev").exists())

    def test_typed_answers_normalize_and_fail_invalid_choices(self):
        library = judging.load_questions()
        payload = judging.request_for("slot",{"start_f":0,"end_f":15,"need":"hero","id":"x"},
            self.shots[0],self.cards[0],self.shots,self.intent,30,"jev-1.13.0",library)
        raw = judging._mock(payload)
        raw["answers"]["slot_fit"]["score"] = 2
        result = judging.parse_response(raw,payload["questions"])
        self.assertEqual(result["answers"]["slot_fit"]["value"],2/3)
        raw["answers"]["sfx_type"]["choice"] = "fabricated-action"
        with self.assertRaises(RemixError):
            judging.parse_response(raw,payload["questions"])
        raw = judging._mock(payload)
        raw["answers"]["mood_match"]["noul"] = float("nan")
        with self.assertRaises(RemixError):
            judging.parse_response(raw,payload["questions"])

    def test_request_budget_before_transport(self):
        planning.slots(self.run)
        cfg = judging.thresholds()
        cfg["judge"]["max_requests"] = 1
        with patch.object(judging,"thresholds",return_value=cfg), \
             patch.object(judging,"call_api",side_effect=AssertionError("no calls")):
            with self.assertRaises(RemixError) as caught:
                judging.score(self.run,mock=True)
        self.assertIn("budget",str(caught.exception))

    def test_stubbed_transport_partial_results_resume_only_missing(self):
        planning.slots(self.run)
        count = [0]
        def flaky(payload,key):
            count[0] += 1
            if count[0] == 1:
                raise judging.JevError("Jev HTTP 429; response omitted")
            return judging._mock(payload)
        with patch.object(judging,"read_key",return_value="test-placeholder"), \
             patch.object(judging,"call_api",side_effect=flaky):
            with self.assertRaises(RemixError):
                judging.score(self.run,allow_text_upload=True,workers=1,top_k=2,pair_k=1)
        self.assertEqual(read_json(self.run/"score-meta.json")["status"],"partial")
        self.assertEqual(len(read_jsonl(self.run/"slot-scores.jsonl")),7)
        calls = [0]
        def success(payload,key):
            calls[0] += 1
            return judging._mock(payload)
        with patch.object(judging,"read_key",return_value="test-placeholder"), \
             patch.object(judging,"call_api",side_effect=success):
            result = judging.score(self.run,allow_text_upload=True,workers=1,top_k=2,pair_k=1)
        self.assertEqual(result["status"],"complete")
        self.assertLessEqual(calls[0],4)  # One missing slot plus newly selected pair edges.

    def repeated_payload_slots(self):
        self.intent["music_slots"] = [
            {"start_f":0,"end_f":30,"need":"same evidenced lyric","mood":"urgent","section":"loop"},
            {"start_f":30,"end_f":60,"need":"same evidenced lyric","mood":"urgent","section":"loop"}]
        self.intent["slot_overrides"] = {sid:{"allowed_shots":["src0#s0"],
            "source_range":{"in_us":0,"out_us":1_000_000}} for sid in ("slot001","slot002")}
        write_json(self.run/"intent.json",self.intent)
        planning.slots(self.run)

    def test_parallel_identical_requests_are_single_flight_and_usage_is_unique(self):
        self.repeated_payload_slots()
        seen = []
        def transport(payload,key):
            from jev_remix.common import digest
            seen.append(digest(payload))
            raw = judging._mock(payload)
            raw["usage"] = {"input_tokens":100,"output_tokens":10}
            return raw
        with patch.object(judging,"read_key",return_value="test-placeholder"), \
             patch.object(judging,"call_api",side_effect=transport):
            result = judging.score(self.run,allow_text_upload=True,workers=8,top_k=4,pair_k=2)
        self.assertEqual(len(seen),len(set(seen)))
        self.assertEqual(len(seen),2)  # One shared slot judgment and one adjacent pair.
        counts = result["request_accounting"]
        self.assertEqual((counts["logical_jobs"],counts["unique_jobs"],counts["request_invocations"],
                          counts["cache_hits"],counts["coalesced_jobs"]),(3,2,2,0,1))
        self.assertEqual(result["new_usage"],{"input_tokens":200,"output_tokens":20})
        self.assertEqual(len(read_jsonl(self.run/"slot-scores.jsonl")),2)
        self.assertTrue(all(r["coalesced"] for r in read_jsonl(self.run/"slot-scores.jsonl")))
        with patch.object(judging,"read_key",side_effect=AssertionError("cached run must not read a key")), \
             patch.object(judging,"call_api",side_effect=AssertionError("cached run must not upload")):
            cached = judging.score(self.run,workers=8,top_k=4,pair_k=2)
        self.assertEqual(cached["request_accounting"]["cache_hits"],3)
        self.assertEqual(cached["request_accounting"]["unique_cache_hits"],2)
        self.assertEqual(cached["request_accounting"]["request_invocations"],0)
        self.assertEqual(cached["new_usage"],{"input_tokens":0,"output_tokens":0})

    def test_probability_failure_keeps_safe_diagnostic_and_never_becomes_score(self):
        self.repeated_payload_slots()
        def transport(payload,key):
            raw = judging._mock(payload)
            raw["model"] = "jev-1.13.0"
            raw["usage"] = {"input_tokens":321,"output_tokens":45}
            raw["untrusted_body"] = "UNTRUSTED-DO-NOT-SAVE"
            raw["answers"]["sfx_type"]["probabilities"]["none"] = .999
            return raw
        with patch.object(judging,"read_key",return_value="test-placeholder"), \
             patch.object(judging,"call_api",side_effect=transport):
            with self.assertRaises(RemixError):
                judging.score(self.run,allow_text_upload=True,workers=8)
        meta = read_json(self.run/"score-meta.json")
        self.assertEqual(meta["request_accounting"]["request_invocations"],1)
        self.assertEqual(meta["failed_response_usage"],{"input_tokens":321,"output_tokens":45})
        self.assertEqual(meta["new_usage"],{"input_tokens":0,"output_tokens":0})
        diagnostics = list((self.run/"response-diagnostics"/"jev").glob("*.json"))
        self.assertEqual(len(diagnostics),1)
        diagnostic = read_json(diagnostics[0])
        self.assertEqual(diagnostic["response"]["answers"]["sfx_type"]["probabilities"]["none"],.999)
        self.assertNotIn("UNTRUSTED-DO-NOT-SAVE",diagnostics[0].read_text())
        self.assertEqual(len(read_jsonl(self.run/"slot-scores.jsonl")),0)
        self.assertFalse(list((self.run/"cache"/"jev").glob("*.json")))
        library = judging.load_questions()["questions"]
        with self.assertRaises(RemixError) as caught:
            judging.parse_response(diagnostic["response"],{n:library[n] for n in judging.SLOT_QUESTIONS})
        self.assertIn("Choice probabilities do not sum to one: sfx_type",str(caught.exception))

    def test_diagnostics_reject_nonfinite_unknown_type_or_incomplete_choice_keys(self):
        questions = {"sfx_type":judging.load_questions()["questions"]["sfx_type"]}
        payload = {"questions":questions}
        good = judging._mock(payload)
        good["answers"]["sfx_type"]["probabilities"]["none"] = .999
        self.assertIsNotNone(judging._numeric_diagnostic(good,questions))
        bads = []
        bad = copy.deepcopy(good); bad["answers"]["sfx_type"]["confidence"] = float("nan"); bads.append(bad)
        bad = copy.deepcopy(good); bad["answers"]["sfx_type"]["type"] = "injected"; bads.append(bad)
        bad = copy.deepcopy(good); bad["answers"]["sfx_type"]["probabilities"].pop("whoosh"); bads.append(bad)
        bad = copy.deepcopy(good); bad["answers"]["sfx_type"]["probabilities"]["injected"] = 0; bads.append(bad)
        bad = copy.deepcopy(good); bad["usage"]["input_tokens"] = True; bads.append(bad)
        for bad in bads:
            with self.subTest(bad=bad):
                self.assertIsNone(judging._numeric_diagnostic(bad,questions))


class SynchronizedMusicTests(RunFixture):
    def explicit_intent(self):
        self.intent["music_slots"] = [
            {"start_f":0,"end_f":30,"need":"first lyric","mood":"restrained","section":"opening"},
            {"start_f":30,"end_f":60,"need":"second lyric","mood":"urgent","section":"response"}]
        self.intent["slot_overrides"] = {
            "slot001":{"allowed_shots":["src0#s0"],"source_range":{"in_us":250_000,"out_us":1_250_000}},
            "slot002":{"allowed_shots":["src1#s0"],"source_range":{"in_us":1_500_000,"out_us":2_500_000}}}
        write_json(self.run/"intent.json", self.intent)

    def test_explicit_slots_preserve_lyric_interval_through_scoring_cast_and_assemble(self):
        self.explicit_intent()
        planning.slots(self.run)
        rows = read_json(self.run/"slots.json")["items"]
        self.assertEqual([(r["start_f"],r["end_f"],r["need"]) for r in rows],
                         [(0,30,"first lyric"),(30,60,"second lyric")])
        with patch.object(judging,"read_key",side_effect=AssertionError("prepare must not read a key")), \
             patch.object(judging,"call_api",side_effect=AssertionError("prepare must not upload")):
            preview = judging.score(self.run,prepare=True,top_k=4,pair_k=2)
        self.assertEqual((preview["slot_requests"],preview["pair_request_upper_bound"]),(2,1))
        judging.score(self.run,mock=True,top_k=4,pair_k=2)
        planning.cast(self.run)
        planning.assemble(self.run)
        selected = read_json(self.run/"cast.json")["items"]
        self.assertEqual([(r["shot_id"],r["in_us"],r["out_us"],r["speed"]) for r in selected],
                         [("src0#s0",250_000,1_250_000,1.0),("src1#s0",1_500_000,2_500_000,1.0)])
        self.assertTrue(all("Declared source range" in r["reasons"][1] for r in selected))
        timeline = read_json(self.run/"timeline.json")
        self.assertTrue(timeline["mock"])
        self.assertEqual(timeline["items"][0]["retime"]["speed"],1.0)
        self.assertEqual(timeline["items"][0]["src"]["in_us"],250_000)

    def test_explicit_boundaries_do_not_fabricate_beat_anchors(self):
        self.intent["music_slots"] = [{"start_f":0,"end_f":29,"need":"phrase one"},
                                     {"start_f":29,"end_f":60,"need":"phrase two"}]
        write_json(self.run/"intent.json",self.intent)
        planning.slots(self.run)
        result = read_json(self.run/"slots.json")
        self.assertEqual([r["beat_anchor_f"] for r in result["items"]],[0,None])
        self.assertIn("without measured beat anchors",result["warnings"][0])
        self.intent["music_slots"][1]["beat_anchor_f"] = 29
        write_json(self.run/"intent.json",self.intent)
        with self.assertRaises(RemixError):
            planning.slots(self.run)

    def test_sync_constraints_reject_missing_shots_mismatched_lengths_and_retime(self):
        self.explicit_intent()
        good = copy.deepcopy(self.intent)
        bad_overrides = [
            {"allowed_shots":[]}, {"allowed_shots":["does-not-exist"]},
            {"allowed_shots":["src0#s0","src0#s0"]},
            {"source_range":{"in_us":2_500_000,"out_us":3_500_000}},
            {"source_range":{"in_us":250_000,"out_us":1_249_999+4}},
            {"source_range":{"in_us":True,"out_us":1_000_000}},
            {"retime":{"speed":.5}}, {"retime":{"reverse":True}},
            {"retime":{"points":[[0,1],[1,1]]}}, {"fx":[{"type":"freeze"}]}]
        for override in bad_overrides:
            with self.subTest(override=override):
                self.intent = copy.deepcopy(good)
                self.intent["slot_overrides"]["slot001"].update(override)
                write_json(self.run/"intent.json",self.intent)
                with self.assertRaises(RemixError):
                    planning.slots(self.run)

    def test_explicit_slots_require_coverage_and_keep_audio_and_confidence_guards(self):
        self.explicit_intent()
        good = copy.deepcopy(self.intent)
        for start,end in ((31,60),(30,59),(30,61)):
            self.intent = copy.deepcopy(good)
            self.intent["music_slots"][1].update(start_f=start,end_f=end)
            write_json(self.run/"intent.json",self.intent)
            with self.assertRaises(RemixError):
                planning.slots(self.run)
        self.intent = good
        write_json(self.run/"intent.json",self.intent)
        self.music["low_confidence"] = True
        write_json(self.run/"music.json",self.music)
        with self.assertRaises(RemixError) as caught:
            planning.slots(self.run)
        self.assertEqual(caught.exception.code,4)
        planning.slots(self.run,allow_low_confidence=True)
        Path(self.music["path"]).write_bytes(b"changed analyzed track")
        with self.assertRaises(RemixError) as caught:
            planning.slots(self.run,allow_low_confidence=True)
        self.assertEqual(caught.exception.code,3)

    def test_fixed_range_rounding_tolerance_and_candidate_hard_gates(self):
        slot = {"start_f":0,"end_f":1,"allowed_shots":["src0#s0"],
                "source_range":{"in_us":100_000,"out_us":133_335},"char_focus":"hero"}
        self.assertTrue(planning.feasible_shot(self.shots[0],self.cards[0],slot,30))
        self.assertFalse(planning.feasible_shot(self.shots[2],self.cards[2],slot,30))
        card = copy.deepcopy(self.cards[0])
        card["words"]["人物"] = "uncertain"
        self.assertFalse(planning.feasible_shot(self.shots[0],card,slot,30))
        slot["source_range"]["out_us"] += 1
        self.assertFalse(planning.feasible_shot(self.shots[0],self.cards[0],slot,30))

    def test_synchronized_slots_cannot_bypass_judgment_or_text_upload_guard(self):
        self.explicit_intent()
        planning.slots(self.run)
        with patch.object(judging,"call_api",side_effect=AssertionError("no approved upload")), \
             patch.object(judging,"read_key",side_effect=AssertionError("no key before approval")):
            with self.assertRaises(RemixError) as caught:
                judging.score(self.run)
        self.assertEqual(caught.exception.code,4)
        original = judging._mock
        def poor_fit(payload):
            raw = original(payload)
            if "slot_fit" in raw["answers"]:
                raw["answers"]["slot_fit"]["score"] = 1
            return raw
        with patch.object(judging,"_mock",side_effect=poor_fit):
            judging.score(self.run,mock=True)
        with self.assertRaises(RemixError):
            planning.cast(self.run)
        self.assertEqual(len(read_json(self.run/"gaps.json")["items"]),2)

    def test_synchronized_slot_lock_conflict_and_ingest_changes_are_stale(self):
        self.explicit_intent()
        planning.slots(self.run)
        judging.score(self.run,mock=True)
        planning.cast(self.run)
        planning.lock(self.run,"slot001")
        planning.cast(self.run)
        locks = read_json(self.run/"locks.json")
        locks["items"][0].update(in_us=500_000,out_us=1_500_000)
        write_json(self.run/"locks.json",locks)
        with self.assertRaises(RemixError) as caught:
            planning.cast(self.run)
        self.assertEqual(caught.exception.code,3)
        self.assertIn("source_range",str(caught.exception))
        self.shots[0]["end_us"] = 1_000_000
        write_jsonl(self.run/"shots.jsonl",self.shots)
        with self.assertRaises(RemixError) as caught:
            planning.cast(self.run)
        self.assertEqual(caught.exception.code,3)


class BeamAndEditTests(RunFixture):
    def test_beam_matches_exhaustive_under_shared_title_budget(self):
        candidates = []
        for i in range(3):
            candidates.append([{"slot":f"s{i}","shot_id":sid,"source":sid,"fused":score,"duration_frames":15,"title":title}
                for sid,score,title in (("A",.9,"licensed-title"),("B",.8,"licensed-title"),("C",.5,None))])
        pairs = {("s1","C","A"):.9,("s2","A","C"):.8}
        best = planning.beam_search(candidates,pairs,beam=1000,budgets={"licensed-title":30})
        scores = []
        for path in itertools.product(*candidates):
            if sum(r["duration_frames"] for r in path if r["title"] == "licensed-title")>30:
                continue
            value = sum(r["fused"] for r in path)
            for j,row in enumerate(path):
                value -= .25*sum(x["shot_id"] == row["shot_id"] for x in path[:j])
                if j:
                    value -= .05*(path[j-1]["source"] == row["source"])
                    value += .2*pairs.get((row["slot"],path[j-1]["shot_id"],row["shot_id"]),0)
            scores.append(value)
        self.assertAlmostEqual(best["score"],max(scores))
        self.assertIsNone(planning.beam_search([candidates[0][:2]],budgets={"licensed-title":0}))

    def test_cast_lock_swap_and_stale_card(self):
        self.full_cast()
        before = read_json(self.run/"cast.json")
        sid = before["items"][0]["slot"]
        old = before["items"][0]["shot_id"]
        planning.lock(self.run,sid)
        planning.cast(self.run)
        self.assertEqual(read_json(self.run/"cast.json")["items"][0]["shot_id"],old)
        with self.assertRaises(RemixError):
            planning.swap(self.run,sid)
        planning.lock(self.run,sid,locked=False)
        swap = planning.swap(self.run,sid,alternative=1)
        self.assertNotEqual(swap["shot_id"],old)
        self.assertTrue(read_json(self.run/"cast.json")["edited"])
        self.cards[0]["words"]["画面"] = "changed evidence"
        write_jsonl(self.run/"cards.jsonl",self.cards)
        with self.assertRaises(RemixError) as caught:
            planning.cast(self.run)
        self.assertEqual(caught.exception.code,3)

    def test_catalog_combines_sources_in_cast_budget(self):
        self.full_cast()
        (self.run/"licenses").mkdir()
        write_json(self.run/"licenses"/"catalog.json",{"titles":[{"title":"both sources",
            "match":{"source_ids":["src0","src1"]},"constraints":{"max_seconds_total":1}}]})
        with self.assertRaises(RemixError) as caught:
            planning.cast(self.run)
        self.assertEqual(caught.exception.code,2)
        self.assertEqual(read_json(self.run/"gaps.json")["items"][0]["slot"],"sequence")

    def test_missing_character_produces_gaps_not_substitute(self):
        self.intent["char_focus"] = "unknown hero"
        write_json(self.run/"intent.json",self.intent)
        planning.slots(self.run)
        judging.score(self.run,mock=True)
        with self.assertRaises(RemixError):
            planning.cast(self.run)
        self.assertEqual(len(read_json(self.run/"gaps.json")["items"]),4)

    def test_assemble_roundtrip_edl_and_hard_cuts(self):
        self.full_cast()
        planning.assemble(self.run)
        timeline = read_json(self.run/"timeline.json")
        self.assertEqual(timeline["duration_frames"],60)
        self.assertTrue(timeline["mock"])
        self.assertTrue(all(r["transition_in"] == {"type":"hard_cut","frames":0} for r in timeline["items"]))
        self.assertEqual(timeline["audio"]["license"],"own")
        exported = planning.export(self.run)
        edl = read_json(exported["path"])
        self.assertEqual(edl["version"],2)
        self.assertEqual(edl["items"][0]["in_us"],timeline["items"][0]["src"]["in_us"])
        self.assertEqual(edl["items"][-1]["end_f"],60)
        self.assertEqual(edl["clips"][0]["start_us"],timeline["items"][0]["src"]["in_us"])
        self.assertEqual(edl["clips"][0]["end_us"],timeline["items"][0]["src"]["out_us"])
        self.assertEqual(edl["clips"][0]["frames"],15)
        self.assertEqual(edl["clips"][0]["timeline_start_frame"],0)
        self.assertEqual(edl["clips"][-1]["timeline_end_frame"],60)
        self.assertEqual(sum(x["frames"] for x in edl["clips"]),edl["total_frames"])
        from jevkit.edl import export
        timeline["items"][0]["overlays"] = [{"text":"My analysis","authored_by":"user"}]
        timeline["subtitles"] = [{"start_f":0,"end_f":15,"text":"First line"}]
        data = export(timeline,read_json(self.run/"sources.json")["items"])
        self.assertEqual(data["items"][0]["overlays"],timeline["items"][0]["overlays"])
        self.assertEqual(data["subtitles"],timeline["subtitles"])

    def test_feedback_exclusion_rebuild_never_recalls_removed_shot(self):
        self.music["dependencies"] = {"brief.json":file_hash(self.run/"brief.json")}
        write_json(self.run/"music.json",self.music)
        self.full_cast()
        old = read_json(self.run/"cast.json")["items"][0]["shot_id"]
        from jev_remix.workflow import configure
        configure(self.run,{"feedback":{"exclude":[old]}})
        # Music is still the same measured grid at the same fps; no reanalysis.
        planning.slots(self.run)
        judging.score(self.run,mock=True)
        self.assertNotIn(old,{r["shot_id"] for r in read_jsonl(self.run/"slot-scores.jsonl")})
        planning.cast(self.run)
        self.assertNotIn(old,{r["shot_id"] for r in read_json(self.run/"cast.json")["items"]})

    def test_narration_mix_claim_tracks_declared_source_audio(self):
        self.intent["narration"] = {"path":self.music["path"],"authored":True,"license":"own",
            "sentences":[{"start_f":0,"end_f":30,"text":"first"},{"start_f":30,"end_f":60,"text":"second"}]}
        self.brief["preserve_source_audio"] = True
        write_json(self.run/"brief.json",self.brief)
        write_json(self.run/"intent.json",self.intent)
        with patch("jev_remix.media.probe",return_value={"duration_us":2_000_000}):
            planning.slots(self.run,mode="narration")
        judging.score(self.run,mock=True)
        planning.cast(self.run)
        planning.assemble(self.run)
        timeline = read_json(self.run/"timeline.json")
        self.assertFalse(timeline["audio"]["replaces_source_audio"])
        self.assertTrue(all(x["source_audio"] for x in timeline["items"]))
        self.assertNotIn("path",timeline["audio"])
        self.assertEqual(Path(timeline["audio"]["narration"]["path"]).resolve(),Path(self.music["path"]).resolve())

    def test_source_derived_music_is_audited_without_new_contribution(self):
        self.brief["music_from_source_audio"] = True
        write_json(self.run/"brief.json",self.brief)
        self.full_cast()
        planning.assemble(self.run)
        timeline = read_json(self.run/"timeline.json")
        self.assertFalse(timeline["audio"]["replaces_source_audio"])
        self.assertTrue(all(not x.get("source_audio") for x in timeline["items"]))
        for mapped,item in zip(timeline["audio"]["items"],timeline["items"]):
            self.assertEqual(mapped,{**item["src"],**item["out"],"source_audio":True,"unaltered":True})
        from jev_remix.originality import originality
        metrics = originality(self.run)["metrics"]
        self.assertEqual(metrics["contribution_ratio"],0)
        self.assertEqual(metrics["source_audio_ratio"],1)

    def test_new_music_keeps_replacement_contribution(self):
        self.full_cast()
        planning.assemble(self.run)
        timeline = read_json(self.run/"timeline.json")
        self.assertTrue(timeline["audio"]["replaces_source_audio"])
        self.assertNotIn("items",timeline["audio"])
        from jev_remix.originality import originality
        metrics = originality(self.run)["metrics"]
        self.assertEqual(metrics["contribution_ratio"],1)
        self.assertEqual(metrics["source_audio_ratio"],0)

    def test_source_derived_music_cannot_double_mix_source_audio(self):
        self.brief.update(music_from_source_audio=True,preserve_source_audio=True)
        write_json(self.run/"brief.json",self.brief)
        self.full_cast()
        with self.assertRaisesRegex(RemixError,"duplicate mixing"):
            planning.assemble(self.run)

    def test_jev_explicit_exclusion_is_a_cast_hard_gate(self):
        self.intent["avoid"] = ["ending spoilers"]
        write_json(self.run/"intent.json",self.intent)
        planning.slots(self.run)
        original = judging._mock
        def excluded(payload):
            raw = original(payload)
            if "excluded" in raw["answers"]:
                raw["answers"]["excluded"]["noul"] = .9
            return raw
        with patch.object(judging,"_mock",side_effect=excluded):
            judging.score(self.run,mock=True)
        with self.assertRaises(RemixError):
            planning.cast(self.run)
        self.assertEqual(len(read_json(self.run/"gaps.json")["items"]),4)

    def test_manual_score_edit_is_stale(self):
        self.full_cast()
        rows = read_jsonl(self.run/"slot-scores.jsonl")
        rows[0]["answers"]["slot_fit"]["value"] = 1
        write_jsonl(self.run/"slot-scores.jsonl",rows)
        with self.assertRaises(RemixError) as caught:
            planning.cast(self.run)
        self.assertEqual(caught.exception.code,3)


if __name__ == "__main__":
    unittest.main()
