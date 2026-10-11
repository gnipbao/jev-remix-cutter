"""Local CPU media sensing and conservative onset-grid analysis.

Source times are presentation timestamps in microseconds. Proxies are CFR solely
for local measurements; original frame rates are never used as a timebase.
"""
from __future__ import annotations
import json
import math
import os
from pathlib import Path

from .common import (RemixError, command, digest, fingerprint, read_json, write_json,
                     write_jsonl, thresholds, snapshot, save_artifact)


def safe_output(run, path):
    """Keep writable media artifacts inside the run and never follow a symlink."""
    run, path = Path(run).resolve(), Path(path)
    if path.is_symlink() or not path.resolve().is_relative_to(run):
        raise RemixError("Media artifact must remain inside its run and not be a symlink")
    return path


def _deps():
    try:
        import numpy as np
        import cv2
    except ImportError:
        raise RemixError("Missing numpy/OpenCV; install the core dependencies", 5) from None
    return np, cv2


def probe(path, video=True):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise RemixError(f"Media does not exist: {path.name}")
    obj = json.loads(command(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path]).stdout)
    streams = obj.get("streams", [])
    v = next((s for s in streams if s["codec_type"] == "video"), None)
    a = next((s for s in streams if s["codec_type"] == "audio"), None)
    if video and not v:
        raise RemixError("Input must contain a video stream")
    if not video and not a:
        raise RemixError("Input must contain an audio stream")
    dur = float(obj.get("format", {}).get("duration", (v or a or {}).get("duration", 0)))
    if not math.isfinite(dur) or dur <= 0:
        raise RemixError("Media has no finite positive duration")
    def rate(value):
        try:
            n, d = value.split("/")
            return float(n) / float(d)
        except (ValueError, ZeroDivisionError, AttributeError):
            return None
    return {"duration_us": round(dur * 1_000_000), "width": v.get("width") if v else None,
            "height": v.get("height") if v else None,
            "fps": rate(v.get("avg_frame_rate")) if v else None,
            "source_frame_rate": v.get("avg_frame_rate") if v else None,
            "nominal_frame_rate": v.get("r_frame_rate") if v else None,
            "vfr_possible": bool(v and rate(v.get("avg_frame_rate")) != rate(v.get("r_frame_rate"))),
            "audio": bool(a), "sample_rate": int(a.get("sample_rate", 0)) if a else None,
            "audio_channels": int(a["channels"]) if a and a.get("channels") else None,
            "codec": v.get("codec_name") if v else a.get("codec_name"),
            "color_transfer": v.get("color_transfer", "unknown") if v else None,
            "color_space": v.get("color_space", "unknown") if v else None,
            "hdr": bool(v and v.get("color_transfer") in ("smpte2084", "arib-std-b67")),
            "stream_start_time": float((v or a).get("start_time", 0)),
            "nb_frames": int(v["nb_frames"]) if v and v.get("nb_frames", "").isdigit() else None}


