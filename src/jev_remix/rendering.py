"""Deterministic CPU frame mapper, compositor, mixer and delivery verification.

Unsupported effects fail explicitly. RIFE, HDR grading and face synthesis require
separate backends and cannot silently become nearest-frame or unprocessed output.
"""
from __future__ import annotations
from collections import Counter, deque
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import wave

from .common import (RemixError, command, read_json, write_json, fingerprint,
                     digest, check_sources, ensure_snapshot, ensure_decision_hashes, thresholds, snapshot, finite)
from .media import _deps, probe, safe_output


def velocity_integral(points, t=1.0):
    """Analytic integral of a piecewise-linear speed curve on output time."""
    finite(t, "Velocity integration time", 0, 1)
    if not points or len(points) < 2 or points[0][0] != 0 or points[-1][0] != 1:
        raise RemixError("Velocity points must cover normalized output time 0 to 1")
    result, last = 0.0, -1.0
    for pair in points:
        if len(pair) != 2 or not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in pair):
            raise RemixError("Velocity points must be finite [time,speed] pairs")
        if not 0 <= pair[0] <= 1 or pair[0] <= last or pair[1] <= 0:
            raise RemixError("Velocity times must increase and speeds must be positive")
        last = pair[0]
    for (a, va), (b, vb) in zip(points, points[1:]):
        x = min(b, max(a, t)) - a
        result += va*x + .5*(vb-va)*x*x/(b-a)
        if t <= b:
            break
    return result


def source_times(item, fps):
    """Frame timestamps from the speed integral, normalized to the source range.

    Points specify the relative velocity profile. The source range and output
    frame count determine its absolute speed; the scale is reported by render.
    """
    np, _ = _deps()
    n = item["out"]["end_f"] - item["out"]["start_f"]
    start, end = item["src"]["in_us"], item["src"]["out_us"]
    r = item.get("retime") or {}
    points = r.get("points", [[0, r.get("speed", 1)], [1, r.get("speed", 1)]])
    area = velocity_integral(points)
    values = np.array([start + (end-start)*velocity_integral(points, f/n)/area for f in range(n)])
    if r.get("reverse"):
        values = start + end - values - min((end-start)/n, 1e6/fps)
        values = np.clip(values, start, end-1)
    return values


def _caption_style(row, width, height):
    """ASS style tags from bounded, non-executable caption options."""
    tags = []
    if "position" in row:
        if row["position"] not in ("center", "bottom"):
            raise RemixError("Caption position must be center or bottom")
        tags.append(f"\\an5\\pos({width//2},{height//2})" if row["position"] == "center" else "\\an2")
    if "font_size" in row:
        size = row["font_size"]
        if not isinstance(size, int) or isinstance(size, bool) or not 16 <= size <= 128:
            raise RemixError("Caption font_size must be an integer from 16 to 128")
        tags.append(f"\\fs{size}")
    if "font_family" in row:
        family = row["font_family"]
        if not isinstance(family, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,63}", family):
            raise RemixError("Caption font_family must contain only safe font-name characters")
        tags.append("\\fn"+family)
    return "{"+"".join(tags)+"}" if tags else ""


