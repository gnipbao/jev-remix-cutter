"""Small read-only source fingerprint; not a perceptual similarity metric."""
import hashlib
from pathlib import Path

def fingerprint(path):
    p = Path(path)
    st = p.stat()
    h = hashlib.sha256()
    with p.open("rb") as f:
        h.update(f.read(1_048_576))
        f.seek(max(0, st.st_size - 1_048_576))
        h.update(f.read(1_048_576))
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "edge_sha256": h.hexdigest()}
