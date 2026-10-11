"""Stable request identifiers include the model and question-library version."""
import hashlib
import json

def request_hash(payload, question_version, mock=False):
    data = {"request": payload, "question_version": question_version, "mock": bool(mock)}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                   separators=(",", ":"), allow_nan=False).encode()).hexdigest()
