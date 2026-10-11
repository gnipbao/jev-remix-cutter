"""Content snapshots for artifact freshness checks."""
import hashlib
from pathlib import Path

def snapshot(run, names):
    return {name: hashlib.sha256((Path(run) / name).read_bytes()).hexdigest()
            for name in names if (Path(run) / name).is_file()}

def check(run, dependencies):
    for name, expected in dependencies.items():
        p = Path(run) / name
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != expected:
            raise ValueError("Stale artifact: " + name)