def _validate_timeline(timeline, sources):
    if timeline.get("version") != 2:
        raise RemixError("Renderer requires timeline version 2")
    fps, width, height = timeline.get("fps"), timeline.get("width"), timeline.get("height")
    if not isinstance(fps, int) or isinstance(fps, bool) or not 1 <= fps <= 120:
        raise RemixError("Output fps must be an integer from 1 to 120")
    if not all(isinstance(v, int) and not isinstance(v, bool) and 16 <= v <= 4096 and v % 2 == 0 for v in (width, height)):
        raise RemixError("Output dimensions must be even integers between 16 and 4096")
    by_id = {x["id"]: x for x in sources}
    cursor = 0
    for item in timeline.get("items", []):
        out, src = item.get("out", {}), item.get("src", {})
        if (out.get("start_f") != cursor or not isinstance(out.get("end_f"), int) or
                isinstance(out["end_f"], bool) or out["end_f"] <= cursor):
            raise RemixError("Timeline must contain contiguous, positive frame intervals")
        cursor = out["end_f"]
        if src.get("source") not in by_id:
            raise RemixError("Timeline references an unknown source")
        source = by_id[src["source"]]
        interval = source.get("range", {"start_us": 0, "end_us": source["media"]["duration_us"]})
        if not all(isinstance(src.get(k), int) and not isinstance(src[k], bool) for k in ("in_us", "out_us")) or not interval["start_us"] <= src["in_us"] < src["out_us"] <= interval["end_us"]:
            raise RemixError("Timeline source interval is outside its ingested range")
        if source["media"].get("hdr"):
            raise RemixError("HDR source requires an explicit tone-mapping backend before core rendering", 5)
        retime = item.get("retime") or {}
        if set(retime) - {"speed", "points", "interp", "preset", "reverse"}:
            raise RemixError("Unsupported retime fields", 5)
        if retime.get("interp", "nearest") not in ("nearest", "blend"):
            raise RemixError("Requested interpolation backend is unavailable; use nearest/blend explicitly", 5)
        source_times(item, fps)
        if retime.get("speed") is not None:
            finite(retime["speed"], "Retime speed", .01, 100)
            expected = (src["out_us"]-src["in_us"])*fps/(1e6*(out["end_f"]-out["start_f"]))
            if abs(retime["speed"]-expected) > 1e-4:
                raise RemixError("Constant retime.speed is inconsistent with source/output durations")
        transition = item.get("transition_in") or {"type": "hard_cut"}
        if transition.get("type") not in ("hard_cut", "flash_white", "flash_black", "dissolve", "wipeleft"):
            raise RemixError(f"Unsupported transition: {transition.get('type')}", 5)
        if set(transition) - {"type", "frames"}:
            raise RemixError("Unsupported transition fields", 5)
        length = transition.get("frames", 0)
        if not isinstance(length, int) or isinstance(length, bool) or not 0 <= length <= out["end_f"] - out["start_f"]:
            raise RemixError("Transition frame length is outside its slot")
        look = item.get("look") or {}
        if look.get("preset", "none") not in ("none", "bw", "warm", "cool"):
            raise RemixError(f"Unsupported Look preset: {look.get('preset')}", 5)
        if set(look) - {"preset", "strength", "brightness", "contrast", "saturation", "vignette", "letterbox"}:
            raise RemixError("Unsupported Look fields; color matching/LUT need explicit backend", 5)
        for name, low, high in (("strength", 0, 1), ("brightness", -1, 1), ("contrast", 0, 8),
                                 ("saturation", 0, 8), ("vignette", 0, 1), ("letterbox", 0, .45)):
            if name in look:
                finite(look[name], "Look " + name, low, high)
        transform = item.get("transform") or {}
        if set(transform) - {"keys"}:
            raise RemixError("Unsupported transform fields", 5)
        last_f = -1
        for key in transform.get("keys", []):
            if set(key) - {"f", "scale", "x", "y", "rotation"}:
                raise RemixError("Unsupported transform key", 5)
            if (not isinstance(key.get("f"), int) or isinstance(key["f"], bool) or key["f"] <= last_f or
                    not 0 <= key["f"] <= out["end_f"]-out["start_f"]):
                raise RemixError("Transform keys must have increasing integer f")
            for name in ("scale", "x", "y", "rotation"):
                if name in key:
                    finite(key[name], "Transform " + name)
            if key.get("scale", 1) <= 0:
                raise RemixError("Transform scale must be positive")
            last_f = key["f"]
        for effect in item.get("fx", []):
            if effect.get("type") not in ("zoom", "shake", "flash_white", "flash_black", "freeze"):
                raise RemixError(f"Unsupported effect: {effect.get('type')}", 5)
            if set(effect) - {"type", "at_f", "frames", "scale", "px", "kind", "strength"}:
                raise RemixError("Unsupported effect fields", 5)
            if effect.get("type") == "shake" and effect.get("kind", "xy") != "xy":
                raise RemixError("Core shake supports kind xy only", 5)
            for name, low, high in (("scale", .01, 100), ("px", 0, 2048), ("strength", 0, 1)):
                if name in effect:
                    finite(effect[name], "Effect " + name, low, high)
            a, n = effect.get("at_f", 0), effect.get("frames", 1)
            if not isinstance(a, int) or isinstance(a, bool) or not isinstance(n, int) or isinstance(n, bool) or a < 0 or n < 1 or a+n > out["end_f"]-out["start_f"]:
                raise RemixError("Effect interval is outside its slot")
        for sfx in item.get("sfx", []):
            if sfx.get("type") not in ("impact", "whoosh", "riser", "shutter"):
                raise RemixError("Unsupported sound effect", 5)
            if set(sfx) - {"type", "at_f", "gain", "duration_s"}:
                raise RemixError("Unsupported sound effect fields", 5)
            if not isinstance(sfx.get("at_f", 0), int) or isinstance(sfx.get("at_f", 0), bool) or not 0 <= sfx.get("at_f", 0) < out["end_f"]-out["start_f"]:
                raise RemixError("Sound effect starts outside its slot")
            finite(sfx.get("gain", .4), "Sound effect gain", 0, 16)
            finite(sfx.get("duration_s", .25), "Sound effect duration", .001, 5)
        if not isinstance(item.get("source_audio", False), bool):
            raise RemixError("source_audio must be a boolean")
        for name in ("fx_pack", "cuts", "montage", "cross_cut"):
            if item.get(name):
                raise RemixError(f"{name} needs an optional renderer backend", 5)
    if not cursor or cursor != timeline.get("duration_frames"):
        raise RemixError("Timeline duration_frames does not equal its frame intervals")
    if cursor > 300 * fps:
        raise RemixError("Core rendering currently supports output up to 300 seconds")
    audio = timeline.get("audio") or {}
    if set(audio) - {"path", "fingerprint", "gain", "start_us", "duck_music", "duration_us", "license", "licensed",
                     "replaces_source_audio", "narration", "items"}:
        raise RemixError("Unsupported audio fields", 5)
    finite(audio.get("gain", 1), "Music gain", 0, 16)
    if not isinstance(audio.get("start_us", 0), int) or isinstance(audio.get("start_us", 0), bool) or audio.get("start_us", 0) < 0:
        raise RemixError("Music start_us must be a nonnegative integer")
    if audio.get("path") and (not audio.get("fingerprint") or fingerprint(audio["path"]) != audio["fingerprint"]):
        raise RemixError("Timeline music fingerprint missing or changed; rerun music → assemble", 3)
    if audio.get("license", "unknown") not in ("own", "licensed", "unknown"):
        raise RemixError("Music license must be own, licensed or unknown")
    if audio.get("replaces_source_audio") and any(item.get("source_audio") for item in timeline["items"]):
        raise RemixError("Music claims to replace source audio while source_audio is enabled")
    if "items" in audio:
        # External-file source mappings are provenance metadata. Mixing reads
        # audio.path once; it does not render these intervals as another track.
        mappings = audio["items"]
        if not isinstance(mappings, list) or not mappings or not audio.get("path"):
            raise RemixError("Audio provenance items need a nonempty array and a rendered audio.path")
        if any(item.get("source_audio") for item in timeline["items"]):
            raise RemixError("External audio provenance cannot also enable per-shot source_audio")
        previous_end = 0
        for row in mappings:
            if not isinstance(row, dict) or set(row) - {"source", "in_us", "out_us", "start_f", "end_f", "source_audio", "unaltered"}:
                raise RemixError("Unsupported audio provenance fields")
            if not isinstance(row.get("source"), str) or row["source"] not in by_id:
                raise RemixError("Audio provenance references an unknown source")
            source = by_id[row["source"]]
            interval = source.get("range", {"start_us":0,"end_us":source["media"]["duration_us"]})
            if (not all(isinstance(row.get(k), int) and not isinstance(row[k], bool) for k in ("in_us", "out_us"))
                    or not interval["start_us"] <= row["in_us"] < row["out_us"] <= interval["end_us"]):
                raise RemixError("Audio provenance source interval is outside its ingested range")
            if (not all(isinstance(row.get(k), int) and not isinstance(row[k], bool) for k in ("start_f", "end_f"))
                    or not previous_end <= row["start_f"] < row["end_f"] <= cursor):
                raise RemixError("Audio provenance needs ordered, nonoverlapping output intervals inside the timeline")
            previous_end = row["end_f"]
            if not all(isinstance(row.get(k), bool) for k in ("source_audio", "unaltered")):
                raise RemixError("Audio provenance source_audio and unaltered must be booleans")
            if row["source_audio"] and not source["media"].get("audio"):
                raise RemixError("Audio provenance requested from a source with no audio")
            if row["source_audio"] and audio.get("replaces_source_audio"):
                raise RemixError("Source-derived audio cannot claim to replace the source audio")
            if row["unaltered"] and abs((row["out_us"]-row["in_us"])*fps - (row["end_f"]-row["start_f"])*1_000_000) > fps:
                raise RemixError("Unaltered audio provenance must preserve forward 1x duration")
    narration = audio.get("narration")
    if narration:
        if not isinstance(narration, dict) or set(narration) - {"path", "fingerprint", "gain", "start_f", "end_f", "authored_by", "authored", "license"}:
            raise RemixError("Unsupported narration fields", 5)
        a, b = narration.get("start_f", 0), narration.get("end_f", cursor)
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (a, b)) or not 0 <= a < b <= cursor:
            raise RemixError("Narration interval is outside timeline")
        if not narration.get("path") or not narration.get("fingerprint") or fingerprint(narration["path"]) != narration["fingerprint"]:
            raise RemixError("Narration fingerprint is missing or stale", 3)
        finite(narration.get("gain", 1), "Narration gain", 0, 16)
    for item in timeline["items"]:
        for overlay in item.get("overlays", []):
            if set(overlay) - {"text", "start_f", "end_f", "authored_by", "authored", "contribution", "position", "font_size", "font_family"} or not isinstance(overlay.get("text"), str):
                raise RemixError("Core supports text overlays only; graph/image compositing requires a backend", 5)
            a, b = overlay.get("start_f", item["out"]["start_f"]), overlay.get("end_f", item["out"]["end_f"])
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in (a, b)) or not item["out"]["start_f"] <= a < b <= item["out"]["end_f"]:
                raise RemixError("Overlay interval is outside its slot")
            _caption_style(overlay, width, height)
    return by_id