def _highlight_sources(plan_path):
    plan_path = Path(plan_path).expanduser().resolve()
    plan = read_json(plan_path)
    index = read_json(plan_path.parent / "index.json") if (plan_path.parent / "index.json").is_file() else {}
    if plan.get("index_hash") and (not index or plan["index_hash"] != digest(index)):
        raise RemixError("Highlight plan is stale against its index; rebuild the highlight plan", 3)
    candidates_path = plan_path.parent / "candidates.json"
    if plan.get("candidates_hash") and candidates_path.is_file():
        candidates = read_json(candidates_path)
        if plan["candidates_hash"] != candidates.get("hash"):
            raise RemixError("Highlight candidate evidence changed; rebuild the highlight plan", 3)
    else:
        candidates = {}
    path = plan.get("source") or index.get("media", {}).get("path")
    if not path:
        raise RemixError("Highlight plan needs source or sibling index.json with media.path")
    old = plan.get("source_fingerprint") or index.get("media", {}).get("fingerprint")
    current = fingerprint(path)
    if old:
        expected = dict(old)
        if "sample_sha256" in expected:
            expected["edge_sha256"] = expected.pop("sample_sha256")
        if current != expected:
            raise RemixError("Highlight source changed; rebuild the highlight plan", 3)
    else:
        raise RemixError("Highlight import requires a recorded source fingerprint", 3)
    clips = plan.get("clips", [])
    if not clips:
        raise RemixError("Highlight plan has no selected clips")
    evidence = {c.get("id"):c for c in candidates.get("items",[])}
    return [{"path": path, "range": {"start_us": c.get("start_us", c.get("source_in_us")),
              "end_us": c.get("end_us", c.get("source_out_us"))},
             "highlight": {"plan": str(plan_path), "candidate": c.get("id"),
                 "transcript": c.get("transcript", evidence.get(c.get("id"),{}).get("transcript","")), "visual": c.get("visual", evidence.get(c.get("id"),{}).get("visual")),
                 "scores": c.get("dimensions", c.get("score"))}}
            for c in clips]


