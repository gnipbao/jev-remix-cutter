"""Text-only typed judgments, bounded request graphs and resumable caches.

Mock and service caches live in different directories. Cached service results
contain only validated answers, model and usage, never untrusted response blobs.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import json
import math
import re
from threading import Lock
from uuid import uuid4

from jevkit.jev import ENDPOINT, JevError, call_api
from jevkit.keys import read_key
from .common import (ROOT, RemixError, check_sources, digest, ensure_snapshot,
                     file_hash, finite, read_json, read_jsonl, snapshot,
                     thresholds, validate, write_json, write_jsonl)
from .cards import verbalize, validate_evidence

SLOT_QUESTIONS = ("slot_fit", "mood_match", "focus_on_required_char",
                  "excluded", "grade_outlier", "slowmo_worth", "sfx_type")
PAIR_QUESTIONS = ("relation_to_prev", "transition_type", "smoothness", "meaningful_link")
DEPENDENCIES = ("brief.json", "sources.json", "shots.jsonl", "cards.jsonl",
                "intent.json", "slots.json", "music.json", "narration.json", "licenses/catalog.json")


def load_questions():
    try:
        import yaml
    except ImportError:
        raise RemixError("Missing PyYAML; install core dependencies", 5) from None
    obj = yaml.safe_load((ROOT / "questions" / "remix.yaml").read_text())
    if obj.get("version") != 1 or not isinstance(obj.get("questions"), dict):
        raise RemixError("Unsupported question-library version", 5)
    for name in SLOT_QUESTIONS + PAIR_QUESTIONS:
        q = obj["questions"].get(name, {})
        if q.get("type") not in ("score", "noul", "choice") or not q.get("instructions"):
            raise RemixError(f"Invalid question: {name}")
        if q["type"] == "score" and len(q.get("criteria", [])) < 2:
            raise RemixError(f"Score criteria missing: {name}")
        if q["type"] == "choice" and not isinstance(q.get("criteria"), dict):
            raise RemixError(f"Choice criteria missing: {name}")
    return obj


def _words_only(value):
    if isinstance(value, str):
        return True
    if isinstance(value, list):
        return all(_words_only(x) for x in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _words_only(v) for k, v in value.items())
    return False


def _verbalized_features(shot, shots):
    result = {}
    for field, label in (("motion", "运动强度"), ("brightness", "亮度"),
                         ("saturation", "饱和度"), ("sharpness", "清晰度")):
        values = sorted(x.get("features", {}).get(field) for x in shots
                        if isinstance(x.get("features", {}).get(field), (int, float)))
        if values:
            # Quantiles are relative to this source pool, not absolute claims.
            qs = [values[round((len(values)-1)*p)] for p in (1/3, 2/3)]
            result[label] = verbalize(shot.get("features", {}).get(field), qs)
        else:
            result[label] = "uncertain"
    return result


def _evidence(shot, card, shots):
    # Only the imported observation words and pool-relative words cross the API.
    return {"observed": card["words"], "measured_words": _verbalized_features(shot, shots),
            "uncertainty": card.get("provenance", {}).get("uncertain", [])}


def _slot_words(slot, intent, fps):
    cfg = thresholds()["verbalize"]
    return {"need": slot["need"], "mood": slot.get("mood", "uncertain"),
            "section": slot.get("section", "uncertain"),
            "character": slot.get("char_focus", "unspecified"),
            "duration": verbalize((slot["end_f"]-slot["start_f"])/fps,
                [cfg["duration_short_s"], cfg["duration_long_s"]], ("短", "中", "长")),
            "theme": intent.get("theme", "unspecified"), "avoid": intent.get("avoid", []),
            "allowed_transitions": slot.get("allowed_transitions", ["hard_cut"])}


def request_for(kind, slot, shot, card, shots, intent, fps, model, library,
                previous=None, previous_card=None):
    names = SLOT_QUESTIONS if kind == "slot" else PAIR_QUESTIONS
    questions = {n: {k: v for k, v in library["questions"][n].items()
                     if k in ("type", "instructions", "criteria")} for n in names}
    state = {"slot": _slot_words(slot, intent, fps), "candidate": _evidence(shot, card, shots),
             "intent_context":{k:intent[k] for k in ("arc","hook","threads") if k in intent}}
    if previous is not None:
        state["previous"] = _evidence(previous, previous_card, shots)
    if not _words_only(state):
        raise RemixError("Jev state accepts words only; numeric observations must be verbalized")
    payload = {"model": model, "state": state, "questions": questions}
    if len(json.dumps(payload, ensure_ascii=False).encode()) > thresholds()["planning"]["max_request_bytes"]:
        raise RemixError("Jev evidence exceeds text request budget; shorten imported cards")
    return payload


def parse_response(raw, questions):
    if not isinstance(raw, dict) or not isinstance(raw.get("answers"), dict):
        raise RemixError("Jev response is missing typed answers")
    result = {}
    for name, question in questions.items():
        answer = raw["answers"].get(name, {})
        if not isinstance(answer, dict) or answer.get("type") != question["type"]:
            raise RemixError(f"Missing or incorrect Jev answer type: {name}")
        kind = question["type"]
        if kind == "score":
            maximum = len(question["criteria"])-1
            value = finite(answer.get("score"), name, 0, maximum)
            result[name] = {"type": kind, "score": value, "value": value/maximum,
                            "confidence": finite(answer.get("confidence"), name+" confidence", 0, 1)}
        elif kind == "noul":
            value = finite(answer.get("noul", answer.get("prob")), name, 0, 1)
            result[name] = {"type": kind, "prob": value, "value": value}
        else:
            value = answer.get("choice")
            if value not in question["criteria"]:
                raise RemixError(f"Unknown Jev choice: {name}")
            confidence = finite(answer.get("confidence"), name+" confidence", 0, 1)
            probabilities = answer.get("probabilities")
            if not isinstance(probabilities,dict) or set(probabilities) != set(question["criteria"]):
                raise RemixError(f"Choice probabilities must cover every criterion: {name}")
            probabilities = {k:finite(v,name+" probability",0,1) for k,v in probabilities.items()}
            if abs(sum(probabilities.values())-1)>1e-6:
                raise RemixError(f"Choice probabilities do not sum to one: {name}")
            result[name] = {"type": kind, "choice": value, "confidence":confidence,
                            "probabilities":probabilities}
    usage = raw.get("usage", {})
    if not isinstance(usage, dict):
        raise RemixError("Jev usage must be an object")
    return {"answers": result, "model": str(raw.get("model", "unknown")),
            "usage": {k: int(finite(usage.get(k, 0), k, 0))
                      for k in ("input_tokens", "output_tokens")}}


def _numeric_diagnostic(raw, questions):
    """Whitelist finite typed response fields without accepting bad sums.

    This is diagnostic evidence, never a scoring cache. Unknown response bodies,
    question keys, and arbitrary response strings are not persisted.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("answers"), dict):
        return None
    answers = {}
    try:
        for name, question in questions.items():
            answer = raw["answers"].get(name)
            if not isinstance(answer, dict) or answer.get("type") != question["type"]:
                return None
            kind = question["type"]
            if kind == "noul":
                answers[name] = {"type":kind,"noul":finite(answer.get("noul",answer.get("prob")),name,0,1)}
            elif kind == "score":
                answers[name] = {"type":kind,"score":finite(answer.get("score"),name,0,len(question["criteria"])-1),
                                 "confidence":finite(answer.get("confidence"),name+" confidence",0,1)}
            elif kind == "choice":
                probs = answer.get("probabilities")
                if (answer.get("choice") not in question["criteria"] or not isinstance(probs,dict)
                        or set(probs) != set(question["criteria"])):
                    return None
                answers[name] = {"type":kind,"choice":answer["choice"],
                    "confidence":finite(answer.get("confidence"),name+" confidence",0,1),
                    "probabilities":{k:finite(v,name+" probability",0,1) for k,v in probs.items()}}
            else:
                return None
        usage = raw.get("usage", {})
        if not isinstance(usage, dict):
            return None
        usage = {k:usage.get(k,0) for k in ("input_tokens","output_tokens")}
        if not all(isinstance(v,int) and not isinstance(v,bool) and v >= 0 for v in usage.values()):
            return None
    except (RemixError, TypeError):
        return None
    model = raw.get("model", "unknown")
    if not isinstance(model,str) or not (re.fullmatch(r"jev-(?:latest|[0-9]+(?:\.[0-9]+){1,3})",model) or model == "MOCK-only"):
        model = "unrecognized"
    return {"answers":answers,"model":model,"usage":usage}