def _qa_timeline(timeline):
    fps = timeline["fps"]
    cfg = thresholds()["qa"]
    flashes, findings = [], []
    slots = timeline["items"]
    for item in slots:
        start = item["out"]["start_f"]
        tr = item.get("transition_in") or {}
        if tr.get("type") == "flash_white":
            flashes.append(start)
        flashes += [start+fx.get("at_f", 0) for fx in item.get("fx", []) if fx.get("type") == "flash_white"]
        dur = (item["out"]["end_f"]-start)/fps
        if dur > cfg["max_change_interval_s"] and not item.get("fx") and not item.get("transform"):
            findings.append({"level": "warn", "metric": "visual_change_interval", "slot": item["slot"], "seconds": dur})
    for f in flashes:
        if sum(f <= other < f+fps for other in flashes) > cfg["max_flash_per_second"]:
            findings.append({"level": "error", "metric": "flash_frequency", "frame": f})
            break
    ids = [item.get("shot_id", item.get("src", {}).get("shot_id")) for item in slots]
    for sid, count in Counter(x for x in ids if x).items():
        if count > 1:
            findings.append({"level": "warn", "metric": "repeated_shot", "shot_id": sid, "count": count})
    for a, b in zip(slots, slots[1:]):
        if a["src"]["source"] == b["src"]["source"]:
            findings.append({"level": "warn", "metric": "same_source_adjacent", "slot": b["slot"]})
    return findings


def qa(run):
    run = Path(run)
    timeline = read_json(run / "timeline.json")
    ensure_snapshot(run, timeline.get("dependencies", {}))
    ensure_decision_hashes(timeline)
    sources = check_sources(run)
    _validate_timeline(timeline, sources)
    findings = _qa_timeline(timeline)
    music_file = run / "music.json"
    if music_file.is_file():
        music = read_json(music_file)
        if fingerprint(music["path"]) != music["fingerprint"]:
            raise RemixError("Music source changed; rerun music → assemble", 3)
        grid = [x["frame"] for x in music.get("beats", [])]
        if music.get("low_confidence"):
            findings.append({"level": "warn", "metric": "beat_confidence", "confidence": music["confidence"]})
        if grid:
            errors = [min(abs(i["out"]["start_f"]-g) for g in grid) for i in timeline["items"][1:]]
            if errors and max(errors) > thresholds()["qa"]["beat_error_frames"]:
                findings.append({"level": "warn", "metric": "beat_alignment", "max_error_frames": max(errors)})
    latest = run / "render-result.json"
    measured = None
    if latest.is_file():
        previous = read_json(latest)
        if previous.get("timeline_hash") == digest(timeline):
            measured = read_json(previous["verification"])
    result = {"version": 1, "ok": not any(x["level"] == "error" for x in findings),
        "findings": findings, "render_verification": measured,
        "unmeasured": ["subject/hard-subtitle overlap", "perceptual loop seam", "silent narrative readout", "optical action-peak alignment"],
        "dependencies": snapshot(run, ["timeline.json", "music.json", "sources.json"])}
    write_json(run / "qa.json", result)
    return result