def ingest(run, paths=(), license="unknown", from_highlight=None, self_shot=False):
    np, cv2 = _deps()
    try:
        from scenedetect import FrameTimecode
        from scenedetect.detectors import ContentDetector
    except ImportError:
        raise RemixError("Missing scenedetect; install the core dependencies", 5) from None
    run = Path(run)
    if license not in ("own", "licensed", "unknown"):
        raise RemixError("License must be own, licensed or unknown")
    incoming = [{"path": str(Path(x).expanduser().resolve())} for x in paths]
    if from_highlight:
        incoming += _highlight_sources(from_highlight)
    cfg = thresholds()["sense"]
    if not incoming or len(incoming) > cfg["max_sources"]:
        raise RemixError(f"Provide 1–{cfg['max_sources']} sources")
    brief = read_json(run / "brief.json")
    fps = int(brief.get("fps", 30))
    total = 0
    source_rows, shots = [], []
    for folder in ("proxies", "features", "contact"):
        (run / folder).mkdir(parents=True, exist_ok=True)
    for i, spec in enumerate(incoming, 1):
        src_id, path = f"src{i:02d}", Path(spec["path"])
        media = probe(path)
        interval = spec.get("range", {"start_us": 0, "end_us": media["duration_us"]})
        start, end = interval["start_us"], interval["end_us"]
        if (not isinstance(start, int) or not isinstance(end, int) or
                not 0 <= start < end <= media["duration_us"]):
            raise RemixError("Source range must be integer microseconds inside the source")
        total += (end - start) / 1e6
        if total > cfg["max_source_seconds"]:
            raise RemixError("Source pool exceeds configured duration budget")
        before = fingerprint(path)
        proxy = safe_output(run, run / "proxies" / (src_id + ".mp4"))
        # fps uses decoded PTS, including VFR input. Proxy origin is range.start_us.
        height = min(cfg["proxy_height"], media["height"])
        height -= height % 2
        command(["ffmpeg", "-v", "error", "-y", "-ss", f"{start/1e6:.6f}", "-i", path,
                 "-t", f"{(end-start)/1e6:.6f}", "-an", "-vf", f"scale=-2:{height},fps={fps},setsar=1",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", proxy])
        source = {"id": src_id, "path": str(path), "fingerprint": before, "license": license,
                  "self_shot": bool(self_shot), "media": media, "range": interval,
                  "proxy": str(proxy.relative_to(run)), "proxy_fps": fps, **{k: v for k, v in spec.items() if k == "highlight"}}
        source_rows.append(source)
        cap = cv2.VideoCapture(str(proxy))
        records, prev_gray, prev_hsv = [], None, None
        detector = ContentDetector(threshold=cfg["scene_threshold"], min_scene_len=cfg["min_shot_frames"])
        detected_cuts = []
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            detected_cuts.extend(c.frame_num for c in detector.process_frame(FrameTimecode(len(records), float(fps)), frame))
            small = cv2.resize(frame, (160, max(2, round(frame.shape[0] * 160 / frame.shape[1]))))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV).astype(np.float32)
            if prev_gray is None:
                motion, vector, difference = 0.0, [0.0, 0.0], 0.0
            else:
                flow = cv2.calcOpticalFlowFarneback(prev_gray, gray, None, .5, 2, 13, 2, 5, 1.1, 0)
                motion = float(np.linalg.norm(flow, axis=2).mean())
                vector = flow.mean(axis=(0, 1)).tolist()
                # Hue wrap avoids false cuts at 0/179. Match content-detector scale.
                dh = np.abs(hsv[:, :, 0] - prev_hsv[:, :, 0])
                dh = np.minimum(dh, 180 - dh)
                difference = float((dh.mean() + np.abs(hsv[:, :, 1:] - prev_hsv[:, :, 1:]).mean(axis=(0, 1)).sum()) / 3)
            lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB).astype(np.float32)
            records.append({"motion": motion, "vector": vector, "difference": difference,
                            "brightness": float(gray.mean()), "saturation": float(hsv[:, :, 1].mean()),
                            "sharpness": float(cv2.Laplacian(gray, cv2.CV_32F).var()),
                            "lab": lab.mean(axis=(0, 1)).tolist(), "lab_std": lab.std(axis=(0, 1)).tolist(),
                            "rgb": cv2.cvtColor(small, cv2.COLOR_BGR2RGB).mean(axis=(0, 1)).tolist()})
            prev_gray, prev_hsv = gray, hsv
        cap.release()
        if not records:
            raise RemixError(f"No proxy frames decoded for {src_id}")
        detected_cuts.extend(c.frame_num for c in detector.post_process(FrameTimecode(len(records)-1, float(fps))))
        boundaries = [0] + sorted({f for f in detected_cuts if 0 < f < len(records)})
        # Merge a trailing fragment into its previous shot; preserve complete coverage.
        if len(boundaries) > 1 and len(records) - boundaries[-1] < cfg["min_shot_frames"]:
            boundaries.pop()
        boundaries.append(len(records))
        np.savez_compressed(run / "features" / (src_id + ".npz"),
            time_us=np.array([start + round(f * 1e6 / fps) for f in range(len(records))], dtype=np.int64),
            motion=np.array([r["motion"] for r in records]),
            brightness=np.array([r["brightness"] for r in records]),
            color_difference=np.array([r["difference"] for r in records]),
            lab=np.array([r["lab"] for r in records]))
        cap = cv2.VideoCapture(str(proxy))
        for j, (a, b) in enumerate(zip(boundaries, boundaries[1:]), 1):
            values = records[a:b]
            shot_id = f"{src_id}#s{j:04d}"
            # Scene-cut flow is unrelated to within-shot action; exclude first sample.
            peak = a + (int(np.argmax([r["motion"] for r in values[1:]])) + 1 if len(values) > 1 else 0)
            contact = Path("contact") / f"{src_id}-s{j:04d}.jpg"
            samples = [a, (a+b-1)//2, b-1, peak]
            cells = []
            for label, frame_id in zip(("first", "middle", "last", "peak"), samples):
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_id)
                ok, frame = cap.read()
                if not ok:
                    raise RemixError("Could not decode contact sheet frame")
                image = cv2.resize(frame, (240, 135))
                canvas = np.full((165, 240, 3), 24, dtype=np.uint8)
                canvas[:135] = image
                cv2.putText(canvas, f"{shot_id} {label}", (4, 153), cv2.FONT_HERSHEY_SIMPLEX, .38, (240, 240, 240), 1)
                cells.append(canvas)
            cv2.imwrite(str(safe_output(run, run / contact)), np.concatenate(cells, axis=1))
            mean = lambda name: float(np.mean([r[name] for r in values]))
            features = {"motion": float(np.mean([r["motion"] for r in values[1:] or values[:1]])), "brightness": mean("brightness"),
                "saturation": mean("saturation"), "sharpness": mean("sharpness"),
                "lab_mean": np.mean([r["lab"] for r in values], axis=0).tolist(),
                "lab_std": np.mean([r["lab_std"] for r in values], axis=0).tolist(),
                "mean_rgb": np.mean([r["rgb"] for r in values], axis=0).round().astype(int).tolist(),
                "dominant_colors": [],
                "motion_start": np.mean([r["vector"] for r in values[1:9] or values[:1]], axis=0).tolist(),
                "motion_end": np.mean([r["vector"] for r in values[-8:]], axis=0).tolist(),
                "peak_us": start + round(peak * 1e6 / fps), "action_peak_us": start + round(peak * 1e6 / fps),
                "action_peak_f": peak - a, "scene_score": values[0]["difference"],
                "feature_method": "opencv-farneback+scenedetect-content", "subject_centroid": None,
                "ocr": {"available": False}, "hdr": media["hdr"]}
            shots.append({"shot_id": shot_id, "source": src_id,
                "start_us": start + round(a * 1e6 / fps),
                "end_us": min(end, start + round(b * 1e6 / fps)),
                "features": features, "contact": str(contact),
                "feature_file": f"features/{src_id}.npz"})
        cap.release()
        if fingerprint(path) != before:
            raise RemixError("Source changed during ingest", 3)
    result = {"version": 1, "items": source_rows, "total_seconds": total,
              "algorithm": "scenedetect-content+opencv-cpu-v1",
              "unavailable": ["cropdetect black-border estimate", "subject saliency", "OCR", "kmeans dominant colors", "quality feature backends"],
              "dependencies": snapshot(run, ["brief.json"])}
    save_artifact(run, "sources.json", result)
    write_jsonl(run / "shots.jsonl", shots)
    return {"sources": len(source_rows), "shots": len(shots), "path": str(run / "sources.json")}


