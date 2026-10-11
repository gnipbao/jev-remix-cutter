"""Local contracts, atomic artifacts and dependency snapshots."""
from __future__ import annotations
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent
_SOURCE_ROOT = _PACKAGE_ROOT.parents[1]
# Checkouts keep editable decision files at their root. Wheels carry the same
# byte-for-byte resources inside the package and need no source checkout.
ROOT = _SOURCE_ROOT if (_SOURCE_ROOT / "thresholds.toml").is_file() else _PACKAGE_ROOT / "_data"

class RemixError(Exception):
    def __init__(self, message, code=2, details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (OSError, ValueError):
        raise RemixError(f"Cannot read valid JSON: {Path(path).name}") from None

def write_json(path, data):
    path = Path(path)
    if path.is_symlink():
        raise RemixError("Refusing to overwrite a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return data

def read_jsonl(path):
    try:
        return [json.loads(line, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x))) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        raise RemixError(f"Cannot read valid JSONL: {Path(path).name}") from None

def write_jsonl(path, rows):
    path = Path(path)
    if path.is_symlink():
        raise RemixError("Refusing to overwrite a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return rows

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def fingerprint(path):
    path = Path(path)
    try:
        st = path.stat()
        h = hashlib.sha256()
        with path.open("rb") as f:
            h.update(f.read(1048576))
            f.seek(max(0, st.st_size - 1048576))
            h.update(f.read(1048576))
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "edge_sha256": h.hexdigest()}
    except OSError:
        raise RemixError(f"Cannot access source: {path.name}", 3) from None

def run_dir(value, create=False):
    p = Path(value).expanduser()
    if p.is_symlink():
        raise RemixError("Run directory must not be a symlink")
    p = p.resolve()
    if p == ROOT or ROOT in p.parents or p == _PACKAGE_ROOT or _PACKAGE_ROOT in p.parents:
        raise RemixError("Run artifacts must be outside the Skill directory")
    if create:
        if p.exists() and any(p.iterdir()):
            raise RemixError("Run directory already contains files; init does not overwrite")
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
    elif not (p / "brief.json").is_file():
        raise RemixError("Run is not initialized; use init")
    return p

def archive(run, name):
    p = Path(run) / name
    if p.exists():
        dst = Path(run) / "history" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + "-" + p.name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)

def save_artifact(run, name, data):
    archive(run, name)
    return write_json(Path(run) / name, data)

def command(args, **kwargs):
    if not shutil.which(str(args[0])):
        raise RemixError(f"Missing system dependency: {args[0]}", 5)
    result = subprocess.run([str(x) for x in args], capture_output=True, **kwargs)
    if result.returncode:
        error = result.stderr.decode(errors="replace") if isinstance(result.stderr, bytes) else result.stderr
        raise RemixError(f"{Path(str(args[0])).name} failed: {error[-1600:]}")
    return result

def thresholds():
    return tomllib.loads((ROOT / "thresholds.toml").read_text())

def finite(value, name, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise RemixError(f"{name} must be a finite JSON number")
    if (low is not None and value < low) or (high is not None and value > high):
        raise RemixError(f"{name} outside allowed range")
    return value

def validate(name, data):
    try:
        import jsonschema
    except ImportError:
        raise RemixError("Missing jsonschema; install the core dependencies", 5) from None
    schema = read_json(_PACKAGE_ROOT / "contracts" / (name + ".schema.json"))
    try:
        jsonschema.Draft202012Validator(schema).validate(data)
    except jsonschema.ValidationError as e:
        raise RemixError(f"Invalid {name}: {'.'.join(str(x) for x in e.path)} {e.message}") from None
    return data

def snapshot(run, names):
    return {name: file_hash(Path(run) / name) for name in names if (Path(run) / name).is_file()}

def ensure_snapshot(run, values):
    for name, h in values.items():
        p = Path(run) / name
        if not p.is_file() or file_hash(p) != h:
            raise RemixError(f"Stale artifact: {name} changed; rebuild slots → score → cast → assemble", 3)

def check_sources(run):
    obj = read_json(Path(run) / "sources.json")
    rows = obj.get("items", obj) if isinstance(obj, dict) else obj
    for src in rows:
        if fingerprint(src["path"]) != src["fingerprint"]:
            raise RemixError(f"Source {src['id']} changed; rerun ingest → cards → slots → score → cast → assemble", 3)
    return rows

def ensure_decision_hashes(data):
    expected = data.get("decision_hashes")
    if not expected:
        if "cast.json" in data.get("dependencies", {}):
            raise RemixError("Timeline has no decision configuration snapshot; rerun assemble", 3)
        return
    fixed = {"questions":ROOT / "questions" / "remix.yaml", "thresholds":ROOT / "thresholds.toml"}
    for name, path in fixed.items():
        if expected.get(name) != file_hash(path):
            raise RemixError(f"Decision configuration {name} changed; rerun score → cast → assemble", 3)
    if expected.get("pack_id"):
        from .planning import load_pack
        if digest(load_pack(expected["pack_id"])) != expected.get("pack"):
            raise RemixError("Pack changed; rerun slots → score → cast → assemble", 3)