def _transform_frame(frame, item, f):
    np, cv2 = _deps()
    h, w = frame.shape[:2]
    keys = item.get("transform", {}).get("keys", [])
    scale, x, y, rotation = 1.0, 0.0, 0.0, 0.0
    if keys:
        positions = [k["f"] for k in keys]
        scale, x, y, rotation = [float(np.interp(f, positions, [k.get(name, default) for k in keys]))
                                for name, default in (("scale", 1), ("x", 0), ("y", 0), ("rotation", 0))]
    for fx in item.get("fx", []):
        at, duration = fx.get("at_f", 0), fx.get("frames", 1)
        if at <= f < at+duration:
            phase = (f-at) / max(1, duration-1)
            if fx["type"] == "zoom":
                scale *= 1+(fx.get("scale", 1.08)-1)*math.sin(math.pi*phase)
            elif fx["type"] == "shake":
                px = fx.get("px", 8)*(1-phase)
                x += math.sin((f-at)*2.4)*px
                y += math.cos((f-at)*3.1)*px
            elif fx["type"] in ("flash_white", "flash_black"):
                strength = max(0, min(1, fx.get("strength", 1)*(1-phase)))
                color = 255 if fx["type"] == "flash_white" else 0
                frame = np.clip(frame.astype(np.float32)*(1-strength)+color*strength, 0, 255).astype(np.uint8)
    if scale != 1 or x or y or rotation:
        matrix = cv2.getRotationMatrix2D((w/2, h/2), rotation, scale)
        matrix[:, 2] += (x, y)
        frame = cv2.warpAffine(frame, matrix, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)
    look = item.get("look") or {}
    preset, strength = look.get("preset", "none"), max(0, min(1, look.get("strength", 1)))
    original = frame.astype(np.float32)
    graded = original.copy()
    if preset == "bw":
        graded = cv2.cvtColor(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR).astype(np.float32)
    elif preset == "warm":
        graded *= np.array([.90, 1.0, 1.08], dtype=np.float32)
    elif preset == "cool":
        graded *= np.array([1.08, 1.0, .90], dtype=np.float32)
    graded = original*(1-strength)+graded*strength
    graded = (graded-127.5)*look.get("contrast", 1)+127.5+look.get("brightness", 0)*255
    frame = np.clip(graded, 0, 255).astype(np.uint8)
    if look.get("saturation") is not None:
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV).astype(np.float32)
        hsv[:, :, 1] = np.clip(hsv[:, :, 1]*look["saturation"], 0, 255)
        frame = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)
    if look.get("vignette"):
        yy, xx = np.mgrid[-1:1:complex(h), -1:1:complex(w)]
        mask = np.clip(1-look["vignette"]*(xx*xx+yy*yy)/2, 0, 1)
        frame = (frame*mask[:, :, None]).astype(np.uint8)
    if look.get("letterbox"):
        thickness = round(h*look["letterbox"])
        if thickness:
            frame[:thickness] = 0
            frame[-thickness:] = 0
    return frame


class _Decoder:
    def __init__(self, path, start_us, end_us, fps, width, height, reverse=False):
        self.np, _ = _deps()
        self.frame_bytes, self.shape = width*height*3, (height, width, 3)
        self.fps, self.start_us = fps, start_us
        self.log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen([shutil.which("ffmpeg"), "-v", "error", "-ss", f"{start_us/1e6:.6f}",
            "-i", str(path), "-t", f"{(end_us-start_us)/1e6:.6f}", "-an", "-vf",
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps},setsar=1",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"], stdout=subprocess.PIPE, stderr=self.log)
        self.index, self.current, self.next = -1, None, None
        self.cache = None
        if reverse:
            estimated = math.ceil((end_us-start_us)*fps/1e6)*self.frame_bytes
            if estimated > 512*1024**2:
                self.close()
                raise RemixError("Reverse segment exceeds the 512 MiB core frame-cache limit")
            self.cache = []
            while True:
                frame = self._read()
                if frame is None:
                    break
                self.cache.append(frame)
            if not self.cache:
                self.close()
                raise RemixError("No source frames decoded")

    def _read(self):
        raw = self.proc.stdout.read(self.frame_bytes)
        if not raw:
            return None
        if len(raw) != self.frame_bytes:
            raise RemixError("Incomplete decoded raw frame")
        return self.np.frombuffer(raw, dtype=self.np.uint8).reshape(self.shape).copy()

    def frame(self, time_us, blend=False):
        position = max(0, (time_us-self.start_us)*self.fps/1e6)
        index = int(math.floor(position)) if blend else int(round(position))
        if self.cache is not None:
            a = self.cache[min(index, len(self.cache)-1)]
            b = self.cache[min(index+1, len(self.cache)-1)]
        else:
            if self.current is None:
                self.current, self.next, self.index = self._read(), self._read(), 0
            while self.index < index and self.next is not None:
                self.current, self.next = self.next, self._read()
                self.index += 1
            a, b = self.current, self.next if self.next is not None else self.current
        if a is None:
            raise RemixError("No frame at selected source timestamp")
        if blend:
            alpha = position-math.floor(position)
            return (a.astype(self.np.float32)*(1-alpha)+b*alpha).astype(self.np.uint8)
        return a.copy()

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
        self.proc.communicate()
        self.log.close()