def decode_audio(path, sample_rate=24000):
    np, _ = _deps()
    raw = command(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(sample_rate),
                   "-f", "f32le", "pipe:1"]).stdout
    return np.frombuffer(raw, dtype="<f4").copy()


def onset_grid(samples, sample_rate, fps=30):
    """Return observed onsets; never invent interpolated beats for missing pulses.

    This energy-onset fallback measures rhythmic regularity, not musical meter.
    Grouped downbeats are explicitly marked as an assumption.
    """
    np, _ = _deps()
    if not isinstance(sample_rate, int) or sample_rate < 1000 or not isinstance(fps, int) or not 1 <= fps <= 120:
        raise RemixError("Onset analysis needs a positive sample rate and integer output fps")
    samples = np.asarray(samples, dtype=np.float32)
    if samples.ndim != 1 or not np.all(np.isfinite(samples)):
        raise RemixError("Onset samples must be finite mono audio")
    hop = max(1, round(sample_rate / 200))
    if not len(samples):
        return [], 0.0, 0.0, np.array([], dtype=np.float32), hop
    padded = np.pad(samples, (0, (-len(samples)) % hop))
    energy = np.sqrt(np.mean(padded.reshape(-1, hop) ** 2, axis=1))
    flux = np.maximum(0, np.diff(energy, prepend=0))
    if not len(flux) or float(flux.max()) < 1e-5:
        return [], 0.0, 0.0, energy, hop
    candidates = []
    floor = max(float(flux.max()) * .12, float(np.percentile(flux, 75)) * 2)
    gap = max(1, round(.12 * sample_rate / hop))
    for i in range(len(flux)):
        if flux[i] < floor or (i and flux[i] < flux[i-1]) or (i+1 < len(flux) and flux[i] < flux[i+1]):
            continue
        if candidates and i - candidates[-1] < gap:
            if flux[i] > flux[candidates[-1]]:
                candidates[-1] = i
        else:
            candidates.append(i)
    seconds = np.array(candidates) * hop / sample_rate
    intervals = np.diff(seconds)
    usable = intervals[(intervals >= .24) & (intervals <= 1.5)]
    if len(usable) >= 3:
        period = float(np.median(usable))
        jitter = float(np.median(np.abs(usable - period)) / period)
        confidence = max(0.0, min(1.0, (1 - jitter * 6) * len(usable) / max(1, len(intervals))))
        bpm = 60 / period
    else:
        confidence, bpm = 0.0, 0.0
    beats = [{"time_us": round(t * 1e6), "frame": round(t * fps),
              "strength": float(flux[candidates[i]] / flux.max()), "downbeat": i % 4 == 0,
              "downbeat_assumed": True} for i, t in enumerate(seconds)]
    return beats, confidence, bpm, energy, hop


