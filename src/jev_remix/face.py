"""Consent registry and a local, deliberately limited blur pipeline.

Haar detections are tracks, not identities. Age, NSFW, identity matching and face
synthesis require licensed specialized models; this module never invents them.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol
import fcntl
import math
import os
import shutil
import sys
import tempfile
import uuid

from .common import (ROOT, RemixError, check_sources, command, digest, ensure_snapshot,
                     file_hash, fingerprint, now, read_json, read_jsonl, save_artifact,
                     snapshot, thresholds, write_json, write_jsonl)
from .originality import _fps, union


@dataclass(frozen=True)
class LicenseInfo:
    spdx: str
    commercial_allowed: bool | None
    url: str
    weights_url: str | None = None


class FaceSwapBackend(Protocol):
    name: str
    version: str
    license: LicenseInfo
    uploads_images: bool

    def prepare(self, target_refs: list[Path]) -> Any: ...
    def swap(self, frames: Any, tracks: list[dict], state: Any) -> Any: ...


BLUR_LICENSE = LicenseInfo("Apache-2.0 (OpenCV library); Intel/BSD cascade notice", True,
    "https://opencv.org/license/",
    "https://github.com/opencv/opencv/blob/4.x/data/haarcascades/haarcascade_frontalface_default.xml")


def _registry_dir(config_dir=None):
    path = Path(config_dir).expanduser() if config_dir else Path.home() / ".config" / "jev-remix" / "consents"
    if path.is_symlink():
        raise RemixError("Consent directory must not be a symlink", 4)
    path = path.resolve()
    if path == ROOT or ROOT in path.parents:
        raise RemixError("Consent registry must be outside the Skill", 4)
    return path


def _registry(config_dir=None):
    root = _registry_dir(config_dir)
    path = root / "registry.json"
    if path.is_symlink():
        raise RemixError("Consent registry must not be a symlink", 4)
    obj = read_json(path) if path.is_file() else {"version": 1, "items": []}
    if obj.get("version") != 1 or not isinstance(obj.get("items"), list):
        raise RemixError("Invalid consent registry", 4)
    return root, obj


@contextmanager
def _registry_lock(config_dir=None):
    root = _registry_dir(config_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = root / ".registry.lock"
    if lock.is_symlink():
        raise RemixError("Consent lock must not be a symlink", 4)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield root
    finally:
        os.close(fd)


def _relative(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise RemixError("Invalid registry document path", 4)
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise RemixError("Registry documents must remain within the registry", 4)
    return path


def _scope(scope):
    if not isinstance(scope, dict):
        raise RemixError("Scope must be an object", 4)
    for key in ("modes", "purposes", "platforms", "projects"):
        if not isinstance(scope.get(key), list) or not scope[key] or not all(isinstance(x, str) and x.strip() for x in scope[key]):
            raise RemixError(f"Scope requires a nonempty {key} list", 4)
    if any(x != "swap" for x in scope["modes"]):
        raise RemixError("Consent modes currently support swap only", 4)
    try:
        expires = date.fromisoformat(scope["expires"])
    except (ValueError, TypeError, KeyError):
        raise RemixError("Scope requires expires as YYYY-MM-DD", 4) from None
    if expires < date.today():
        raise RemixError("Cannot register an expired consent", 4)
    return scope


def consent_add(name, doc, refs, roles, scope, relation="other", config_dir=None):
    # Must remain first: neither an agent nor CI can pre-fill or create a record.
    if not sys.stdin.isatty():
        raise RemixError("consent add requires the user to run it in an interactive terminal; agents cannot register consent", 4)
    if not isinstance(name, str) or not name.strip() or "\n" in name:
        raise RemixError("Consent name must be nonempty", 4)
    roles = roles.split(",") if isinstance(roles, str) else list(roles)
    if not roles or len(set(roles)) != len(roles) or any(x not in ("subject", "target") for x in roles):
        raise RemixError("Consent roles use subject,target", 4)
    if relation not in ("self", "employee", "model", "other"):
        raise RemixError("Invalid consent relation", 4)
    scope_obj = read_json(scope) if isinstance(scope, (str, Path)) else scope
    _scope(scope_obj)
    doc_path = Path(doc).expanduser().resolve()
    if doc_path.suffix.lower() not in (".pdf", ".png", ".jpg", ".jpeg", ".webp") or not doc_path.is_file():
        raise RemixError("Consent document must be a PDF or image", 4)
    references = [Path(p).expanduser().resolve() for p in refs]
    if not references or any(not p.is_file() or p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp") for p in references):
        raise RemixError("Provide at least one reference image", 4)
    print(f"登记：{name}；关系：{relation}；角色：{','.join(roles)}；有效期：{scope_obj['expires']}")
    print("范围：" + str(scope_obj))
    print("引擎记录授权书哈希，不鉴定授权书真实性。")
    phrase = f"我确认已取得 {name} 的书面单独同意"
    if input(f"请逐字输入：{phrase}\n") != phrase:
        raise RemixError("Consent confirmation did not match; nothing was registered", 4)
    with _registry_lock(config_dir) as root:
        _, registry = _registry(config_dir)
        identity = "id_" + uuid.uuid4().hex[:12]
        doc_hash = file_hash(doc_path)
        destination = root / "docs" / (doc_hash + doc_path.suffix.lower())
        destination.parent.mkdir(mode=0o700, exist_ok=True)
        if destination.is_symlink():
            raise RemixError("Registry document must not be a symlink", 4)
        if not destination.exists():
            destination.write_bytes(doc_path.read_bytes())
            destination.chmod(0o600)
        if file_hash(destination) != doc_hash:
            raise RemixError("Existing registry document hash mismatch", 4)
        ref_dir = root / "refs" / identity
        ref_dir.mkdir(parents=True, mode=0o700)
        ref_names = []
        for number, ref in enumerate(references, 1):
            target = ref_dir / (f"{number:02d}" + ref.suffix.lower())
            target.write_bytes(ref.read_bytes())
            target.chmod(0o600)
            ref_names.append(str(target.relative_to(root)))
        item = {"identity_id": identity, "display_name": name, "relation": relation, "roles": roles,
                "doc": {"file": str(destination.relative_to(root)), "sha256": doc_hash, "signer": name},
                "scope": scope_obj, "refs": ref_names, "embedding_model": None,
                "created_at": now(), "created_via": "interactive", "revoked": False}
        registry["items"].append(item)
        write_json(root / "registry.json", registry)
    return {"identity_id": identity, "display_name": name, "created_via": "interactive"}


def consent_list(config_dir=None):
    _, registry = _registry(config_dir)
    return [{key: row.get(key) for key in ("identity_id", "display_name", "relation", "roles", "scope", "created_at", "revoked")}
            for row in registry["items"]]


def consent_revoke(identity_id, config_dir=None):
    with _registry_lock(config_dir) as root:
        _, registry = _registry(config_dir)
        for row in registry["items"]:
            if row.get("identity_id") == identity_id:
                row["revoked"] = True
                row["revoked_at"] = now()
                write_json(root / "registry.json", registry)
                return {"identity_id": identity_id, "revoked": True}
    raise RemixError("Consent identity not found", 4)


def _check_identity(identity_id, role, plan, registry_root, registry, project):
    error = f"{role}_not_consented"
    row = next((x for x in registry["items"] if x.get("identity_id") == identity_id), None)
    if not row or row.get("revoked") or role not in row.get("roles", []):
        raise RemixError(error, 4, {"identity_id": identity_id, "allowed_modes": ["keep", "blur"]})
    scope = row.get("scope", {})
    try:
        expired = date.today() > date.fromisoformat(scope["expires"])
    except (TypeError, ValueError, KeyError):
        expired = True
    if expired or any(plan.get(key) not in scope.get(scope_key, []) and "*" not in scope.get(scope_key, [])
        for key, scope_key in (("purpose", "purposes"), ("platform", "platforms"))):
        raise RemixError(error, 4, {"identity_id": identity_id, "reason": "expired_or_scope"})
    if "swap" not in scope.get("modes", []) or (project not in scope.get("projects", []) and "*" not in scope.get("projects", [])):
        raise RemixError(error, 4, {"identity_id": identity_id, "reason": "scope"})
    doc = row.get("doc", {})
    path = _relative(registry_root, doc.get("file", ""))
    if not path.is_file() or file_hash(path) != doc.get("sha256"):
        raise RemixError(error, 4, {"identity_id": identity_id, "reason": "document_missing_or_changed"})
    return row


def consent_verify(run, config_dir=None):
    run = Path(run)
    plan = read_json(run / "face-plan.json")
    root, registry = _registry(config_dir)
    brief = read_json(run / "brief.json")
    project = brief.get("project", brief.get("run_id", run.name))
    verified = []
    for item in plan.get("items", []):
        if item.get("mode") == "swap":
            for role in ("subject", "target"):
                row = _check_identity(item.get(role), role, plan, root, registry, project)
                verified.append({"identity_id": row["identity_id"], "role": role, "doc_sha256": row["doc"]["sha256"]})
    return {"ok": True, "verified": verified}


def _cv2():
    try:
        import cv2
    except ImportError:
        raise RemixError("Install the core OpenCV 4 dependency (opencv-python>=4.12,<5) for local face scan/blur", 5) from None
    return cv2


def _source_fps(source, cap=None):
    fps = source.get("media", {}).get("fps")
    if fps:
        return _fps(fps)
    return _fps(cap.get(_cv2().CAP_PROP_FPS))


def _frame_range(item, fps):
    first = Fraction(item["src"]["in_us"], 1_000_000) * fps
    last = Fraction(item["src"]["out_us"], 1_000_000) * fps
    return (first.numerator // first.denominator, (last.numerator + last.denominator - 1) // last.denominator)


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    intersection = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(0, min(ay + ah, by + bh) - max(ay, by))
    return intersection / max(1, aw * ah + bw * bh - intersection)


def faces_scan(run):
    run = Path(run)
    sources = {x["id"]: x for x in check_sources(run)}
    timeline = read_json(run / "timeline.json")
    cv2 = _cv2()
    if not hasattr(cv2, "CascadeClassifier"):
        raise RemixError("Haar face scan requires OpenCV 4 (opencv-python>=4.12,<5); this OpenCV build has no CascadeClassifier", 5)
    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(str(cascade_path))
    if detector.empty():
        raise RemixError("OpenCV Haar cascade is unavailable", 5)
    clusters, ranges_by_source = [], {}
    gap_max = thresholds()["face"]["tracking_gap_frames"]
    contact = run / "faces"
    contact.mkdir(exist_ok=True)
    for source_id in sorted({x["src"]["source"] for x in timeline["items"]}):
        source = sources[source_id]
        if source.get("media", {}).get("vfr_possible"):
            raise RemixError("Core face tracking needs verified CFR timestamps; VFR face processing is unavailable", 5)
        cap = cv2.VideoCapture(source["path"])
        if not cap.isOpened():
            raise RemixError(f"Cannot open source {source_id}", 3)
        try:
            fps = _source_fps(source, cap)
            ranges = union([_frame_range(x, fps) for x in timeline["items"] if x["src"]["source"] == source_id])
            ranges_by_source[source_id] = {"fps": {"num": fps.numerator, "den": fps.denominator},
                                          "intervals": [{"start_f": a, "end_f": b} for a, b in ranges]}
            for first, last in ranges:
                tracks = []
                cap.set(cv2.CAP_PROP_POS_FRAMES, first)
                for frame_index in range(first, last):
                    ok, frame = cap.read()
                    if not ok:
                        raise RemixError(f"Source {source_id} ended during face scan", 3)
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    boxes = [list(map(int, b)) for b in detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24))]
                    active = [t for t in tracks if frame_index - t["last_detected"] <= gap_max + 1]
                    used = set()
                    for box in boxes:
                        candidates = [(_iou(box, t["last_bbox"]), t) for t in active if t["cluster"] not in used]
                        similarity, track = max(candidates, key=lambda x: x[0]) if candidates else (0, None)
                        if similarity < 0.25:
                            track = {"cluster": f"c{len(clusters) + 1:04d}", "source": source_id, "detections": [],
                                     "identity": "unknown", "age_estimate": None, "nsfw": "unknown", "landmarks": None}
                            clusters.append(track)
                            tracks.append(track)
                            # A representative is an observed frame, never an invented identity.
                            x, y, w, h = box
                            image_path = contact / (track["cluster"] + ".jpg")
                            cv2.imwrite(str(image_path), frame[y:y + h, x:x + w])
                            track["representative"] = str(image_path.relative_to(run))
                        previous = track.get("last_detected")
                        if previous is not None and frame_index > previous + 1:
                            previous_box = track["last_bbox"]
                            for missing in range(previous + 1, frame_index):
                                # Union boxes is conservative for short gaps; not identity inference.
                                x = min(previous_box[0], box[0]); y = min(previous_box[1], box[1])
                                right = max(previous_box[0] + previous_box[2], box[0] + box[2])
                                bottom = max(previous_box[1] + previous_box[3], box[1] + box[3])
                                track["detections"].append({"frame": missing, "bbox": [x, y, right - x, bottom - y], "predicted": True})
                        track["detections"].append({"frame": frame_index, "bbox": box, "predicted": False})
                        track["last_detected"], track["last_bbox"] = frame_index, box
                        used.add(track["cluster"])
        finally:
            cap.release()
    for track in clusters:
        track.pop("last_detected", None); track.pop("last_bbox", None)
    obj = {"version": 1, "backend": "opencv-haar", "backend_version": cv2.__version__, "clusters": clusters,
           "source_ranges": ranges_by_source, "nsfw": "unknown", "deceptive_intent": "unknown",
           "limitations": ["Only frontal Haar detections and IoU tracks; recall is incomplete.",
                            "No identity clustering, landmarks, age estimation or NSFW classifier is installed.",
                            "Visually review every selected frame; full-frame blur is available for detector misses."],
           "snapshot": snapshot(run, ["timeline.json", "sources.json"])}
    return save_artifact(run, "faces.json", obj)


def faces_match(run, config_dir=None):
    run = Path(run)
    scan = read_json(run / "faces.json")
    ensure_snapshot(run, scan.get("snapshot", {}))
    # Reading the registry does not imply successful biometric identification.
    _, registry = _registry(config_dir)
    obj = {"version": 1, "backend": None, "available": False, "registry_entries": len(registry["items"]),
           "items": [{"cluster": x["cluster"], "identity_id": None, "status": "unknown", "similarity": None} for x in scan["clusters"]],
           "notice": "No licensed recognition backend is installed; tracks cannot be matched to identities.",
           "snapshot": snapshot(run, ["faces.json", "timeline.json", "sources.json"])}
    return save_artifact(run, "face-identities.json", obj)


def guard_plan(run, plan, scan, matches=None, config_dir=None, require_backend=True):
    """Fail closed for synthesis; reusable by future licensed adapters and unit tests."""
    if scan.get("nsfw") is True or any(x.get("nsfw") is True for x in scan.get("clusters", [])):
        raise RemixError("nsfw_flagged: face processing disabled for this run", 4)
    root, registry = _registry(config_dir)
    brief = read_json(Path(run) / "brief.json")
    project = brief.get("project", brief.get("run_id", Path(run).name))
    clusters = {x["cluster"]: x for x in scan.get("clusters", [])}
    identities = {x.get("cluster"): x for x in (matches or {}).get("items", [])}
    for item in plan.get("items", []):
        mode = item.get("mode")
        if mode not in ("keep", "blur", "swap", "anonymize"):
            raise RemixError("Unsupported face mode")
        if mode == "swap":
            _check_identity(item.get("subject"), "subject", plan, root, registry, project)
            _check_identity(item.get("target"), "target", plan, root, registry, project)
        if mode in ("swap", "anonymize"):
            age = clusters.get(item["cluster"], {}).get("age_estimate")
            if isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age):
                raise RemixError("Age evidence is unavailable; only keep or blur is available", 5)
            if age < thresholds()["face"]["minor_age_buffer"]:
                raise RemixError("possible_minor: only keep or blur is available", 4)
            track = clusters.get(item["cluster"], {})
            if scan.get("nsfw") is not False or track.get("nsfw") is not False:
                raise RemixError("NSFW evidence is unavailable; synthesis is disabled", 5)
            if mode == "swap":
                matched = identities.get(item["cluster"], {})
                if matched.get("status") != "confirmed" or matched.get("identity_id") != item.get("subject"):
                    raise RemixError("Subject identity is unknown or mismatched; synthesis is disabled", 5)
            if mode == "swap" and (scan.get("deceptive_intent") is True or isinstance(scan.get("deceptive_intent"), (int, float)) and scan["deceptive_intent"] >= thresholds()["face"]["deceptive_intent_max"]):
                raise RemixError("deceptive_intent: swap is disabled", 4)
            if mode == "swap" and not (scan.get("deceptive_intent") is False or
                (not isinstance(scan.get("deceptive_intent"), bool) and isinstance(scan.get("deceptive_intent"), (int, float))
                 and math.isfinite(scan["deceptive_intent"]) and 0 <= scan["deceptive_intent"] < thresholds()["face"]["deceptive_intent_max"])):
                raise RemixError("Deceptive-intent evidence is unavailable; swap is disabled", 5)
            if require_backend:
                raise RemixError("No licensed synthesis/identity/age/NSFW/policy/label validation pipeline is installed; swap and anonymize are unavailable", 5)
    return {"ok": True}


def faces_plan(run, mappings=(), default="keep", purpose="personal_creation", platform="local", config_dir=None, full_frame=False):
    run = Path(run)
    scan = read_json(run / "faces.json")
    ensure_snapshot(run, scan.get("snapshot", {}))
    matches = read_json(run / "face-identities.json") if (run / "face-identities.json").is_file() else {"items": []}
    if matches.get("snapshot"):
        ensure_snapshot(run, matches["snapshot"])
    identities = {x["cluster"]: x.get("identity_id") for x in matches.get("items", []) if x.get("status") == "confirmed"}
    parsed = {}
    for mapping in mappings:
        try:
            cluster, mode = mapping.split("=", 1)
        except ValueError:
            raise RemixError("Face mappings use cluster=mode or cluster=swap:identity_id") from None
        if cluster in parsed:
            raise RemixError("Duplicate face mapping")
        parsed[cluster] = mode
    clusters = {x["cluster"] for x in scan["clusters"]}
    source_clusters = {"source:" + sid for sid in scan.get("source_ranges", {})}
    if set(parsed) - clusters - source_clusters:
        raise RemixError("Face mapping references an unknown track/source")
    rows = []
    for cluster in sorted(clusters | set(parsed) | (source_clusters if full_frame else set())):
        value = parsed.get(cluster, default if not cluster.startswith("source:") or full_frame else "keep")
        parts = value.split(":", 1)
        mode = parts[0]
        row = {"cluster": cluster, "mode": mode}
        if cluster.startswith("source:"):
            if mode not in ("keep", "blur"):
                raise RemixError("Full-frame source processing supports keep/blur only", 5)
            row["source"] = cluster[len("source:"):]
            row["full_frame"] = True
        if mode == "swap":
            row.update(subject=identities.get(cluster), target=parts[1] if len(parts) == 2 else None)
            row["consents"] = [row["subject"], row["target"]]
        if mode == "blur":
            row["style"] = "gaussian"
        rows.append(row)
    obj = {"version": 1, "purpose": purpose, "platform": platform, "items": rows,
           "snapshot": snapshot(run, ["faces.json", "face-identities.json", "timeline.json", "sources.json"])}
    guard_plan(run, obj, scan, matches, config_dir)
    return save_artifact(run, "face-plan.json", obj)


def _load_plan(run, config_dir=None):
    run = Path(run)
    plan = read_json(run / "face-plan.json")
    scan = read_json(run / "faces.json")
    ensure_snapshot(run, plan.get("snapshot", {}))
    matches = read_json(run / "face-identities.json") if (run / "face-identities.json").is_file() else None
    guard_plan(run, plan, scan, matches, config_dir=config_dir)
    return plan, scan


def faces_prepare(run, backend="opencv-blur", config_dir=None):
    run = Path(run)
    plan, scan = _load_plan(run, config_dir)
    if backend != "opencv-blur":
        raise RemixError("Requested face backend is unavailable; install and verify a licensed adapter", 5)
    selected = {x["cluster"] for x in plan["items"] if x["mode"] == "blur"}
    frames = set()
    for track in scan["clusters"]:
        if track["cluster"] in selected:
            frames.update((track["source"], d["frame"]) for d in track["detections"])
    for item in plan["items"]:
        if item["mode"] == "blur" and item.get("full_frame"):
            frames.update((item["source"], f) for interval in scan["source_ranges"][item["source"]]["intervals"] for f in range(interval["start_f"], interval["end_f"]))
    obj = {"version": 1, "backend": backend, "frames_to_process": len(frames), "uploads_images": False,
           "transfers": [], "cost": {"currency": None, "amount": 0, "basis": "local CPU; no API call"},
           "estimated_seconds": None, "license": BLUR_LICENSE.__dict__, "limitations": scan["limitations"],
           "snapshot": snapshot(run, ["face-plan.json", "faces.json", "timeline.json", "sources.json"])}
    return save_artifact(run, "face-transfer-preview.json", obj)


def blur_frame(frame, boxes, full_frame=False):
    """Blur an actual image array; expose this primitive for synthetic-only tests."""
    cv2 = _cv2()
    result = frame.copy()
    height, width = result.shape[:2]
    boxes = [[0, 0, width, height]] if full_frame else boxes
    for x, y, w, h in boxes:
        # Include a margin around faces; clip the box safely at the image boundary.
        pad = max(4, min(w, h) // 5) if not full_frame else 0
        left, top = max(0, x - pad), max(0, y - pad)
        right, bottom = min(width, x + w + pad), min(height, y + h + pad)
        if right <= left or bottom <= top:
            raise RemixError("Invalid face rectangle")
        roi = result[top:bottom, left:right]
        # Large Gaussian kernel removes facial detail; review is still mandatory.
        kernel = max(31, (min(right - left, bottom - top) // 2) | 1)
        result[top:bottom, left:right] = cv2.GaussianBlur(roi, (kernel, kernel), 0)
    return result


def faces_apply(run, backend="opencv-blur", allow_image_upload=False, config_dir=None):
    run = Path(run)
    plan, scan = _load_plan(run, config_dir)
    if backend != "opencv-blur":
        # Upload consent is necessary for future API adapters, never sufficient.
        if backend.startswith("api") and not allow_image_upload:
            raise RemixError("Image upload authorization is required for API face backends", 4)
        raise RemixError("Requested licensed face backend is unavailable", 5)
    cv2 = _cv2()
    sources = {x["id"]: x for x in check_sources(run)}
    track_map = {x["cluster"]: x for x in scan["clusters"]}
    by_source = {}
    for item in plan["items"]:
        if item["mode"] != "blur":
            continue
        source_id = item.get("source") or track_map[item["cluster"]]["source"]
        entry = by_source.setdefault(source_id, {"boxes": {}, "full_ranges": [], "items": []})
        entry["items"].append(item)
        if item.get("full_frame"):
            entry["full_ranges"].extend((x["start_f"], x["end_f"]) for x in scan["source_ranges"][source_id]["intervals"])
        else:
            for detection in track_map[item["cluster"]]["detections"]:
                entry["boxes"].setdefault(detection["frame"], []).append(detection["bbox"])
    cache = run / "cache" / "face"
    cache.mkdir(parents=True, exist_ok=True)
    outputs, audit = {}, []
    for source_id, ops in by_source.items():
        source = sources[source_id]
        if source.get("media", {}).get("vfr_possible"):
            raise RemixError("Core blur cannot safely remap VFR face regions; a timestamp-aware backend is required", 5)
        key = digest({"source": source["fingerprint"], "ops": ops, "plan": plan, "backend": backend, "version": cv2.__version__})
        output = cache / (key + ".mp4")
        record_file = cache / (key + ".json")
        valid_cache = output.is_file() and record_file.is_file() and read_json(record_file).get("fingerprint") == fingerprint(output)
        if not valid_cache:
            cap = cv2.VideoCapture(source["path"])
            if not cap.isOpened():
                raise RemixError(f"Cannot open source {source_id}", 3)
            temporary = tempfile.TemporaryDirectory(prefix="face-", dir=cache)
            writer = None
            frames_written = processed = 0
            try:
                fps = _source_fps(source, cap)
                first_ok, first_frame = cap.read()
                if not first_ok:
                    raise RemixError("Cannot decode source for blur", 3)
                height, width = first_frame.shape[:2]
                silent = Path(temporary.name) / "silent.mp4"
                writer = cv2.VideoWriter(str(silent), cv2.VideoWriter_fourcc(*"mp4v"), float(fps), (width, height))
                if not writer.isOpened():
                    raise RemixError("OpenCV video encoder unavailable", 5)
                frame = first_frame
                while True:
                    full = any(a <= frames_written < b for a, b in ops["full_ranges"])
                    boxes = ops["boxes"].get(frames_written, [])
                    if full or boxes:
                        frame = blur_frame(frame, boxes, full)
                        processed += 1
                    writer.write(frame)
                    frames_written += 1
                    ok, frame = cap.read()
                    if not ok:
                        break
                writer.release(); writer = None
                maximum_selected = max([x for x in ops["boxes"]] + [b - 1 for a, b in ops["full_ranges"]], default=-1)
                if frames_written <= maximum_selected:
                    raise RemixError("Source decode ended before all selected face frames were processed", 3)
                # Restore the unmodified source audio while preserving full source frame timing.
                command(["ffmpeg", "-v", "error", "-y", "-i", silent, "-i", source["path"],
                         "-map", "0:v:0", "-map", "1:a?", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                         "-c:a", "copy", "-movflags", "+faststart", output])
                write_json(record_file, {"fingerprint": fingerprint(output), "frames_written": frames_written, "processed_frames": processed})
            finally:
                cap.release()
                if writer:
                    writer.release()
                temporary.cleanup()
        record = read_json(record_file)
        outputs[source_id] = {"path": str(output.resolve()), "fingerprint": fingerprint(output), "cache_key": key,
                              "source_fingerprint": source["fingerprint"], "processed_frames": record["processed_frames"]}
        for item in ops["items"]:
            audit.append({"ts": now(), "run": run.name, "cluster": item["cluster"], "mode": "blur", "source": source_id,
                          "subject": None, "target": None, "consent_docs": [], "backend": backend,
                          "backend_version": cv2.__version__, "cache_key": key,
                          "frames": scan["source_ranges"][source_id]["intervals"] if item.get("full_frame") else [d["frame"] for d in track_map[item["cluster"]]["detections"]],
                          "label": {"explicit": False, "implicit": False}, "full_frame": item.get("full_frame", False)})
    audit_path = run / "face-audit.jsonl"
    existing = read_jsonl(audit_path) if audit_path.is_file() else []
    write_jsonl(audit_path, existing + audit)
    obj = {"version": 1, "sources": outputs, "modes": sorted({x["mode"] for x in plan["items"]}),
           "visual_review_required": True, "snapshot": snapshot(run, ["face-plan.json", "faces.json", "timeline.json", "sources.json"])}
    return save_artifact(run, "face-applied.json", obj)


def verify_render(run, timeline=None, config_dir=None):
    run = Path(run)
    if not (run / "face-plan.json").is_file():
        return {}
    plan, scan = _load_plan(run, config_dir)
    if not any(x["mode"] == "blur" for x in plan["items"]):
        return {}
    applied = read_json(run / "face-applied.json")
    ensure_snapshot(run, applied.get("snapshot", {}))
    required = {"face-plan.json", "faces.json", "timeline.json", "sources.json"}
    if not required.issubset(applied.get("snapshot", {})):
        raise RemixError("Face cache is missing its plan/source snapshot", 3)
    if timeline is not None and digest(timeline) != digest(read_json(run / "timeline.json")):
        raise RemixError("Face cache does not match the requested timeline", 3)
    tracks = {x["cluster"]: x for x in scan["clusters"]}
    expected = {x.get("source") or tracks[x["cluster"]]["source"] for x in plan["items"] if x["mode"] == "blur"}
    if set(applied.get("sources", {})) != expected:
        raise RemixError("Face cache does not cover every planned blurred source", 3)
    sources = {x["id"]: x for x in check_sources(run)}
    for sid, obj in applied["sources"].items():
        path = Path(obj["path"])
        if not path.resolve().is_relative_to((run / "cache" / "face").resolve()):
            raise RemixError("Face processed source must remain inside the face cache", 3)
        if sid not in sources or obj.get("source_fingerprint") != sources[sid]["fingerprint"] or fingerprint(path) != obj["fingerprint"]:
            raise RemixError("Processed face cache changed; rerun faces apply", 3)
    return applied["sources"]


def faces_qa(run, config_dir=None):
    run = Path(run)
    plan, scan = _load_plan(run, config_dir)
    outputs = verify_render(run, config_dir=config_dir)
    obj = {"version": 1, "status": "visual_review_required", "ok": None,
           "processed_sources": list(outputs), "modes": sorted({x["mode"] for x in plan["items"]}),
           "checks": {"source_cache_integrity": True, "identity_similarity": "unavailable", "residual_identity": "unavailable",
                      "age_detection": "unavailable", "nsfw_detection": "unavailable", "temporal_stability": "visual_review_required",
                      "boundary_artifacts": "visual_review_required", "occlusion": "visual_review_required", "aigc_labels": "not_required_for_blur"},
           "limitations": scan["limitations"], "notice": "Blur has been applied to selected regions. Automated identity removal has not been verified; inspect all selected frames.",
           "snapshot": snapshot(run, ["face-plan.json", "face-applied.json", "faces.json", "timeline.json", "sources.json"])}
    return save_artifact(run, "face-qa.json", obj)


def verify_labels(plan, explicit, implicit):
    if any(x.get("mode") in ("swap", "anonymize") for x in plan.get("items", [])) and not (explicit is True and implicit is True):
        raise RemixError("AI synthesized faces require both explicit and implicit labels", 4)
    return {"ok": True}