def _write_video(path, timeline, by_id, width, height):
    np, _ = _deps()
    fps = timeline["fps"]
    log = tempfile.TemporaryFile()
    encoder = subprocess.Popen([shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}", "-r", str(fps), "-i", "pipe:0", "-an", "-frames:v", str(timeline["duration_frames"]),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", path], stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=log)
    max_transition = max([i.get("transition_in", {}).get("frames", 0) for i in timeline["items"]]+[1])
    tail = deque(maxlen=max_transition)
    try:
        for item in timeline["items"]:
            src = item["src"]
            mapped = source_times(item, fps)
            decoder = _Decoder(by_id[src["source"]]["path"], src["in_us"], src["out_us"], fps*2, width, height,
                               reverse=bool(item.get("retime", {}).get("reverse")))
            previous = list(tail)
            tr = item.get("transition_in", {})
            count, kind = tr.get("frames", 0), tr.get("type", "hard_cut")
            freeze = {}
            try:
                for f, timestamp in enumerate(mapped):
                    frame = decoder.frame(timestamp, item.get("retime", {}).get("interp") == "blend")
                    for fx in item.get("fx", []):
                        if fx["type"] == "freeze" and fx.get("at_f", 0) <= f < fx.get("at_f", 0)+fx.get("frames", 1):
                            key = fx.get("at_f", 0)
                            freeze.setdefault(key, frame.copy())
                            frame = freeze[key].copy()
                    frame = _transform_frame(frame, item, f)
                    if f < count and kind != "hard_cut":
                        alpha = (f+1)/count
                        if kind in ("flash_white", "flash_black"):
                            color = 255 if kind == "flash_white" else 0
                            frame = (frame.astype(np.float32)*alpha+color*(1-alpha)).astype(np.uint8)
                        elif previous:
                            old = previous[min(max(0, len(previous)-count)+f, len(previous)-1)]
                            if kind == "dissolve":
                                frame = (old.astype(np.float32)*(1-alpha)+frame*alpha).astype(np.uint8)
                            elif kind == "wipeleft":
                                edge = round(width*alpha)
                                frame[:, edge:] = old[:, edge:]
                    encoder.stdin.write(frame.tobytes())
                    tail.append(frame.copy())
            finally:
                decoder.close()
        encoder.stdin.close()
        encoder.wait()
        if encoder.returncode:
            log.seek(0)
            raise RemixError("Video encoder failed: " + log.read().decode(errors="replace")[-1200:])
    except (BrokenPipeError, OSError) as e:
        log.seek(0)
        raise RemixError("Video pipe failed: " + log.read().decode(errors="replace")[-1200:]) from e
    finally:
        if encoder.poll() is None:
            encoder.terminate()
            encoder.wait()
        log.close()


def _pcm(path, sr, start=0, duration=None, channels=1):
    """Decode mono by default, or return frame-major interleaved stereo PCM."""
    if not isinstance(channels, int) or isinstance(channels, bool) or channels not in (1, 2):
        raise RemixError("PCM decoding supports one or two channels", 5)
    np, _ = _deps()
    args = ["ffmpeg", "-v", "error", "-ss", f"{start:.6f}", "-i", path, "-vn", "-ac", str(channels), "-ar", str(sr)]
    if duration is not None:
        args += ["-t", f"{duration:.6f}"]
    data = np.frombuffer(command(args+["-f", "f32le", "pipe:1"]).stdout, dtype="<f4").copy()
    return data if channels == 1 else data.reshape(-1, channels)


