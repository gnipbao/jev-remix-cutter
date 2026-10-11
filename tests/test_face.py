"""Isolated synthetic fixtures; never read or modify a live consent registry."""
from __future__ import annotations

import copy
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from jev_remix.common import (RemixError, file_hash, fingerprint, read_json,
                              read_jsonl, snapshot, write_json)
from jev_remix.face import (blur_frame, consent_add, consent_list, consent_revoke,
                            consent_verify, faces_apply, faces_match, faces_plan,
                            faces_prepare, faces_qa, faces_scan, guard_plan,
                            verify_labels, verify_render)


class ConsentAndGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="remix-consent-")
        self.root = Path(self.temporary.name)
        self.run = self.root / "run"
        self.run.mkdir()
        self.registry = self.root / "isolated-consents"
        self.scope = {"modes": ["swap"], "purposes": ["personal_creation"],
                      "platforms": ["local"], "projects": ["synthetic-test"], "expires": "2999-01-01"}
        self.plan = {"version": 1, "purpose": "personal_creation", "platform": "local", "items": [
            {"cluster": "c0001", "mode": "swap", "subject": "id_subject", "target": "id_target"}]}
        self.scan = {"nsfw": False, "deceptive_intent": .1,
                     "clusters": [{"cluster": "c0001", "age_estimate": 30, "nsfw": False}]}
        self.matches = {"items": [{"cluster": "c0001", "identity_id": "id_subject", "status": "confirmed"}]}
        write_json(self.run / "brief.json", {"project": "synthetic-test"})
        self.document = self.root / "synthetic-document.png"
        self.reference = self.root / "synthetic-reference.png"
        self.document.write_bytes(b"synthetic fixture, not a real consent")
        self.reference.write_bytes(b"synthetic reference, not a real person")

    def tearDown(self):
        self.temporary.cleanup()

    def install_fixture_registry(self):
        doc = self.registry / "docs" / "synthetic-document.png"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_bytes(self.document.read_bytes())
        rows = [{"identity_id": "id_" + role, "display_name": "Synthetic " + role, "relation": "other",
                 "roles": [role], "scope": copy.deepcopy(self.scope), "revoked": False,
                 "doc": {"file": "docs/synthetic-document.png", "sha256": file_hash(doc)},
                 "refs": [], "created_via": "synthetic-test"} for role in ("subject", "target")]
        write_json(self.registry / "registry.json", {"version": 1, "items": rows})
        write_json(self.run / "face-plan.json", self.plan)
        return rows

    def modify_record(self, role, **values):
        obj = read_json(self.registry / "registry.json")
        next(x for x in obj["items"] if x["identity_id"] == "id_" + role).update(values)
        write_json(self.registry / "registry.json", obj)

    def guard(self, plan=None, scan=None, matches=None, require_backend=False):
        return guard_plan(self.run, plan or self.plan, scan or self.scan,
                          self.matches if matches is None else matches,
                          config_dir=self.registry, require_backend=require_backend)

    def test_non_terminal_registration_refuses_before_any_file_writes(self):
        with patch("jev_remix.face.sys.stdin.isatty", return_value=False), self.assertRaises(RemixError) as caught:
            consent_add("Synthetic user", self.document, [self.reference], "subject,target", self.scope,
                        config_dir=self.registry)
        self.assertEqual(caught.exception.code, 4)
        self.assertFalse(self.registry.exists())

    def test_exact_phrase_required_without_partial_registration(self):
        with patch("jev_remix.face.sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="yes"), redirect_stdout(StringIO()):
            with self.assertRaises(RemixError) as caught:
                consent_add("Synthetic user", self.document, [self.reference], "subject,target", self.scope,
                            config_dir=self.registry)
        self.assertEqual(caught.exception.code, 4)
        self.assertFalse(self.registry.exists())

    def test_fixture_registration_list_and_revoke(self):
        # Mock interactive confirmation only inside a temporary synthetic registry.
        with patch("jev_remix.face.sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="我确认已取得 Synthetic user 的书面单独同意"), redirect_stdout(StringIO()):
            added = consent_add("Synthetic user", self.document, [self.reference], "subject,target", self.scope,
                                config_dir=self.registry)
        row = consent_list(self.registry)[0]
        self.assertEqual(row["identity_id"], added["identity_id"])
        self.assertFalse(row["revoked"])
        registry = read_json(self.registry / "registry.json")
        doc = self.registry / registry["items"][0]["doc"]["file"]
        self.assertEqual(file_hash(doc), file_hash(self.document))
        self.assertEqual(doc.stat().st_mode & 0o777, 0o600)
        consent_revoke(added["identity_id"], self.registry)
        self.assertTrue(consent_list(self.registry)[0]["revoked"])

    def test_registration_rejects_expired_or_invalid_scope(self):
        for scope in [{**self.scope, "expires": "1900-01-01"}, {**self.scope, "platforms": []},
                      {**self.scope, "modes": ["anonymize"]}]:
            with self.subTest(scope=scope), patch("jev_remix.face.sys.stdin.isatty", return_value=True):
                with self.assertRaises(RemixError) as caught:
                    consent_add("Synthetic user", self.document, [self.reference], "subject", scope,
                                config_dir=self.registry)
                self.assertEqual(caught.exception.code, 4)
                self.assertFalse(self.registry.exists())

    def test_verify_both_parties_then_revoke_invalidates_plan(self):
        self.install_fixture_registry()
        verified = consent_verify(self.run, self.registry)
        self.assertEqual({x["role"] for x in verified["verified"]}, {"subject", "target"})
        consent_revoke("id_subject", self.registry)
        with self.assertRaises(RemixError) as caught:
            consent_verify(self.run, self.registry)
        self.assertEqual(str(caught.exception), "subject_not_consented")
        self.assertEqual(caught.exception.code, 4)

    def test_expired_role_scope_purpose_platform_and_project_fail_closed(self):
        for change in [
            {"scope": {**self.scope, "expires": "1900-01-01"}},
            {"roles": ["target"]},
            {"scope": {**self.scope, "purposes": ["other"]}},
            {"scope": {**self.scope, "platforms": ["other"]}},
            {"scope": {**self.scope, "projects": ["other"]}},
            {"scope": {**self.scope, "modes": []}}]:
            with self.subTest(change=change):
                self.install_fixture_registry()
                self.modify_record("subject", **change)
                with self.assertRaises(RemixError) as caught:
                    consent_verify(self.run, self.registry)
                self.assertEqual(caught.exception.code, 4)

    def test_wildcard_scope_is_explicit_and_document_hash_is_checked(self):
        self.install_fixture_registry()
        self.modify_record("subject", scope={**self.scope, "projects": ["*"], "purposes": ["*"], "platforms": ["*"]})
        self.assertTrue(consent_verify(self.run, self.registry)["ok"])
        (self.registry / "docs/synthetic-document.png").write_bytes(b"changed")
        with self.assertRaises(RemixError) as caught:
            consent_verify(self.run, self.registry)
        self.assertEqual(caught.exception.details["reason"], "document_missing_or_changed")

    def test_missing_subject_or_target_are_distinct_errors(self):
        for role in ("subject", "target"):
            with self.subTest(role=role):
                self.install_fixture_registry()
                altered = copy.deepcopy(self.plan)
                altered["items"][0][role] = "unknown"
                with self.assertRaises(RemixError) as caught:
                    self.guard(plan=altered)
                self.assertEqual(str(caught.exception), role + "_not_consented")

    def test_unknown_nonfinite_or_boolean_age_cannot_enable_synthesis(self):
        self.install_fixture_registry()
        for mode in ("swap", "anonymize"):
            for age in (None, "unknown", True, float("nan"), float("inf")):
                with self.subTest(mode=mode, age=age):
                    scan = copy.deepcopy(self.scan)
                    scan["clusters"][0]["age_estimate"] = age
                    plan = copy.deepcopy(self.plan)
                    plan["items"][0]["mode"] = mode
                    with self.assertRaises(RemixError) as caught:
                        self.guard(plan=plan, scan=scan)
                    self.assertEqual(caught.exception.code, 5)

    def test_minor_age_buffer_blocks_swap_and_anonymize(self):
        self.install_fixture_registry()
        for mode in ("swap", "anonymize"):
            with self.subTest(mode=mode):
                plan, scan = copy.deepcopy(self.plan), copy.deepcopy(self.scan)
                plan["items"][0]["mode"] = mode
                scan["clusters"][0]["age_estimate"] = 20.9
                with self.assertRaises(RemixError) as caught:
                    self.guard(plan, scan)
                self.assertEqual(caught.exception.code, 4)
                self.assertIn("possible_minor", str(caught.exception))

    def test_nsfw_flag_disables_entire_face_run_and_unknown_blocks_synthesis(self):
        self.install_fixture_registry()
        keep = {**self.plan, "items": [{"cluster": "c0001", "mode": "keep"}]}
        for scan in [{**self.scan, "nsfw": True}, {**self.scan, "clusters": [{"cluster": "c0001", "nsfw": True}]}]:
            with self.subTest(scan=scan), self.assertRaises(RemixError) as caught:
                self.guard(plan=keep, scan=scan)
            self.assertEqual(caught.exception.code, 4)
        for nsfw in (None, "unknown"):
            with self.subTest(nsfw=nsfw), self.assertRaises(RemixError) as caught:
                self.guard(scan={**self.scan, "nsfw": nsfw})
            self.assertEqual(caught.exception.code, 5)

    def test_unknown_or_mismatched_identity_cannot_be_approved_by_plan(self):
        self.install_fixture_registry()
        for matches in [{"items": []}, {"items": [{"cluster": "c0001", "status": "unknown", "identity_id": "id_subject"}]},
                        {"items": [{"cluster": "c0001", "status": "confirmed", "identity_id": "id_target"}]}]:
            with self.subTest(matches=matches), self.assertRaises(RemixError) as caught:
                self.guard(matches=matches)
            self.assertEqual(caught.exception.code, 5)

    def test_deceptive_probability_and_unknown_policy_evidence_block_swap(self):
        self.install_fixture_registry()
        for value in (.5, .8, True):
            with self.subTest(value=value), self.assertRaises(RemixError) as caught:
                self.guard(scan={**self.scan, "deceptive_intent": value})
            self.assertEqual(caught.exception.code, 4)
        for value in (None, "unknown", float("nan"), -1):
            with self.subTest(value=value), self.assertRaises(RemixError) as caught:
                self.guard(scan={**self.scan, "deceptive_intent": value})
            self.assertEqual(caught.exception.code, 5)

    def test_synthesis_is_unavailable_even_with_complete_fixture_evidence(self):
        self.install_fixture_registry()
        for mode in ("swap", "anonymize"):
            with self.subTest(mode=mode):
                plan = copy.deepcopy(self.plan)
                plan["items"][0]["mode"] = mode
                with self.assertRaises(RemixError) as caught:
                    self.guard(plan=plan, require_backend=True)
                self.assertEqual(caught.exception.code, 5)

    def test_both_explicit_and_implicit_labels_are_mandatory_for_synthesis(self):
        for mode in ("swap", "anonymize"):
            for explicit, implicit in ((False, True), (True, False), (False, False), (1, True)):
                with self.subTest(mode=mode, explicit=explicit, implicit=implicit):
                    with self.assertRaises(RemixError) as caught:
                        verify_labels({"items": [{"mode": mode}]}, explicit, implicit)
                    self.assertEqual(caught.exception.code, 4)
        self.assertTrue(verify_labels(self.plan, True, True)["ok"])
        self.assertTrue(verify_labels({"items": [{"mode": "blur"}]}, False, False)["ok"])

    def test_registry_and_document_symlinks_are_rejected(self):
        self.install_fixture_registry()
        link = self.root / "consent-link"
        link.symlink_to(self.registry)
        with self.assertRaises(RemixError):
            consent_list(link)
        doc = self.registry / "docs/synthetic-document.png"
        doc.unlink()
        doc.symlink_to(self.document)
        with self.assertRaises(RemixError) as caught:
            consent_verify(self.run, self.registry)
        self.assertEqual(caught.exception.code, 4)


class SyntheticBlurTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import cv2
            import numpy
        except ImportError:
            raise unittest.SkipTest("OpenCV/numpy optional face dependencies are unavailable")
        if not shutil.which("ffmpeg"):
            raise unittest.SkipTest("FFmpeg is unavailable")
        cls.cv2, cls.np = cv2, numpy

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="remix-blur-")
        self.root = Path(self.temporary.name)
        self.run = self.root / "run"
        self.run.mkdir()
        self.registry = self.root / "isolated-consents"
        self.video = self.root / "synthetic.mp4"
        writer = self.cv2.VideoWriter(str(self.video), self.cv2.VideoWriter_fourcc(*"mp4v"), 6, (64, 64))
        if not writer.isOpened():
            self.skipTest("OpenCV test encoder unavailable")
        rng = self.np.random.default_rng(42)
        for _ in range(12):
            writer.write(rng.integers(0, 256, (64, 64, 3), dtype=self.np.uint8))
        writer.release()
        sources = {"version": 1, "items": [{"id": "synthetic", "path": str(self.video),
            "fingerprint": fingerprint(self.video), "license": "own", "self_shot": True,
            "media": {"fps": {"num": 6, "den": 1}, "duration_us": 2_000_000}}]}
        self.timeline = {"version": 2, "fps": 6, "width": 64, "height": 64, "duration_frames": 12,
            "items": [{"slot": "one", "src": {"source": "synthetic", "in_us": 0, "out_us": 2_000_000},
                       "out": {"start_f": 0, "end_f": 12}}]}
        write_json(self.run / "brief.json", {"project": "synthetic-test"})
        write_json(self.run / "sources.json", sources)
        write_json(self.run / "timeline.json", self.timeline)
        self.scan = {"version": 1, "backend": "synthetic-fixture", "clusters": [], "nsfw": "unknown",
            "source_ranges": {"synthetic": {"fps": {"num": 6, "den": 1}, "intervals": [{"start_f": 0, "end_f": 12}]}},
            "limitations": ["Synthetic test fixtures contain no real people. Identity removal is not verified."],
            "snapshot": snapshot(self.run, ["timeline.json", "sources.json"])}
        write_json(self.run / "faces.json", self.scan)

    def tearDown(self):
        self.temporary.cleanup()

    def full_blur(self):
        return faces_plan(self.run, default="blur", full_frame=True, config_dir=self.registry)

    def test_blur_changes_selected_pixels_but_keeps_unselected_region(self):
        pixels = self.np.random.default_rng(2).integers(0, 256, (80, 80, 3), dtype=self.np.uint8)
        result = blur_frame(pixels, [[16, 16, 16, 16]])
        self.assertTrue(self.np.array_equal(result[:8], pixels[:8]))
        self.assertLess(result[16:32, 16:32].std(), pixels[16:32, 16:32].std() * .2)
        self.assertFalse(self.np.array_equal(result, pixels))
        # The primitive cannot claim biometric anonymity from pixel blur alone.

    def test_scan_reports_missing_models_without_inventing_identity(self):
        if not hasattr(self.cv2, "CascadeClassifier"):
            with self.assertRaises(RemixError) as caught:
                faces_scan(self.run)
            self.assertEqual(caught.exception.code, 5)
            self.assertIn("CascadeClassifier", str(caught.exception))
            return
        scan = faces_scan(self.run)
        self.assertEqual(scan["nsfw"], "unknown")
        self.assertEqual(scan["source_ranges"]["synthetic"]["intervals"], [{"start_f": 0, "end_f": 12}])
        for track in scan["clusters"]:
            self.assertEqual(track["identity"], "unknown")
            self.assertIsNone(track["age_estimate"])
        match = faces_match(self.run, self.registry)
        self.assertFalse(match["available"])
        self.assertTrue(all(x["status"] == "unknown" and x["identity_id"] is None for x in match["items"]))

    def test_tracking_interpolates_three_frame_gap_and_splits_longer_gap(self):
        detector = Mock()
        detector.empty.return_value = False
        box = [[16, 16, 24, 24]]
        detector.detectMultiScale.side_effect = [box, [], [], [], box, [], [], [], [], box, [], []]
        # The classifier is a deterministic fixture; decoding and tracking are real.
        with patch.object(self.cv2, "CascadeClassifier", return_value=detector, create=True):
            scan = faces_scan(self.run)
        self.assertEqual(len(scan["clusters"]), 2)
        first, second = scan["clusters"]
        self.assertEqual([x["frame"] for x in first["detections"]], [0, 1, 2, 3, 4])
        self.assertEqual([x["frame"] for x in first["detections"] if x["predicted"]], [1, 2, 3])
        self.assertEqual([x["frame"] for x in second["detections"]], [9])
        self.assertEqual(first["identity"], "unknown")
        self.assertIsNone(first["age_estimate"])
        match = faces_match(self.run, self.registry)
        self.assertFalse(match["available"])
        self.assertEqual({x["status"] for x in match["items"]}, {"unknown"})

    def test_scan_decodes_only_selected_source_frame_ranges(self):
        timeline = copy.deepcopy(self.timeline)
        timeline["items"][0]["src"].update(in_us=1_000_000, out_us=1_333_333)
        write_json(self.run / "timeline.json", timeline)
        detector = Mock()
        detector.empty.return_value = False
        detector.detectMultiScale.return_value = []
        with patch.object(self.cv2, "CascadeClassifier", return_value=detector, create=True):
            scan = faces_scan(self.run)
        self.assertEqual(detector.detectMultiScale.call_count, 2)
        self.assertEqual(scan["source_ranges"]["synthetic"]["intervals"], [{"start_f": 6, "end_f": 8}])

    def test_prepare_counts_selected_frames_local_only_and_license_evidence(self):
        self.full_blur()
        prepared = faces_prepare(self.run, config_dir=self.registry)
        self.assertEqual(prepared["frames_to_process"], 12)
        self.assertFalse(prepared["uploads_images"])
        self.assertEqual(prepared["transfers"], [])
        self.assertEqual(prepared["cost"]["amount"], 0)
        self.assertTrue(prepared["license"]["commercial_allowed"])

    def test_apply_cache_audit_qa_and_renderer_share_processed_sources(self):
        self.full_blur()
        applied = faces_apply(self.run, config_dir=self.registry)
        output = applied["sources"]["synthetic"]
        self.assertEqual(output["processed_frames"], 12)
        cache = Path(output["path"])
        modified = cache.stat().st_mtime_ns
        repeated = faces_apply(self.run, config_dir=self.registry)
        self.assertEqual(repeated["sources"]["synthetic"]["cache_key"], output["cache_key"])
        self.assertEqual(cache.stat().st_mtime_ns, modified)
        audit = read_jsonl(self.run / "face-audit.jsonl")
        self.assertEqual(len(audit), 2)
        required = {"ts", "run", "cluster", "mode", "source", "subject", "target", "consent_docs", "backend", "backend_version", "cache_key", "frames", "label", "full_frame"}
        self.assertTrue(required.issubset(audit[0]))
        self.assertEqual(audit[0]["consent_docs"], [])
        self.assertEqual(verify_render(self.run, self.timeline, self.registry)["synthetic"]["path"], str(cache))
        qa = faces_qa(self.run, self.registry)
        self.assertIsNone(qa["ok"])
        self.assertEqual(qa["status"], "visual_review_required")
        self.assertEqual(qa["checks"]["residual_identity"], "unavailable")
        from jev_remix.rendering import _face_sources
        src = read_json(self.run / "sources.json")["items"][0]
        replaced = _face_sources(self.run, self.timeline, {"synthetic": src})
        self.assertEqual(replaced["synthetic"]["path"], str(cache))

    def test_cache_changes_and_missing_planned_sources_block_verification(self):
        self.full_blur()
        applied = faces_apply(self.run, config_dir=self.registry)
        cache = Path(applied["sources"]["synthetic"]["path"])
        cache.write_bytes(cache.read_bytes() + b"changed")
        with self.assertRaises(RemixError) as caught:
            verify_render(self.run, config_dir=self.registry)
        self.assertEqual(caught.exception.code, 3)
        applied["sources"] = {}
        write_json(self.run / "face-applied.json", applied)
        with self.assertRaises(RemixError) as caught:
            verify_render(self.run, config_dir=self.registry)
        self.assertEqual(caught.exception.code, 3)

    def test_stale_source_timeline_and_scan_invalidate_plans(self):
        self.full_blur()
        self.timeline["duration_frames"] = 13
        write_json(self.run / "timeline.json", self.timeline)
        with self.assertRaises(RemixError) as caught:
            faces_prepare(self.run, config_dir=self.registry)
        self.assertEqual(caught.exception.code, 3)

    def test_upload_permission_does_not_enable_unimplemented_api_backend(self):
        self.full_blur()
        with self.assertRaises(RemixError) as caught:
            faces_apply(self.run, "api-test", config_dir=self.registry)
        self.assertEqual(caught.exception.code, 4)
        with self.assertRaises(RemixError) as caught:
            faces_apply(self.run, "api-test", allow_image_upload=True, config_dir=self.registry)
        self.assertEqual(caught.exception.code, 5)

    def test_full_frame_mode_cannot_request_synthesis(self):
        with self.assertRaises(RemixError) as caught:
            faces_plan(self.run, mappings=["source:synthetic=anonymize"], config_dir=self.registry)
        self.assertEqual(caught.exception.code, 5)


if __name__ == "__main__":
    unittest.main()