def _mock(payload):
    seed = int(digest(payload)[:8], 16)
    answers = {}
    for i, (name, q) in enumerate(payload["questions"].items()):
        x = ((seed >> (i % 16)) % 100)/100
        if q["type"] == "score":
            answers[name] = {"type": "score", "score": (2 if x < .65 else 3), "confidence": 1.0}
        elif q["type"] == "noul":
            answers[name] = {"type": "noul", "noul": (.1*x if name in ("grade_outlier","excluded") else .7+.3*x)}
        else:
            preferred = {"sfx_type": "none", "transition_type": "hard_cut", "relation_to_prev": "continue"}
            chosen = preferred.get(name, next(iter(q["criteria"])))
            answers[name] = {"type": "choice", "choice": chosen,"confidence":1.0,
                             "probabilities":{k:float(k==chosen) for k in q["criteria"]}}
    return {"answers": answers, "model": "MOCK-only", "usage": {}}


def _rank_candidates(slots, shots, cards, top_k, excluded=()):
    from .planning import feasible_shot
    result = {}
    for slot in slots["items"]:
        ranked = []
        for shot in shots:
            if shot["shot_id"] in excluded or shot["shot_id"] not in cards or not feasible_shot(shot, cards[shot["shot_id"]], slot, slots["fps"]):
                continue
            duration = (slot["end_f"]-slot["start_f"])*1_000_000/slots["fps"]
            available = shot["end_us"]-shot["start_us"]
            rank = abs(math.log(max(1, available)/duration))
            ranked.append((rank, shot["shot_id"]))
        result[slot["id"]] = [sid for _, sid in sorted(ranked)[:top_k]]
    return result