def _mix_audio(path, timeline, by_id):
    np, _ = _deps()
    sr, fps = 48000, timeline["fps"]
    length = round(timeline["duration_frames"]*sr/fps)
    audio = timeline.get("audio") or {}
    music_channels = 1
    source_channels = {}
    if audio.get("path"):
        if audio.get("fingerprint") and fingerprint(audio["path"]) != audio["fingerprint"]:
            raise RemixError("Timeline audio changed; rerun music → slots → score → cast → assemble", 3)
        music_channels = probe(audio["path"], video=False).get("audio_channels") or 1
    for item in timeline["items"]:
        if item.get("source_audio", False):
            sid = item["src"]["source"]
            if not by_id[sid]["media"].get("audio"):
                raise RemixError("source_audio requested from a source with no audio")
            if sid not in source_channels:
                source_channels[sid] = by_id[sid]["media"].get("audio_channels") or probe(
                    by_id[sid]["path"], video=False).get("audio_channels") or 1
    channels = 2 if max([music_channels, *source_channels.values()]) >= 2 else 1
    music, voices, effects = [np.zeros((length, channels)) for _ in range(3)]

    def mix_pcm(source, start=0, duration=None, input_channels=1):
        data = _pcm(source, sr, start, duration, channels=min(2, input_channels))
        if data.ndim == 1:
            data = data[:, None]
        return np.repeat(data, 2, axis=1) if channels == 2 and data.shape[1] == 1 else data

    if audio.get("path"):
        data = mix_pcm(audio["path"], audio.get("start_us", 0)/1e6, length/sr, music_channels)
        if len(data) < length-round(sr/fps):
            raise RemixError("Music is shorter than the timeline; edit or choose a longer track")
        music[:min(length, len(data))] = data[:length]*audio.get("gain", 1)
    for item in timeline["items"]:
        a, b = [round(item["out"][k]*sr/fps) for k in ("start_f", "end_f")]
        keep = item.get("source_audio", False)
        if keep:
            src = item["src"]
            if not by_id[src["source"]]["media"].get("audio"):
                raise RemixError("source_audio requested from a source with no audio")
            curve = item.get("retime", {}).get("points", [])
            if item.get("retime", {}).get("reverse") or (curve and any(p[1] != curve[0][1] for p in curve)):
                raise RemixError("Core source-audio mix supports constant forward retime only", 5)
            data = mix_pcm(by_id[src["source"]]["path"], src["in_us"]/1e6,
                           (src["out_us"]-src["in_us"])/1e6, source_channels[src["source"]])
            gain = keep.get("gain", 1) if isinstance(keep, dict) else 1
            if len(data):
                positions = np.linspace(0, len(data)-1, b-a)
                for channel in range(channels):
                    voices[a:b, channel] += np.interp(positions, np.arange(len(data)), data[:, channel])*gain
            # Conservative fixed ducking during declared dialogue intervals.
            if audio.get("duck_music", True):
                music[a:b] *= .35
        for sfx in item.get("sfx", []):
            at = a+round(sfx.get("at_f", 0)*sr/fps)
            duration = float(sfx.get("duration_s", .25 if sfx["type"] == "impact" else .35))
            if not math.isfinite(duration) or not 0 < duration <= 5:
                raise RemixError("Sound effect duration must be between 0 and 5 seconds")
            t = np.arange(round(duration*sr))/sr
            kind = sfx["type"]
            if kind == "impact":
                signal = np.sin(2*np.pi*(70*t-30*t*t))*np.exp(-t*18)
            elif kind == "shutter":
                signal = np.random.default_rng(0).normal(0, .3, len(t))*np.exp(-t*70)
            elif kind == "whoosh":
                signal = np.random.default_rng(0).normal(0, .12, len(t))*np.sin(np.pi*t/duration)**2
            else:
                signal = np.sin(2*np.pi*(180*t+700*t*t))*np.linspace(0, .3, len(t))
            n = max(0, min(len(signal), length-at))
            if at < 0:
                raise RemixError("Sound effect starts outside timeline")
            effects[at:at+n] += signal[:n, None]*sfx.get("gain", .4)
    narration = audio.get("narration")
    if narration:
        a, b = round(narration.get("start_f", 0)*sr/fps), round(narration.get("end_f", timeline["duration_frames"])*sr/fps)
        data = mix_pcm(narration["path"], duration=(b-a)/sr)
        if len(data) < b-a-round(sr/fps):
            raise RemixError("Narration is shorter than its declared contribution interval")
        voices[a:a+min(b-a, len(data))] += data[:b-a]*narration.get("gain", 1)
        if audio.get("duck_music", True):
            music[a:b] *= .35
    mixed = music+voices+effects
    peak = float(np.max(np.abs(mixed)))
    headroom_gain = 1/max(1.0, peak)
    mixed *= headroom_gain
    if not np.any(np.abs(mixed) > 1e-6):
        raise RemixError("Timeline has no audible audio; loudness cannot be verified")
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(sr)
        out.writeframes((mixed*32767).astype("<i2").tobytes())
    return {"sample_rate": sr, "samples": length, "channels": channels,
            "channel_mix": "stereo music/source preserved; mono narration and SFX centered" if channels == 2 else "mono",
            "music_input_channels": music_channels if audio.get("path") else None,
            "source_input_channels": source_channels,
            "ducking": "declared-dialogue-window gain 0.35",
            "source_retime_audio": "constant-speed linear sample remap; pitch changes with speed",
            "pre_gain_peak": peak, "headroom_gain": headroom_gain}


def _loudness(path):
    cfg = thresholds()["qa"]
    # AAC may overshoot a PCM limiter; reserve half a dB before encoding.
    pcm_peak_target = cfg["max_true_peak"] - cfg.get("encoder_peak_margin", .5)
    result = command(["ffmpeg", "-hide_banner", "-i", path, "-vn", "-af",
        f"loudnorm=I={cfg['target_lufs']}:TP={pcm_peak_target}:LRA=11:print_format=json", "-f", "null", "-"])
    matches = re.findall(r'\{\s*"input_i".*?\}', result.stderr.decode(errors="replace"), re.S)
    if not matches:
        raise RemixError("Could not measure output loudness")
    raw = json.loads(matches[-1])
    try:
        values = {k: float(v) for k, v in raw.items() if k != "normalization_type"}
        if not all(math.isfinite(v) for v in values.values()):
            raise ValueError
        return values
    except ValueError:
        raise RemixError("Audio loudness is not finite") from None


def generate_ass(timeline, path):
    fps, width, height = timeline["fps"], timeline["width"], timeline["height"]
    def ass_time(frame):
        cs = round(frame*100/fps)
        return f"{cs//360000}:{cs//6000%60:02}:{cs//100%60:02}.{cs%100:02}"
    def escape(text):
        return str(text).replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N").replace("\r", "")
    rows = list(timeline.get("subtitles", []))
    rows += [{"start_f": overlay.get("start_f", item["out"]["start_f"]),
              "end_f": overlay.get("end_f", item["out"]["end_f"]), "text": overlay["text"],
              **{key:overlay[key] for key in ("position", "font_size", "font_family") if key in overlay}}
             for item in timeline["items"] for overlay in item.get("overlays", [])]
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,{max(16,round(height*.05))},&H00FFFFFF,&H0000FFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,30,30,{round(height*.08)},1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    for row in rows:
        if set(row) - {"start_f", "end_f", "text", "words", "position", "font_size", "font_family"}:
            raise RemixError("Unsupported subtitle fields", 5)
        if not 0 <= row["start_f"] < row["end_f"] <= timeline["duration_frames"]:
            raise RemixError("Subtitle interval is outside timeline")
        text = escape(row.get("text", ""))
        if row.get("words"):
            text = "".join("{\\k"+str(max(1, round((w["end_f"]-w["start_f"])*100/fps)))+"}"+escape(w["text"]) for w in row["words"])
        text = _caption_style(row, width, height)+text
        header += f"Dialogue: 0,{ass_time(row['start_f'])},{ass_time(row['end_f'])},Default,,0,0,0,,{text}\n"
    Path(path).write_text(header, encoding="utf-8")
    return str(path)


