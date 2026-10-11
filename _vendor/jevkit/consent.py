"""A transfer preview describes exactly the text requests that may leave a run."""
import json

def preview(requests, destination, model, uncached):
    return {"version": 1, "destination": destination, "model": model,
            "request_count": len(requests), "uncached_requests": uncached,
            "estimated_input_tokens": sum((len(json.dumps(r, ensure_ascii=False).encode()) + 3) // 4 for r in requests),
            "estimate_note": "Token estimate only; prices and billed usage belong to the provider console.",
            "data": "Text descriptions, editorial needs and questions; no source paths, files, images or credentials.",
            "requests": requests}