def score(run, prepare=False, mock=False, allow_text_upload=False,
          model="jev-1.13.0", workers=4, top_k=None, pair_k=None, key_file=None):
    run = Path(run)
    if prepare and mock:
        raise RemixError("--prepare and --mock are separate scoring operations")
    sources = check_sources(run)
    slots = read_json(run / "slots.json")
    ensure_snapshot(run, slots.get("dependencies", {}))
    shots = read_jsonl(run / "shots.jsonl")
    card_rows = read_jsonl(run / "cards.jsonl")
    validate_evidence(run,card_rows,shots)
    for row in card_rows:
        validate("card", row)
    cards, by_id = {x["shot_id"]: x for x in card_rows}, {x["shot_id"]: x for x in shots}
    intent = read_json(run / "intent.json")
    cfg, library = thresholds()["judge"], load_questions()
    top_k, pair_k = top_k or cfg["top_k"], pair_k or cfg["pair_k"]
    if not all(isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in (top_k, pair_k, workers)):
        raise RemixError("k, pairs and workers must be positive integers")
    workers = min(workers, cfg["max_workers"])
    excluded = read_json(run/"brief.json").get("feedback",{}).get("exclude",[])
    if not isinstance(excluded,list) or not all(isinstance(sid,str) for sid in excluded):
        raise RemixError("brief.feedback.exclude must be an array of shot IDs")
    shortlist = _rank_candidates(slots, shots, cards, top_k,set(excluded))
    slot_jobs = []
    for slot in slots["items"]:
        for sid in shortlist[slot["id"]]:
            payload = request_for("slot", slot, by_id[sid], cards[sid], shots, intent, slots["fps"], model, library)
            slot_jobs.append(({"kind": "slot", "slot": slot["id"], "shot_id": sid}, payload))
    # Pair jobs depend on scored rankings. Prepare reports an honest upper bound.
    pair_bound = sum(min(pair_k, len(shortlist[a["id"]]))*min(pair_k, len(shortlist[b["id"]]))
                     for a, b in zip(slots["items"], slots["items"][1:]))
    bound = len(slot_jobs)+pair_bound
    if bound > cfg["max_requests"]:
        raise RemixError("Scoring graph exceeds request budget; reduce --k/--pairs or slots", 2,
                         {"request_upper_bound": bound, "max_requests": cfg["max_requests"]})
    cache = run / "cache" / ("mock" if mock else "jev")
    slot_hashes = {digest(payload) for _,payload in slot_jobs}
    missing = sum(not (cache / (digest(payload)+".json")).is_file() for _, payload in slot_jobs)
    transfer = {"version": 1, "destination": ENDPOINT, "model": model,
        "request_count": bound, "slot_requests": len(slot_jobs), "pair_request_upper_bound": pair_bound,
        "uncached_slot_requests": missing, "estimated_tokens_upper_bound": bound*thresholds()["planning"]["max_request_bytes"],
        "unique_slot_requests":len(slot_hashes),
        "uncached_unique_slot_requests":sum(not (cache/(h+".json")).is_file() for h in slot_hashes),
        "estimate_method": "Conservative UTF-8 byte upper bound; pair states selected after slot judgments",
        "data": "Shot observation words, relative feature words, intent and slot requirements; no media, paths, credentials or numeric features",
        "question_library_hash": file_hash(ROOT / "questions" / "remix.yaml"),
        "pair_selection": "Top scored candidates at each boundary; at most pairs squared requests per boundary",
        "requests": [payload for _, payload in slot_jobs]}
    write_json(run / "transfer-preview.json", transfer)
    if prepare:
        return {k: v for k, v in transfer.items() if k != "requests"}
    # An interrupted or unauthorized second stage cannot reuse an earlier
    # complete score-meta with newly replaced partial score files.
    write_json(run / "score-meta.json", {"version":1,"status":"running","mock":bool(mock),
        "dependencies":snapshot(run,DEPENDENCIES)})
    key = None
    stage_accounting, failed_response_usage = [], {"input_tokens":0,"output_tokens":0}
    def run_jobs(jobs):
        nonlocal key
        groups = {}
        for labels,payload in jobs:
            h = digest(payload)
            groups.setdefault(h,{"payload":payload,"labels":[]})["labels"].append(labels)
        stats = {"logical_jobs":len(jobs),"unique_jobs":len(groups),"request_invocations":0,
                 "mock_invocations":0,"cache_hits":0,"unique_cache_hits":0,
                 "coalesced_jobs":len(jobs)-len(groups),"diagnostics":0}
        stats_lock, diagnostic_paths = Lock(), {}
        uncached = any(not (cache / (h+".json")).is_file() for h in groups)
        if uncached and not mock:
            if not allow_text_upload:
                raise RemixError("Review transfer-preview.json; actual Jev text transfer needs --allow-text-upload", 4)
            if key is None:
                try:
                    key = read_key(key_file)
                except ValueError as exc:
                    raise RemixError(str(exc), 5) from None
        def one(h, group):
            payload = group["payload"]
            dest = cache / (h+".json")
            cached = dest.is_file()
            if cached:
                with stats_lock:
                    stats["cache_hits"] += len(group["labels"])
                    stats["unique_cache_hits"] += 1
                parsed = read_json(dest)
                # Revalidate cached canonical answers before using them.
                raw = {"answers": {n: ({"type":"noul", "noul":a["prob"]} if a["type"] == "noul" else a)
                       for n,a in parsed["answers"].items()}, "usage":parsed["usage"], "model":parsed["model"]}
                parsed = parse_response(raw, payload["questions"])
            else:
                with stats_lock:
                    stats["mock_invocations" if mock else "request_invocations"] += 1
                raw = _mock(payload) if mock else call_api(payload, key)
                try:
                    parsed = parse_response(raw, payload["questions"])
                except RemixError as exc:
                    numeric = _numeric_diagnostic(raw,payload["questions"])
                    if numeric is not None:
                        diagnostic = run/"response-diagnostics"/("mock" if mock else "jev")/(h+"-"+uuid4().hex+".json")
                        write_json(diagnostic,{"version":1,"request_hash":h,"mock":bool(mock),
                            "parser_policy":"strict-choice-sum-1e-6-v1","validation_error":str(exc),
                            "response":numeric,"response_hash":digest(numeric),
                            "hash_scope":"whitelisted numeric response; unknown fields omitted"})
                        with stats_lock:
                            diagnostic_paths[h] = str(diagnostic.relative_to(run))
                            stats["diagnostics"] += 1
                            for k in failed_response_usage:
                                failed_response_usage[k] += numeric["usage"][k]
                    raise
                write_json(dest, parsed)
            return [{**labels, **parsed, "request_hash":h,"cached":cached,"mock":bool(mock),
                     "coalesced":len(group["labels"]) > 1} for labels in group["labels"]]
        rows, errors = [], []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            tasks = {pool.submit(one,h,group):(h,group) for h,group in groups.items()}
            for future in as_completed(tasks):
                try:
                    rows.extend(future.result())
                except (RemixError, JevError, OSError, ValueError, KeyError, TypeError) as exc:
                    # Error bodies are never persisted; service errors are already sanitized.
                    message = str(exc) if isinstance(exc, (RemixError, JevError)) else "Invalid cache or local scoring failure"
                    h,group = tasks[future]
                    errors.extend({**labels,"error":message,
                                   **({"diagnostic":diagnostic_paths[h]} if h in diagnostic_paths else {})}
                                  for labels in group["labels"])
        stage_accounting.append(stats)
        return sorted(rows, key=lambda x:(x["slot"], x.get("prev_shot_id", ""), x["shot_id"])), errors
    slot_rows, errors = run_jobs(slot_jobs)
    write_jsonl(run / "slot-scores.jsonl", slot_rows)
    pair_jobs = []
    ranking = {}
    for slot in slots["items"]:
        rows = [r for r in slot_rows if r["slot"] == slot["id"]]
        ranking[slot["id"]] = sorted(rows, key=lambda r:(-fused(r), r["shot_id"]))[:pair_k]
    for a, b in zip(slots["items"], slots["items"][1:]):
        for prev in ranking[a["id"]]:
            for current in ranking[b["id"]]:
                sid, pid = current["shot_id"], prev["shot_id"]
                payload = request_for("pair", b, by_id[sid], cards[sid], shots, intent,
                    slots["fps"], model, library, previous=by_id[pid], previous_card=cards[pid])
                pair_jobs.append(({"kind":"pair", "slot":b["id"], "shot_id":sid, "prev_shot_id":pid}, payload))
    pair_rows, pair_errors = run_jobs(pair_jobs)
    errors += pair_errors
    write_jsonl(run / "pair-scores.jsonl", pair_rows)
    new_responses = {r["request_hash"]:r for r in slot_rows+pair_rows if not r["cached"]}
    accounting = {k:sum(stage[k] for stage in stage_accounting) for k in stage_accounting[0]}
    accounting["invocation_semantics"] = "call_api invocations; internal HTTP transport retries are not counted"
    meta = {"version":1, "status":"partial" if errors else "complete", "mock":bool(mock),
        "model":model, "completed":len(slot_rows)+len(pair_rows), "request_count":len(slot_jobs)+len(pair_jobs),
        "errors":errors, "dependencies":snapshot(run, DEPENDENCIES),
        "score_files":snapshot(run,["slot-scores.jsonl","pair-scores.jsonl"]),
        "question_library_hash":file_hash(ROOT / "questions" / "remix.yaml"),
        "thresholds_hash":file_hash(ROOT / "thresholds.toml"),
        "request_accounting":accounting,"failed_response_usage":failed_response_usage,
        "new_usage":{k:sum(r["usage"].get(k,0) for r in new_responses.values())
                     for k in ("input_tokens", "output_tokens")}}
    write_json(run / "score-meta.json", meta)
    if errors:
        raise RemixError("Partial Jev scoring failure; retry reuses successful caches", 2, {"errors":errors})
    return meta


def fused(row):
    a, cfg = row["answers"], thresholds()["cast"]
    return (a["slot_fit"]["value"]*cfg["slot_fit_weight"] + a["mood_match"]["value"]*cfg["mood_weight"]
            + a["focus_on_required_char"]["value"]*cfg["character_weight"])


def pair_value(row):
    if not row:
        return 0.0
    a = row["answers"]
    return (a["smoothness"]["value"]+a["meaningful_link"]["value"])/2