def _face_sources(run, timeline, by_id):
    plan_path = run / "face-plan.json"
    if not plan_path.is_file():
        if any(item.get("face_ops") for item in timeline["items"]):
            raise RemixError("Timeline face_ops need a verified face plan", 4)
        return by_id
    plan = read_json(plan_path)
    if any(i["mode"] in ("anonymize", "swap") for i in plan.get("items", [])):
        raise RemixError("Synthetic face rendering and mandatory labeling verification need a supported backend", 5)
    if any(i["mode"] == "blur" for i in plan.get("items", [])):
        from .face import verify_render
        verify_render(run, timeline)
        applied = read_json(run / "face-applied.json")
        ensure_snapshot(run, applied.get("snapshot", {}))
        replaced = {sid: dict(src) for sid, src in by_id.items()}
        for sid, value in applied.get("sources", {}).items():
            if sid not in replaced or fingerprint(value["path"]) != value["fingerprint"]:
                raise RemixError("Face processed-source cache changed or references unknown source", 3)
            replaced[sid]["path"] = value["path"]
        return replaced
    return by_id


def render(run, preview=False, allow_mock=False, confirm_unknown_license=False):
    run = Path(run)
    timeline = read_json(run / "timeline.json")
    ensure_snapshot(run, timeline.get("dependencies", {}))
    ensure_decision_hashes(timeline)
    sources = check_sources(run)
    by_id = _validate_timeline(timeline, sources)
    if timeline.get("mock") and not allow_mock:
        raise RemixError("Mock judgments cannot be delivered without --allow-mock", 4)
    used = {x["src"]["source"] for x in timeline["items"]}
    unknown = [sid for sid in used if by_id[sid]["license"] == "unknown"]
    sound = timeline.get("audio", {})
    if sound.get("path") and sound.get("license") not in ("own", "licensed"):
        unknown.append("music")
    if sound.get("narration", {}).get("path") and sound["narration"].get("license") not in ("own", "licensed"):
        unknown.append("narration")
    if not preview and unknown and not confirm_unknown_license:
        raise RemixError("Unknown source licenses require --confirm-unknown-license for formal export", 4, {"sources": unknown})
    if not preview:
        from .originality import check_catalog, originality
        check_catalog(run, timeline)
        originality(run)
    findings = _qa_timeline(timeline)
    if any(f["level"] == "error" for f in findings):
        raise RemixError("Timeline QA has errors (including flash safety); fix before rendering", 2, {"findings": findings})
    by_id = _face_sources(run, timeline, by_id)
    for executable in ("ffmpeg", "ffprobe"):
        if not shutil.which(executable):
            raise RemixError(f"Missing system dependency: {executable}", 5)
    if (run / "music.json").is_file():
        music = read_json(run / "music.json")
        if fingerprint(music["path"]) != music["fingerprint"]:
            raise RemixError("Music source changed; rerun music → slots → score → cast → assemble", 3)
    plan_id = digest(timeline)[:16]
    folder = run / "renders" / plan_id
    folder.mkdir(parents=True, exist_ok=True)
    width, height = timeline["width"], timeline["height"]
    if preview and max(width, height) > 720:
        scale = 720/max(width, height)
        width, height = max(16, round(width*scale/2)*2), max(16, round(height*scale/2)*2)
    filename = ("preview" if preview else "final") + ("-MOCK" if timeline.get("mock") else "") + ".mp4"
    output, video, audio = [safe_output(run, x) for x in (folder / filename, folder / "video.mp4", folder / "mix.wav")]
    _write_video(video, timeline, by_id, width, height)
    audio_info = _mix_audio(audio, timeline, by_id)
    measured = _loudness(audio)
    cfg = thresholds()["qa"]
    pcm_peak_target = cfg["max_true_peak"] - cfg.get("encoder_peak_margin", .5)
    normalization = (f"loudnorm=I={cfg['target_lufs']}:TP={pcm_peak_target}:LRA=11:"
        f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:measured_LRA={measured['input_lra']}:"
        f"measured_thresh={measured['input_thresh']}:offset={measured['target_offset']}:linear=true")
    args = ["ffmpeg", "-v", "error", "-y", "-i", video, "-i", audio, "-map", "0:v:0", "-map", "1:a:0"]
    if timeline.get("subtitles") or any(item.get("overlays") for item in timeline["items"]):
        scaled = {**timeline, "width": width, "height": height}
        if height != timeline["height"]:
            scaled["items"] = [{**item, "overlays":[
                {**overlay, **({"font_size":max(16,round(overlay["font_size"]*height/timeline["height"]))}
                               if "font_size" in overlay else {})}
                for overlay in item.get("overlays", [])]} for item in timeline["items"]]
        subtitle = Path(generate_ass(scaled, safe_output(run, folder / "subtitles.ass")))
        # FFmpeg filter quoting is a separate language even though argv is shell-free.
        escaped = str(subtitle).replace("\\", "\\\\").replace(":", "\\:").replace("'", "'\\''").replace(",", "\\,").replace("[", "\\[").replace("]", "\\]")
        args += ["-vf", "ass=filename='" + escaped + "'", "-c:v", "libx264", "-crf", "18", "-preset", "veryfast"]
    else:
        args += ["-c:v", "copy"]
    args += ["-af", normalization, "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
             "-t", f"{timeline['duration_frames']/timeline['fps']:.9f}", "-movflags", "+faststart", output]
    command(args)
    verification = verify_output(output, {**timeline, "width": width, "height": height})
    verification.update({"preview": preview, "mock": bool(timeline.get("mock")),
                         "unknown_license_confirmed": bool(unknown and confirm_unknown_license),
                         "unknown_license_sources": unknown,
                         "findings": findings, "audio_pipeline": audio_info,
                         "timeline_hash": digest(timeline), "source_fingerprints_checked": True})
    write_json(folder / "verification.json", verification)
    write_json(folder / "timeline.json", timeline)
    write_json(run / "render-result.json", {"path": str(output), "verification": str(folder / "verification.json"),
        "timeline_hash": digest(timeline), "preview": preview, "mock": bool(timeline.get("mock"))})
    if not verification["ok"]:
        raise RemixError("Output verification failed; inspect verification.json", 2, verification)
    check_sources(run)
    if timeline.get("audio", {}).get("path") and fingerprint(timeline["audio"]["path"]) != timeline["audio"]["fingerprint"]:
        raise RemixError("Music changed while rendering; output is stale", 3)
    narration = timeline.get("audio", {}).get("narration")
    if narration and fingerprint(narration["path"]) != narration["fingerprint"]:
        raise RemixError("Narration changed while rendering; output is stale", 3)
    return {"path": str(output), "verification": str(folder / "verification.json"), "ok": True, "mock": bool(timeline.get("mock"))}


