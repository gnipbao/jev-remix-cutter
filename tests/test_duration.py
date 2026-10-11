"""Longer user-requested edits retain integer timing and a bounded duration."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from jev_remix.common import RemixError, fingerprint, read_json, write_json
from jev_remix.planning import slots
from jev_remix.rendering import _validate_timeline
from jev_remix.workflow import init


class DurationTests(unittest.TestCase):
    def test_brief_accepts_two_minutes_and_five_minute_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for seconds in (120, 300):
                run = root / str(seconds)
                init(run, target_seconds=seconds, format="16:9")
                self.assertEqual(read_json(run / "brief.json")["target_seconds"], seconds)
            for seconds in (0, -1, 300.01):
                run = root / str(seconds)
                with self.assertRaises(RemixError):
                    init(run, target_seconds=seconds)
                self.assertFalse((run / "brief.json").exists())

    def test_two_minute_slots_cover_exact_output_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            for fps in (30, 60):
                run = Path(directory) / str(fps)
                init(run, target_seconds=120, fps=fps, format="16:9", pack="amv")
                write_json(run / "intent.json", {"version": 1, "theme": "synthetic timing",
                    "arc": ["intro", "action", "resolve"], "hook": "visible title", "avoid": []})
                audio = run / "timing.wav"
                audio.write_bytes(b"local timing fixture")
                write_json(run / "music.json", {"version": 1, "path": str(audio),
                    "fingerprint": fingerprint(audio), "fps": fps, "duration_us": 120_000_000,
                    "duration_frames": 120 * fps, "low_confidence": False, "warnings": [],
                    "beats": [{"frame": frame, "strength": 1}
                        for frame in range(0, 120 * fps, fps // 2)]})
                slots(run, density="2bar")
                result = read_json(run / "slots.json")
                self.assertEqual(result["duration_frames"], 120 * fps)
                rows = result["items"]
                self.assertEqual(rows[0]["start_f"], 0)
                self.assertEqual(rows[-1]["end_f"], 120 * fps)
                self.assertEqual(sum(row["end_f"] - row["start_f"] for row in rows), 120 * fps)
                self.assertTrue(all(a["end_f"] == b["start_f"] for a, b in zip(rows, rows[1:])))

    def test_renderer_accepts_two_minutes_and_enforces_same_limit(self):
        sources = [{"id": "synthetic", "media": {"duration_us": 301_000_000, "hdr": False}}]
        for seconds, fps in ((120, 30), (120, 60), (300, 30), (301, 30)):
            timeline = {"version": 2, "fps": fps, "width": 320, "height": 180,
                "duration_frames": seconds * fps, "items": [{"slot": "timing",
                    "out": {"start_f": 0, "end_f": seconds * fps},
                    "src": {"source": "synthetic", "in_us": 0, "out_us": seconds * 1_000_000},
                    "retime": {"speed": 1}, "source_audio": False}]}
            if seconds <= 300:
                self.assertEqual(_validate_timeline(timeline, sources)["synthetic"], sources[0])
            else:
                with self.assertRaisesRegex(RemixError, "up to 300 seconds"):
                    _validate_timeline(timeline, sources)


if __name__ == "__main__":
    unittest.main()
