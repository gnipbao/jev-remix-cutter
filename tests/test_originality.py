"""Known-answer timelines; no model calls or external content are needed."""
from __future__ import annotations

import copy
from fractions import Fraction
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jev_remix.common import RemixError, read_json, write_json
from jev_remix.originality import (catalog_limits, check_catalog, import_catalog,
                                  originality, union, _original_speed)


def clip(slot, source, start, end, source_in, source_out, audio=False, **kwargs):
    return {"slot": slot, "out": {"start_f": start, "end_f": end},
            "src": {"source": source, "in_us": source_in, "out_us": source_out},
            "source_audio": audio, **kwargs}


class OriginalityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="remix-originality-")
        self.run = Path(self.temporary.name)
        self.sources = {"version": 1, "items": [
            {"id": "A", "license": "licensed"},
            {"id": "B", "license": "own", "self_shot": True},
            {"id": "C", "license": "unknown"}]}
        self.timeline = {"version": 2, "fps": 30, "duration_frames": 600, "items": [
            clip("A.first", "A", 0, 180, 0, 6_000_000, True),
            clip("A.next", "A", 180, 360, 6_000_000, 12_000_000, True),
            clip("B.self", "B", 360, 420, 0, 2_000_000),
            clip("C.analysis", "C", 420, 480, 0, 2_000_000, overlays=[
                {"text": "My analysis", "authored_by": "user", "contribution": "analysis"},
                {"text": "Overlapping opinion", "authored_by": "agent_user", "contribution": "opinion"}]),
            clip("A.late", "A", 480, 540, 20_000_000, 22_000_000),
            clip("A.earlier", "A", 540, 600, 14_000_000, 16_000_000)],
            "audio": {"narration": {"path": "user-narration.wav", "authored_by": "user",
                                    "intervals": [{"start_f": 480, "end_f": 600}]}}}
        self.intent = {"mode": "narration", "format": "remix", "theme": "My argument"}
        write_json(self.run / "brief.json", {"format": "remix"})
        self.save()

    def tearDown(self):
        self.temporary.cleanup()

    def save(self):
        write_json(self.run / "sources.json", self.sources)
        write_json(self.run / "timeline.json", self.timeline)
        write_json(self.run / "intent.json", self.intent)

    def test_eight_metrics_known_answer(self):
        result = originality(self.run)
        self.assertEqual(result["metrics"], {
            "contribution_ratio": .4, "longest_raw_run_s": 12.0,
            "order_preservation": .5, "source_audio_ratio": .6,
            "max_source_share": .8, "restructure": True,
            "license_coverage": .9, "catalog_violations": []})
        self.assertAlmostEqual(result["basis"]["kendall_tau"], 2 / 3)
        self.assertEqual(result["basis"]["contribution_intervals"], [{"start_f": 360, "end_f": 600}])
        findings = {x["metric"]: x for x in result["findings"]}
        self.assertEqual(set(findings), {"contribution_ratio", "longest_raw_run_s", "source_audio_ratio", "license_coverage"})
        self.assertEqual(findings["longest_raw_run_s"]["where"], {"start_f": 0, "end_f": 360})
        self.assertEqual(findings["license_coverage"]["where"][0]["slot"], "C.analysis")
        self.assertEqual(read_json(self.run / "originality.json")["metrics"], result["metrics"])

    def test_frame_union_does_not_double_count_or_overrun(self):
        self.assertEqual(union([(1, 6), (4, 9), (9, 12), (20, 30)], 25), [(1, 12), (20, 25)])
        for bad in [[(-1, 1)], [(0, 1.0)], [(True, 2)], [(4, 3)]]:
            with self.subTest(intervals=bad), self.assertRaises(RemixError):
                union(bad)

    def test_custom_profile_changes_findings_without_changing_metrics(self):
        normal = originality(self.run)
        profile = self.run / "permissive.toml"
        profile.write_text("[originality]\ncontribution_min=0.2\nlongest_raw_max_s=15\norder_max=0.9\nsource_audio_max=0.9\nsource_share_max=0.9\n")
        changed = originality(self.run, str(profile))
        self.assertEqual(changed["metrics"], normal["metrics"])
        self.assertEqual([x["metric"] for x in changed["findings"]], ["license_coverage"])

    def test_restructure_depends_on_theme_narration_or_order(self):
        self.intent = {"format": "recap", "theme": "A theme", "theme_is_original": True}
        self.save()
        self.assertFalse(originality(self.run)["metrics"]["restructure"])
        self.intent["format"] = "remix"
        self.save()
        self.assertTrue(originality(self.run)["metrics"]["restructure"])
        self.intent = {"format": "remix"}
        self.timeline["items"] = [clip("back", "A", 0, 300, 20_000_000, 30_000_000),
                                  clip("front", "A", 300, 600, 0, 10_000_000)]
        self.save()
        result = originality(self.run)
        self.assertEqual(result["basis"]["kendall_tau"], -1)
        self.assertTrue(result["metrics"]["restructure"])

    def test_own_stock_and_decorative_subtitles_are_not_new_contribution(self):
        self.sources["items"][1]["self_shot"] = False
        self.timeline["audio"] = {}
        self.timeline["items"][3]["overlays"] = [{"text": "A copied subtitle", "contribution": "subtitle", "authored_by": "user"}]
        self.save()
        self.assertEqual(originality(self.run)["metrics"]["contribution_ratio"], 0)

    def test_raw_audio_does_not_credit_licensed_music_as_replacement(self):
        self.timeline["audio"] = {"path": "licensed.wav", "license": "licensed", "replaces_source_audio": True}
        self.sources["items"][1]["self_shot"] = False
        self.timeline["items"][3]["overlays"] = []
        self.save()
        self.assertEqual(originality(self.run)["metrics"]["contribution_ratio"], .4)
        for row in self.timeline["items"]:
            row["source_audio"] = False
        self.save()
        self.assertEqual(originality(self.run)["metrics"]["contribution_ratio"], 1)

    def test_audio_track_interruption_breaks_raw_run(self):
        self.timeline["audio"] = {"items": [
            {"start_f": 0, "end_f": 90, "source_audio": True},
            {"start_f": 120, "end_f": 360, "source_audio": True}]}
        self.save()
        metrics = originality(self.run)["metrics"]
        self.assertEqual(metrics["longest_raw_run_s"], 8)
        self.assertEqual(metrics["source_audio_ratio"], .55)

    def test_speed_ramps_reverse_and_unstated_duration_change_are_not_raw(self):
        unit = clip("unit", "A", 0, 30, 0, 1_000_000, True)
        self.assertTrue(_original_speed(unit, Fraction(30)))
        self.assertTrue(_original_speed({**unit, "retime": {"points": [[0, 2], [1, 2]]}}, Fraction(30)))
        for changed in [
            {**unit, "retime": {"speed": 1, "reverse": True}},
            {**unit, "retime": {"points": [[0, 1], [.5, 2], [1, 1]]}},
            {**unit, "retime": {"speed": 2}},
            {**unit, "src": {"source": "A", "in_us": 0, "out_us": 2_000_000}}]:
            with self.subTest(changed=changed):
                self.assertFalse(_original_speed(changed, Fraction(30)))

    def test_raw_chain_gap_cutoff_is_strict(self):
        self.timeline["items"][1]["src"]["in_us"] = 6_499_999
        self.timeline["items"][1]["src"]["out_us"] = 12_499_999
        self.save()
        self.assertEqual(originality(self.run)["metrics"]["longest_raw_run_s"], 12)
        self.timeline["items"][1]["src"]["in_us"] += 1
        self.timeline["items"][1]["src"]["out_us"] += 1
        self.save()
        self.assertEqual(originality(self.run)["metrics"]["longest_raw_run_s"], 6)

    def catalog(self, maximum=20, expires="2999-01-01", formats=None):
        obj = {"catalog_id": "test", "titles": [{"title": "Shared title", "match": {"source_ids": ["A", "B"]},
            "constraints": {"max_seconds_total": maximum, "expires": expires, "allowed_formats": formats or ["remix"]}}]}
        write_json(self.run / "licenses/catalog.json", obj)
        return obj

    def test_title_budget_aggregates_sources_and_repeated_uses(self):
        self.catalog(maximum=18)
        self.assertEqual(check_catalog(self.run, as_of="2026-10-11"), [])
        self.catalog(maximum=17)
        with self.assertRaises(RemixError) as caught:
            check_catalog(self.run, as_of="2026-10-11")
        self.assertEqual(caught.exception.code, 4)
        violation = caught.exception.details["catalog_violations"][0]
        self.assertEqual(violation["used_seconds"], 18)
        self.assertEqual(violation["reason"], "max_seconds_total")
        self.assertEqual(catalog_limits(self.run)[0]["source_ids"], ["A", "B"])
        # The same source interval used twice consumes twice its output duration.
        self.timeline["items"][1]["src"] = copy.deepcopy(self.timeline["items"][0]["src"])
        self.assertEqual(check_catalog(self.run, self.timeline, False, "2026-10-11")[0]["used_seconds"], 18)

    def test_catalog_expiry_date_inclusive_and_format_scope(self):
        self.catalog(expires="2026-10-11")
        self.assertEqual(check_catalog(self.run, as_of="2026-10-11"), [])
        self.assertEqual(check_catalog(self.run, raise_on_error=False, as_of="2026-10-12")[0]["reason"], "expired")
        self.catalog(formats=["commentary"])
        self.assertEqual(check_catalog(self.run, raise_on_error=False, as_of="2026-10-11")[0]["reason"], "format_not_allowed")

    def test_formal_renderer_blocks_catalog_overrun_before_encoding(self):
        self.catalog(maximum=17)
        self.timeline.update(width=64, height=64, dependencies={}, mock=False, audio={})
        self.save()
        # Source existence has separate coverage in face/media integration tests;
        # this fixture isolates the renderer's last catalog gate before encoding.
        sources = [{**row, "media": {"duration_us": 40_000_000}} for row in self.sources["items"]]
        from jev_remix.rendering import render
        with patch("jev_remix.rendering.check_sources", return_value=sources), self.assertRaises(RemixError) as caught:
            render(self.run, confirm_unknown_license=True)
        self.assertEqual(caught.exception.code, 4)
        self.assertEqual(caught.exception.details["catalog_violations"][0]["reason"], "max_seconds_total")
        self.assertFalse((self.run / "renders").exists())

    def test_catalog_import_remaps_and_marks_only_mapped_sources(self):
        supplied = self.run / "supplied.json"
        write_json(supplied, {"titles": [{"title": "User declared", "match": {"source_ids": []}, "constraints": {"max_seconds_total": 3}}]})
        import_catalog(self.run, supplied, ["C=User declared"])
        sources = read_json(self.run / "sources.json")["items"]
        self.assertEqual(sources[2]["license"], "licensed")
        self.assertEqual(sources[2]["catalog_title"], "User declared")
        self.assertNotIn("catalog_title", sources[0])
        self.assertEqual(check_catalog(self.run), [])

    def test_catalog_rejects_duplicate_unknown_and_invalid_conditions(self):
        for obj in [
            {"titles": [{"title": "T", "match": {"source_ids": ["missing"]}}]},
            {"titles": [{"title": "T", "match": {"source_ids": ["A", "A"]}}]},
            {"titles": [{"title": "T", "match": {"source_ids": ["A"]}}, {"title": "U", "match": {"source_ids": ["A"]}}]},
            {"titles": [{"title": "T", "constraints": {"max_seconds_total": -1}}]},
            {"titles": [{"title": "T", "constraints": {"expires": "tomorrow"}}]},
            {"titles": [{"title": "T", "constraints": {"allowed_formats": "remix"}}]}]:
            with self.subTest(obj=obj):
                write_json(self.run / "supplied.json", obj)
                with self.assertRaises(RemixError):
                    import_catalog(self.run, self.run / "supplied.json")


if __name__ == "__main__":
    unittest.main()