def verify_output(path, timeline):
    info = json.loads(command(["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", path]).stdout)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    frames = int(video.get("nb_read_frames", video.get("nb_frames", 0)))
    actual_audio = len(_pcm(path, 48000))/48000 if audio else 0
    loudness = _loudness(path) if audio else None
    duration = timeline["duration_frames"]/timeline["fps"]
    cfg = thresholds()["qa"]
    checks = {"frame_count": frames == timeline["duration_frames"], "audio_present": bool(audio),
        "frame_rate": abs(float(video["r_frame_rate"].split("/")[0])/float(video["r_frame_rate"].split("/")[1])-timeline["fps"]) < 1e-6,
        "dimensions": (video["width"], video["height"]) == (timeline["width"], timeline["height"]),
        "audio_duration": abs(actual_audio-duration) <= 1/timeline["fps"],
        "loudness": bool(loudness and math.isfinite(loudness["input_i"]) and abs(loudness["input_i"]-cfg["target_lufs"]) <= cfg["loudness_tolerance"]),
        "true_peak": bool(loudness and loudness["input_tp"] <= cfg["max_true_peak"])}
    # Measure scene evidence at each planned hard cut. Similar cuts are unobservable;
    # do not claim a content detector verified a boundary between identical frames.
    np, cv2 = _deps()
    cap = cv2.VideoCapture(str(path))
    cut_evidence = []
    for item in timeline["items"][1:]:
        if item.get("transition_in", {}).get("type", "hard_cut") != "hard_cut":
            continue
        f = item["out"]["start_f"]
        differences = []
        for boundary in range(max(1, f-1), min(timeline["duration_frames"], f+2)):
            cap.set(cv2.CAP_PROP_POS_FRAMES, boundary-1)
            ok_a, a = cap.read()
            ok_b, b = cap.read()
            if ok_a and ok_b:
                differences.append((boundary, float(np.abs(a.astype(np.float32)-b).mean())))
        if differences:
            peak, score = max(differences, key=lambda x: x[1])
            cut_evidence.append({"slot": item["slot"], "planned_frame": f, "detected_frame": peak,
                "error_frames": abs(peak-f), "difference": score, "observable": score >= 5})
    cap.release()
    checks["observable_cut_alignment"] = all(x["error_frames"] <= 1 for x in cut_evidence if x["observable"])
    return {"version": 1, "ok": all(checks.values()), "checks": checks, "frames": frames,
        "planned_frames": timeline["duration_frames"], "audio_seconds": actual_audio,
        "audio_channels": int(audio.get("channels", 0)) if audio else 0,
        "planned_seconds": duration, "loudness": loudness, "cut_evidence": cut_evidence,
        "unobservable_cuts": sum(not x["observable"] for x in cut_evidence),
        "unverified": ["perceptual flash detection from decoded full sequence", "subject identity/occlusion"]}


def frames(run, slots=None, times=None):
    run = Path(run)
    timeline = read_json(run / "timeline.json")
    ensure_snapshot(run, timeline.get("dependencies", {}))
    ensure_decision_hashes(timeline)
    sources = check_sources(run)
    by_id = _face_sources(run, timeline, _validate_timeline(timeline, sources))
    selected = set(slots or [])
    if selected - {x["slot"] for x in timeline["items"]}:
        raise RemixError("Requested frames reference an unknown slot")
    folder = run / "frames" / digest(timeline)[:16]
    folder.mkdir(parents=True, exist_ok=True)
    rendered = read_json(run / "render-result.json") if (run / "render-result.json").is_file() else None
    result = []
    if times:
        if not rendered or rendered.get("timeline_hash") != digest(timeline):
            raise RemixError("Output-time frames need a current render")
        for n, seconds in enumerate(times):
            if not isinstance(seconds, (float, int)) or not math.isfinite(seconds) or not 0 <= seconds < timeline["duration_frames"]/timeline["fps"]:
                raise RemixError("Requested frame time is outside the timeline")
            output = safe_output(run, folder / f"time-{n:03d}.jpg")
            command(["ffmpeg", "-v", "error", "-y", "-ss", f"{seconds:.6f}", "-i", rendered["path"], "-frames:v", "1", output])
            result.append(str(output))
    else:
        for n, item in enumerate(timeline["items"]):
            if selected and item["slot"] not in selected:
                continue
            output = safe_output(run, folder / f"slot-{n:03d}.jpg")
            # Slot IDs are data; filenames deliberately never incorporate them.
            if rendered and rendered.get("timeline_hash") == digest(timeline):
                path, seconds = rendered["path"], item["out"]["start_f"]/timeline["fps"]
            else:
                path, seconds = by_id[item["src"]["source"]]["path"], item["src"]["in_us"]/1e6
            command(["ffmpeg", "-v", "error", "-y", "-ss", f"{seconds:.6f}", "-i", path, "-frames:v", "1", output])
            result.append(str(output))
    return {"frames": result, "timeline_hash": digest(timeline)}
