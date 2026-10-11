"""Evidence-backed local cards and an explicitly authorized image provider."""
from __future__ import annotations
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from .common import ROOT, RemixError, archive, digest, file_hash, read_json, read_jsonl, thresholds, validate, write_json, write_jsonl

def verbalize(value, quantiles, labels=("低", "中", "高")):
    """One numeric-to-word boundary; exact boundaries go to the upper bin."""
    if value is None:
        return "uncertain"
    from math import isfinite
    if not isinstance(value, (int, float)) or not isfinite(value):
        return "uncertain"
    if len(labels) != len(quantiles) + 1 or list(quantiles) != sorted(quantiles):
        raise RemixError("Invalid verbalization boundaries")
    return labels[sum(value >= q for q in quantiles)]

def prepare(run):
    rows = read_jsonl(run / "shots.jsonl")[:thresholds()["sense"]["max_cards"]]
    files = []
    for row in rows:
        p = run / row.get("contact", "")
        if not p.is_file() or run.resolve() not in p.resolve().parents:
            raise RemixError(f"Missing contact sheet for {row['shot_id']}; rerun ingest")
        files.append({"shot_id":row["shot_id"],"file":str(p.relative_to(run)),"sha256":file_hash(p),"bytes":p.stat().st_size})
    endpoint = os.environ.get("JEV_REMIX_VLM_ENDPOINT", "")
    parsed = urllib.parse.urlsplit(endpoint)
    safe_endpoint = endpoint if parsed.scheme == "https" and parsed.netloc and not any((parsed.username, parsed.password, parsed.query, parsed.fragment)) else "not configured or invalid HTTPS endpoint"
    manifest = {"version":1,"destination":safe_endpoint,"image_count":len(files),"images":files,"data":"Selected shot contact sheets only; no source video, no credentials","fields":["画面","人物","景别","运镜","主体运动","运动强度","动作峰值","主色","亮度","情绪","叙事功能"],"uncertainty":"Unobserved or ambiguous facts must be uncertain"}
    write_json(run / "image-transfer-preview.json", manifest)
    write_json(run / "cards.todo.json", {"version":1,"items":[{"shot_id":x["shot_id"],"contact":x["file"]} for x in files]})
    return manifest

def evidence_hash(run, row, shot):
    paths = []
    for name in row["provenance"]["frames"]:
        p = Path(name)
        p = p if p.is_absolute() else run / p
        if not p.is_file() or run.resolve() not in p.resolve().parents:
            raise RemixError("Card evidence is missing or outside this run; prepare and reobserve cards", 3)
        paths.append(p.resolve())
    if not shot.get("contact") or (run / shot["contact"]).resolve() not in paths:
        raise RemixError("Card must cite its own current contact sheet", 3)
    return digest({"shot":shot,"frames":[file_hash(p) for p in paths]})

def validate_evidence(run, rows, shots):
    by_id = {s["shot_id"]:s for s in shots} if isinstance(shots,list) else shots
    for row in rows:
        sid = row["shot_id"]
        if sid not in by_id or row.get("evidence_hash") != evidence_hash(run,row,by_id[sid]):
            raise RemixError(f"Stale or unimported card evidence: {sid}; reobserve and cards --import", 3)
    return rows

def import_cards(run, path):
    rows = read_jsonl(path)
    shots = {x["shot_id"]:x for x in read_jsonl(run / "shots.jsonl")}
    old = {}
    if (run / "cards.jsonl").is_file():
        for existing in read_jsonl(run / "cards.jsonl"):
            try:
                validate_evidence(run,[existing],shots)
                old[existing["shot_id"]] = existing
            except RemixError:
                pass  # Stale observations are never merged into a new media pool.
    seen = set()
    for row in rows:
        validate("card", row)
        sid = row["shot_id"]
        if sid not in shots or sid in seen:
            raise RemixError(f"Unknown or duplicated shot_id: {sid}")
        seen.add(sid)
        provenance = row["provenance"]
        if provenance["mode"] == "agent":
            contact = shots[sid].get("contact")
            paths = []
            for frame in provenance["frames"]:
                p = Path(frame)
                p = p if p.is_absolute() else run / p
                if not p.is_file() or run.resolve() not in p.resolve().parents:
                    raise RemixError(f"Agent evidence must name existing run contact sheets: {sid}")
                paths.append(p.resolve())
            if contact and (run / contact).resolve() not in paths:
                raise RemixError(f"Agent card must cite its own contact sheet: {sid}")
        row["evidence_hash"] = evidence_hash(run,row,shots[sid])
        provenance["frames"] = [str((Path(f) if Path(f).is_absolute() else run / f).resolve().relative_to(run.resolve())) for f in provenance["frames"]]
        old[sid] = row
    archive(run, "cards.jsonl")
    write_jsonl(run / "cards.jsonl", [old[k] for k in sorted(old)])
    return {"imported":len(rows),"total":len(old),"path":str(run / "cards.jsonl")}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RemixError("Image provider redirect refused; credential not forwarded")