def _musical_onsets(samples, sample_rate, fps):
    """Spectral-flux onsets with an accurately measured periodic-pulse fallback.

    Both grids contain detected attacks only. A beat tracker cannot certify
    musical meter, semantic sections, or invent missing attacks in this core tier.
    """
    try:
        import librosa
    except ImportError:
        raise RemixError("Missing librosa; install the core dependencies", 5) from None
    np, _ = _deps()
    pulses, pulse_confidence, pulse_bpm, energy, energy_hop = onset_grid(samples, sample_rate, fps)
    if not len(samples) or float(np.max(np.abs(samples))) < 1e-5:
        return pulses, pulse_confidence, pulse_bpm, energy, energy_hop, "librosa-spectral-onsets; silent"
    hop = max(1, round(sample_rate / 200))
    envelope = librosa.onset.onset_strength(y=samples, sr=sample_rate, hop_length=hop, n_fft=1024)
    attacks = librosa.onset.onset_detect(onset_envelope=envelope, sr=sample_rate, hop_length=hop,
                                        backtrack=False, units="frames")
    if pulse_confidence >= .8 and len(pulses) >= 4:
        return pulses, pulse_confidence, pulse_bpm, energy, energy_hop, "librosa-spectral-onsets+periodic-energy-pulse-grid"
    seconds = librosa.frames_to_time(attacks, sr=sample_rate, hop_length=hop)
    intervals = np.diff(seconds)
    usable = intervals[(intervals >= .24) & (intervals <= 1.5)]
    bpm, confidence = 0.0, 0.0
    if len(usable) >= 3:
        period = float(np.median(usable))
        jitter = float(np.median(np.abs(usable-period))/period)
        bpm, confidence = 60/period, max(0.0, min(1.0, (1-jitter*6)*len(usable)/max(1, len(intervals))))
    peak = float(envelope.max()) or 1.0
    beats = [{"time_us": round(t*1e6), "frame": round(t*fps), "strength": float(envelope[f]/peak),
              "downbeat": i % 4 == 0, "downbeat_assumed": True} for i, (f, t) in enumerate(zip(attacks, seconds))]
    return beats, confidence, bpm, energy, energy_hop, "librosa-spectral-onset-200hz-cpu-v1"


