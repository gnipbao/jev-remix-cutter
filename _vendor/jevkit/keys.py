"""Credential reads only; callers must obtain transfer consent first."""
import os
from pathlib import Path

def configured():
    return bool(os.environ.get("TYPESAFE_API_KEY", "").strip())

def read_key(key_file=None):
    # Workspace policy: a global key always wins over a supplied fallback.
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key and key_file:
        try:
            key = Path(key_file).expanduser().read_text().strip()
        except OSError:
            raise ValueError("Cannot read private key file") from None
    if not key or "\n" in key or "\r" in key:
        raise ValueError("TYPESAFE_API_KEY unavailable; supply a private --key-file path")
    return key