def provider(run, name, allow_image_upload=False):
    manifest = prepare(run)
    if not allow_image_upload:
        raise RemixError("Review image-transfer-preview.json and authorize image transfer before --allow-image-upload", 4)
    if name != "openai-compatible":
        raise RemixError("Supported image provider: openai-compatible; use --import for local Agent cards", 5)
    endpoint = os.environ.get("JEV_REMIX_VLM_ENDPOINT", "")
    parsed = urllib.parse.urlsplit(endpoint)
    model = os.environ.get("JEV_REMIX_VLM_MODEL", "")
    key = os.environ.get("JEV_REMIX_VLM_API_KEY", "")
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RemixError("Configure an HTTPS JEV_REMIX_VLM_ENDPOINT without embedded credentials", 5)
    if not model or not key or "\n" in key or "\r" in key:
        raise RemixError("Image provider requires JEV_REMIX_VLM_MODEL and JEV_REMIX_VLM_API_KEY environment variables", 5)
    opener = urllib.request.build_opener(NoRedirect())
    rows, errors = [], []
    prompt = (ROOT / "assets" / "card-prompt.txt").read_text()
    for img in manifest["images"]:
        p = run / img["file"]
        request_hash = digest({"image":img["sha256"],"prompt":prompt,"model":model,"endpoint":endpoint})
        cache = run / "cache" / "cards" / (request_hash + ".json")
        try:
            if cache.is_file():
                words = read_json(cache)["words"]
            else:
                payload = {"model":model,"messages":[{"role":"user","content":[{"type":"text","text":prompt},{"type":"image_url","image_url":{"url":"data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode()}}]}],"response_format":{"type":"json_object"}}
                req = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers={"Content-Type":"application/json","Authorization":"Bearer " + key}, method="POST")
                with opener.open(req, timeout=90) as response:
                    raw = json.loads(response.read(2000000))
                words = json.loads(raw["choices"][0]["message"]["content"])["words"]
                validate("card", {"shot_id":img["shot_id"],"words":words,"provenance":{"mode":"provider","frames":[img["file"]],"model":model}})
                write_json(cache, {"words":words,"model":str(raw.get("model",model)),"usage":raw.get("usage",{})})
            rows.append({"shot_id":img["shot_id"],"words":words,"provenance":{"mode":"provider","model":model,"frames":[img["file"]],"uncertain":[k for k,v in words.items() if v == "uncertain"]}})
        except urllib.error.HTTPError as e:
            errors.append({"shot_id":img["shot_id"],"error":f"Provider HTTP {e.code}; response omitted"})
        except (ValueError, KeyError, urllib.error.URLError, TimeoutError, RemixError):
            errors.append({"shot_id":img["shot_id"],"error":"Provider failed or returned invalid card; response omitted"})
    write_jsonl(run / "cards.provider.jsonl", rows)
    if rows:
        import_cards(run, run / "cards.provider.jsonl")
    write_json(run / "image-transfer-log.json", {"version":1,"authorized":True,"endpoint":endpoint,"manifest_hash":digest(manifest),"completed":len(rows),"errors":errors})
    if errors:
        raise RemixError("Partial image card failure; retry reuses successful caches", 2, {"errors":errors})
    return {"completed":len(rows),"model":model}