def music(run, audio, fps=None, **options):
    if options.get("stems") or options.get("lyrics") or options.get("provider", "core") not in (None, "core"):
        raise RemixError("Stems/lyrics/quality analysis needs an optional backend; core does not fabricate results", 5)
    run, audio = Path(run), Path(audio).expanduser().resolve()
    media = probe(audio, video=False)
    if media["duration_us"] > 30 * 60 * 1e6:
        raise RemixError("Core music analysis is limited to 30 minutes")
    fps = int(fps or read_json(run / "brief.json").get("fps", 30))
    before = fingerprint(audio)
    sr = 24000
    data = decode_audio(audio, sr)
    if "NUMBA_CACHE_DIR" not in os.environ:
        # Configure this before librosa lazily imports Numba: installed packages
        # may be read-only in the agent sandbox, while the run is writable.
        cache_dir = safe_output(run, run / "cache" / "numba")
        cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ["NUMBA_CACHE_DIR"] = str(cache_dir.resolve())
    beats, confidence, bpm, energy, hop, algorithm = _musical_onsets(data, sr, fps)
    np, _ = _deps()
    (run / "features").mkdir(exist_ok=True)
    np.savez_compressed(run / "features" / "music.npz", energy=energy,
                        time_us=np.arange(len(energy), dtype=np.int64) * round(hop * 1e6 / sr))
    dur = round(len(data) * 1e6 / sr)
    if before != fingerprint(audio):
        raise RemixError("Music changed during analysis", 3)
    silence = []
    for i, e in enumerate(energy):
        if float(e) < 10 ** (thresholds()["music"]["silence_db"] / 20):
            if silence and silence[-1]["end_us"] == round(i * hop * 1e6 / sr):
                silence[-1]["end_us"] = round((i+1) * hop * 1e6 / sr)
            else:
                silence.append({"start_us": round(i * hop * 1e6 / sr), "end_us": round((i+1) * hop * 1e6 / sr)})
    result = {"version": 1, "path": str(audio), "fingerprint": before, "fps": fps,
        "duration_us": dur, "duration_frames": round(dur * fps / 1e6),
        "beats": beats, "bpm": bpm, "confidence": confidence,
        "low_confidence": confidence < thresholds()["music"]["confidence_min"],
        "algorithm": algorithm, "downbeat_confidence": 0.0,
        "sections": [{"name": "full", "start_f": 0, "end_f": round(dur * fps / 1e6),
                      "confidence": 0.0, "method": "whole-track; no semantic section detector"}],
        "silence": silence, "feature_file": "features/music.npz",
        "warnings": ["Observed onsets are used as a grid; musical meter and section labels are unverified"],
        "dependencies": snapshot(run, ["brief.json"])}
    save_artifact(run, "music.json", result)
    return result


def music_edit(run, target_s, keep=None):
    """Conservative prefix edit on complete four-bar phrases, without speed changes."""
    run = Path(run)
    old = read_json(run / "music.json")
    if fingerprint(old["path"]) != old["fingerprint"]:
        raise RemixError("Music changed; rerun music", 3)
    if isinstance(target_s, bool) or not isinstance(target_s, (float, int)) or not math.isfinite(target_s) or target_s <= 0:
        raise RemixError("target-s must be finite and positive")
    if old["low_confidence"] or old["bpm"] <= 0:
        raise RemixError("Cannot edit on phrase boundaries with a low-confidence beat grid")
    if keep and keep not in ("full",):
        raise RemixError("Core analyzer has no verified chorus/verse labels; import a verified section map first")
    phrase = 16 * 60 / old["bpm"]
    max_len = old["duration_us"] / 1e6
    lengths = [phrase * i for i in range(1, int(max_len / phrase + 1e-6) + 1)]
    fps = old["fps"]
    closest = sorted(lengths, key=lambda n: abs(n-target_s))[:2]
    if not closest or abs(closest[0] - target_s) > .5 / fps:
        raise RemixError("Target is unreachable without cutting a phrase or changing speed", 2,
                         {"target_s": target_s, "nearest_lengths_s": sorted(closest)})
    target = closest[0]
    from .common import digest
    folder = run / "audio-edits"
    folder.mkdir(exist_ok=True)
    output = safe_output(run, folder / (digest({"source": old["fingerprint"], "target_s": target})[:16] + ".wav"))
    command(["ffmpeg", "-v", "error", "-y", "-i", old["path"], "-t", f"{target:.6f}",
             "-af", "afade=t=out:st=" + f"{max(0,target-.035):.6f}" + ":d=0.035", "-c:a", "pcm_s16le", output])
    updated = music(run, output, fps)
    updated["edit"] = {"source": old["path"], "source_fingerprint": old["fingerprint"],
        "target_s": target_s, "actual_s": target, "speed": 1.0,
        "phrase_bars": 4, "phase_assumed": True, "method": "prefix-on-inferred-four-bar-boundary"}
    save_artifact(run, "music.json", updated)
    return updated
