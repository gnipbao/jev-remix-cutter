#!/usr/bin/env python3
"""Generate only synthetic, owned 120 BPM media and ground-truth timestamps.

Usage: python examples/make_demo.py --out /tmp/remix-demo --duration-s 16
No downloaded media, credentials or model calls are needed.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import wave


def make_demo(output, duration_s=16, fps=30, width=320, height=180):
    import numpy as np
    import cv2
    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required")
    if duration_s < 2 or duration_s % 2 or fps < 1:
        raise ValueError("Demo duration must be a positive even integer >= 2")
    samples, sr = duration_s*24000, 24000
    t = np.arange(samples)/sr
    # A quiet bed keeps the delivered loudness achievable without clipping peaks.
    music = .08*np.sin(2*np.pi*220*t)
    beats = list(range(0, duration_s*1_000_000, 500_000))
    for time_us in beats:
        start = round(time_us*sr/1_000_000)
        pulse_t = np.arange(round(.07*sr))/sr
        pulse = .35*np.sin(2*np.pi*1200*pulse_t)*np.exp(-pulse_t*45)
        n = min(len(pulse), samples-start)
        music[start:start+n] += pulse[:n]
    audio = out/"music-120bpm.wav"
    with wave.open(str(audio), "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(sr)
        wav.writeframes((music*32767).astype("<i2").tobytes())
    colors = [(30, 30, 180), (30, 150, 30), (180, 30, 30), (30, 150, 180)]
    source = out/"source.mp4"
    # Raw frames are generated deterministically. The square jumps at local 1 s.
    with tempfile.TemporaryDirectory(prefix="demo-", dir=out) as temp:
        silent = Path(temp)/"silent.mp4"
        log = tempfile.TemporaryFile()
        encoder = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}", "-r", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p", str(silent)], stdin=subprocess.PIPE, stderr=log)
        try:
            block = 56
            for f in range(duration_s*fps):
                scene, local = divmod(f, 2*fps)
                frame = np.full((height, width, 3), colors[scene % len(colors)], dtype=np.uint8)
                x = 32 + min(local, fps)//4 + (12 if local >= fps else 0)
                # Checkerboard gives the optical flow a locally trackable subject.
                for yy in range(0, block, 7):
                    for xx in range(0, block, 7):
                        level = 230 if (xx//7+yy//7) % 2 == 0 else 45
                        frame[55+yy:55+yy+7, x+xx:x+xx+7] = level
                cv2.putText(frame, f"S{scene+1} F{local:03}", (140, 35), cv2.FONT_HERSHEY_SIMPLEX, .45, (235,235,235), 1)
                encoder.stdin.write(frame.tobytes())
            encoder.stdin.close(); encoder.wait()
            if encoder.returncode:
                log.seek(0)
                raise RuntimeError(log.read().decode(errors="replace"))
        finally:
            if encoder.poll() is None:
                encoder.terminate(); encoder.wait()
            log.close()
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(silent), "-i", str(audio),
            "-c:v", "copy", "-c:a", "aac", "-shortest", str(source)], check=True, capture_output=True)
    result = {"source": str(source), "music": str(audio), "license": "own", "self_shot": True,
        "duration_s": duration_s, "fps": fps, "width": width, "height": height, "bpm": 120,
        "beat_times_us": beats, "beat_frames": [round(u*fps/1_000_000) for u in beats],
        "scene_boundaries_f": list(range(2*fps, duration_s*fps, 2*fps)),
        "action_peak_frames": list(range(fps, duration_s*fps, 2*fps)),
        "notice": "Ground-truth motion event is a square jump; optical-flow peak is an approximate measured feature."}
    (out/"ground-truth.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--duration-s", type=int, default=16)
    args = parser.parse_args()
    print(json.dumps(make_demo(args.out, args.duration_s), indent=2))
