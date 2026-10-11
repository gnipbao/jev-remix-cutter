"""Integer-frame slots, constrained beam casting and a conservative timeline IR."""
from __future__ import annotations

from collections import Counter
from datetime import date
from fractions import Fraction
from pathlib import Path

from jevkit.timebase import frame_to_us, round_half_up
from jevkit.edl import export as export_edl
from .common import (ROOT, RemixError, check_sources, digest, ensure_snapshot,
                     ensure_decision_hashes, file_hash, fingerprint, read_json, read_jsonl, save_artifact,
                     snapshot, thresholds, validate, write_json)


def load_pack(name):
    if not isinstance(name, str) or not name or any(x not in "abcdefghijklmnopqrstuvwxyz0123456789-" for x in name):
        raise RemixError("Pack must be a built-in identifier")
    found = [ROOT / "packs" / kind / (name+".json") for kind in ("tracks", "formats")]
    path = next((p for p in found if p.is_file()), None)
    if path is None:
        raise RemixError(f"Unknown pack: {name}", 5)
    if name == "side-by-side":
        raise RemixError("side-by-side requires a two-panel compositor; this backend is unavailable", 5)
    return read_json(path)


def _section(pack, start, total):
    return next((x for x in pack["skeleton"] if Fraction(start, total) < Fraction(str(x["until"]))), pack["skeleton"][-1])


def _check_audio(obj, name):
    if fingerprint(obj["path"]) != obj["fingerprint"]:
        raise RemixError(f"{name} source changed; rebuild its analysis and slots", 3)


_SLOT_OVERRIDE_FIELDS = {"need", "mood", "char_focus", "thread", "source_audio", "fx", "look",
                         "transition_in", "sfx", "retime", "allowed_shots", "source_range", "overlays"}


def _fixed_range(slot, fps):
    """Return a valid, rounded 1x interval, or None for an unconstrained slot."""
    if "source_range" not in slot:
        return None
    interval = slot["source_range"]
    if (not isinstance(interval, dict) or set(interval) != {"in_us", "out_us"}
            or not all(isinstance(interval[k], int) and not isinstance(interval[k], bool)
                       for k in ("in_us", "out_us"))
            or not 0 <= interval["in_us"] < interval["out_us"]):
        raise RemixError("source_range needs nonnegative integer in_us < out_us")
    wanted = frame_to_us(slot["end_f"]-slot["start_f"], fps)
    if abs(interval["out_us"]-interval["in_us"]-wanted) > 2:
        raise RemixError("source_range length must match the output slot at 1x within 2 microseconds")
    retime = slot.get("retime", {})
    if (not isinstance(retime, dict) or retime.get("speed", 1) != 1
            or isinstance(retime.get("speed", 1), bool) or retime.get("reverse", False)
            or "points" in retime or retime.get("preset", "constant") != "constant"):
        raise RemixError("source_range requires constant forward 1x retime")
    if any(isinstance(effect, dict) and effect.get("type") == "freeze" for effect in slot.get("fx", [])):
        raise RemixError("Freeze effects cannot preserve a source_range synchronization constraint")
    return interval


def _validate_slot_constraints(slot, shots, fps):
    allowed = slot.get("allowed_shots")
    if allowed is not None:
        if (not isinstance(allowed, list) or not allowed or
                not all(isinstance(sid, str) and sid in shots for sid in allowed)
                or len(set(allowed)) != len(allowed)):
            raise RemixError("allowed_shots must be a nonempty list of distinct existing shot IDs")
    interval = _fixed_range(slot, fps)
    if interval is not None:
        candidates = [shots[sid] for sid in allowed] if allowed is not None else shots.values()
        if not any(shot["start_us"] <= interval["in_us"] < interval["out_us"] <= shot["end_us"]
                   for shot in candidates):
            raise RemixError("source_range is not fully contained in an allowed ingested shot")


def validate_intent(run, file):
    run = Path(run)
    obj = validate("intent",read_json(file))
    if not obj["theme"].strip() or not obj["arc"] or not obj["hook"]:
        raise RemixError("Intent needs a nonempty theme, arc and hook")
    if isinstance(obj["arc"],list) and not all(isinstance(x,(str,dict)) and x for x in obj["arc"]):
        raise RemixError("Intent arc must contain nonempty strings or section objects")
    if obj.get("char_focus") is not None and (not isinstance(obj["char_focus"],str) or not obj["char_focus"].strip()):
        raise RemixError("intent.char_focus must be a nonempty character name")
    if "slot_overrides" in obj and not isinstance(obj["slot_overrides"],dict):
        raise RemixError("intent.slot_overrides must map slot IDs to overrides")
    if "narration" in obj and obj["narration"].get("license","unknown") not in ("own","licensed","unknown"):
        raise RemixError("Narration license must be own, licensed or unknown")
    unsupported = [k for k in ("montage","counterpoint","cross_cut","twist","fx_pack") if obj.get(k)]
    if unsupported:
        raise RemixError("Intent requests unavailable planning modules: "+", ".join(unsupported),5)
    if Path(file).resolve() != (run/"intent.json").resolve():
        save_artifact(run,"intent.json",obj)
    return {"path":str(run/"intent.json"),"valid":True,"theme":obj["theme"]}


def slots(run, pack=None, mode=None, density=None, allow_low_confidence=False):
    run = Path(run)
    brief, intent = read_json(run / "brief.json"), read_json(run / "intent.json")
    validate("intent", intent)
    pack_obj = load_pack(pack or brief["pack"])
    mode = mode or brief["mode"]
    if mode not in ("music", "narration"):
        raise RemixError("mode must be music or narration")
    unsupported = [k for k in ("montage", "counterpoint", "cross_cut", "twist", "fx_pack") if intent.get(k)]
    if unsupported:
        raise RemixError("Intent requests unavailable planning modules: "+", ".join(unsupported), 5)
    fps = brief["fps"]
    total = round_half_up(Fraction(str(brief["target_seconds"]))*fps)
    cfg = thresholds()["planning"]
    result_rows, warnings, subtitles = [], [], []
    if intent.get("threads"):
        warnings.append("Thread descriptors guide semantic fit; automatic cross-cut optimization is unavailable")
    if mode == "music":
        music = read_json(run / "music.json")
        # Only fps affects local music analysis. A changed preference, license
        # or excluded-shot list does not invalidate measured onset timestamps.
        ensure_snapshot(run, {k:v for k,v in music.get("dependencies",{}).items() if k != "brief.json"})
        _check_audio(music, "Music")
        if music["fps"] != fps:
            raise RemixError("Music fps differs from the brief; rerun music")
        if total > music["duration_frames"]:
            raise RemixError("Music is shorter than target_seconds; choose a longer track or shorten the brief")
        if music.get("low_confidence") and not allow_low_confidence:
            raise RemixError("Observed beat grid has low confidence; review it before --allow-low-confidence", 4)
        beats = music.get("beats", [])
        if music.get("low_confidence"):
            warnings.append("Low-confidence onset grid was explicitly accepted; musical beat alignment is unverified")
        warnings.extend(music.get("warnings", []))
        if "music_slots" in intent:
            rows, cursor = intent["music_slots"], 0
            if not isinstance(rows, list) or not rows:
                raise RemixError("intent.music_slots must be a nonempty list")
            for i, row in enumerate(rows, 1):
                if (not isinstance(row, dict) or set(row)-(_SLOT_OVERRIDE_FIELDS | {"start_f", "end_f", "section", "accent_f"})
                        or not all(isinstance(row.get(k), int) and not isinstance(row[k], bool) for k in ("start_f", "end_f"))
                        or row["start_f"] != cursor or not cursor < row["end_f"] <= total
                        or not isinstance(row.get("need"), str) or not row["need"].strip()):
                    raise RemixError("music_slots need contiguous positive frame ranges and nonempty need; beat anchors are measured, not supplied")
                section = _section(pack_obj, cursor, total)
                observed = any(beat["frame"] == cursor for beat in beats)
                item = {"id":f"slot{i:03d}", "mood":section.get("mood",pack_obj.get("mood","uncertain")),
                        "section":section["name"], **row, "beat_anchor_f":cursor if observed else None,
                        "accent_f":row.get("accent_f", 0 if observed else (row["end_f"]-cursor)//2),
                        "allowed_transitions":["hard_cut"]}
                if (not all(isinstance(item[k], str) and item[k].strip() for k in ("mood", "section"))
                        or not isinstance(item["accent_f"], int) or isinstance(item["accent_f"], bool)
                        or not 0 <= item["accent_f"] < row["end_f"]-cursor):
                    raise RemixError("music_slots need nonempty mood/section and an accent_f inside their slot")
                result_rows.append(item)
                cursor = row["end_f"]
            if cursor != total:
                raise RemixError("music_slots must cover target duration exactly")
            density = "explicit"
            if any(row["beat_anchor_f"] is None for row in result_rows):
                warnings.append("Explicit music slots include boundaries without measured beat anchors; review lyric and audio synchronization")
        else:
            density = density or pack_obj["density"]
            stride = {"1beat":1, "2beat":2, "1bar":4, "2bar":8}.get(density)
            if stride is None:
                raise RemixError("density must be 1beat, 2beat, 1bar or 2bar")
            boundaries = sorted({0, total, *[b["frame"] for b in beats[::stride] if 0 < b["frame"] < total]})
            if len(boundaries) <= 2:
                raise RemixError("No usable observed beat boundaries inside target duration")
            # Remove too-short intervals. Never synthesize unobserved beat positions.
            cleaned = [0]
            for frame in boundaries[1:-1]:
                if frame-cleaned[-1] >= cfg["min_slot_frames"] and total-frame >= cfg["min_slot_frames"]:
                    cleaned.append(frame)
            cleaned.append(total)
            for i, (a,b) in enumerate(zip(cleaned, cleaned[1:]), 1):
                section = _section(pack_obj, a, total)
                beat = next((x for x in beats if x["frame"] == a), {})
                result_rows.append({"id":f"slot{i:03d}", "start_f":a, "end_f":b,
                    "need":section["need"], "mood":section.get("mood",pack_obj.get("mood","uncertain")),
                    "section":section["name"], "beat_anchor_f":a if beat else None,
                    "accent_f":0 if beat else (b-a)//2, "allowed_transitions":["hard_cut"]})
    else:
        narration = intent.get("narration", {})
        if not isinstance(narration, dict) or not narration.get("path") or not narration.get("sentences"):
            raise RemixError("Narration needs intent.narration.path and timestamped sentences {start_f,end_f,text}")
        from .media import probe
        path = Path(narration["path"]).expanduser().resolve()
        audio = probe(path, video=False)
        if frame_to_us(total, fps) > audio["duration_us"]:
            raise RemixError("Narration audio is shorter than the target")
        cursor = 0
        for i,row in enumerate(narration["sentences"], 1):
            if (not isinstance(row, dict) or not all(isinstance(row.get(k),int) and not isinstance(row[k],bool) for k in ("start_f","end_f"))
                    or row["start_f"] != cursor or row["end_f"] <= cursor or row["end_f"] > total or not isinstance(row.get("text"),str)):
                raise RemixError("Narration sentence intervals must form contiguous positive frame ranges inside the target")
            cursor = row["end_f"]
            section = _section(pack_obj,row["start_f"],total)
            result_rows.append({"id":f"slot{i:03d}", "start_f":row["start_f"], "end_f":cursor,
                "need":row.get("need",row["text"]), "mood":row.get("mood",section.get("mood","uncertain")),
                "section":section["name"], "accent_f":(cursor-row["start_f"])//2,
                "allowed_transitions":["hard_cut"]})
            sub = {k:row[k] for k in ("start_f","end_f","text","words") if k in row}
            for word in sub.get("words",[]):
                if (not isinstance(word,dict) or not isinstance(word.get("text"),str)
                    or not all(isinstance(word.get(k),int) and not isinstance(word[k],bool) for k in ("start_f","end_f"))
                    or not row["start_f"] <= word["start_f"] < word["end_f"] <= cursor):
                    raise RemixError("Narration words need absolute frame intervals inside their sentence")
            subtitles.append(sub)
        if cursor != total:
            raise RemixError("Narration sentences must cover target duration exactly")
        write_json(run / "narration.json", {"version":1,"path":str(path),"fingerprint":fingerprint(path),
            "duration_us":audio["duration_us"],"authored":narration.get("authored") is True,
            "license":narration.get("license","unknown")})
    overrides_by_slot = intent.get("slot_overrides", {})
    if not isinstance(overrides_by_slot, dict) or set(overrides_by_slot)-{row["id"] for row in result_rows}:
        raise RemixError("intent.slot_overrides must name existing output slots")
    shots = {shot["shot_id"]:shot for shot in read_jsonl(run/"shots.jsonl")} if any(
        "allowed_shots" in row or "source_range" in row for row in [*result_rows, *overrides_by_slot.values()]
        if isinstance(row, dict)) else {}
    for row in result_rows:
        row["theme"] = intent["theme"]
        required = intent.get("char_focus", intent.get("required_character", ""))
        if required:
            if not isinstance(required,str):
                raise RemixError("intent.char_focus must name one evidenced character")
            row["char_focus"] = required
        overrides = overrides_by_slot.get(row["id"], {})
        if not isinstance(overrides,dict) or set(overrides)-_SLOT_OVERRIDE_FIELDS:
            raise RemixError("Unsupported slot override fields",5)
        row.update(overrides)
        _validate_slot_constraints(row, shots, fps)
        if (row["end_f"]-row["start_f"])/fps > cfg["max_slot_seconds"]:
            warnings.append(f"{row['id']} exceeds the visual-change interval; review before delivery")
    artifact = {"version":1,"fps":fps,"duration_frames":total,"pack":pack_obj["id"],"mode":mode,
        "density":density or pack_obj["density"],"items":result_rows,"subtitles":subtitles,"warnings":warnings,
        "dependencies":snapshot(run,["brief.json","intent.json","music.json","narration.json"]+
                                (["shots.jsonl","sources.json"] if shots else [])),
        "pack_hash":digest(pack_obj)}
    validate("slots",artifact)
    save_artifact(run,"slots.json",artifact)
    return {"slots":len(result_rows),"duration_frames":total,"warnings":warnings,"path":str(run/"slots.json")}


def feasible_shot(shot, card, slot, fps):
    """Conservative local hard gates; Jev supplies the semantic fit judgment."""
    if "allowed_shots" in slot and shot["shot_id"] not in slot["allowed_shots"]:
        return False
    try:
        interval = _fixed_range(slot, fps)
    except RemixError:
        return False
    if interval is not None and not shot["start_us"] <= interval["in_us"] < interval["out_us"] <= shot["end_us"]:
        return False
    duration = Fraction((slot["end_f"]-slot["start_f"])*1_000_000,fps)
    available = shot["end_us"]-shot["start_us"]
    if available < duration*Fraction(str(thresholds()["cast"]["min_speed"])):
        return False
    required = slot.get("char_focus")
    if required:
        observed = card["words"].get("人物",card["words"].get("character","uncertain"))
        if isinstance(observed,list):
            observed = " ".join(observed)
        if not isinstance(observed,str) or "uncertain" in observed.lower() or required not in observed:
            return False
    return True


def solve_in_point(shot, slot, fps):
    """Trim around a measured motion peak, bounded by actual available handles."""
    interval = _fixed_range(slot, fps)
    if interval is not None:
        if not shot["start_us"] <= interval["in_us"] < interval["out_us"] <= shot["end_us"]:
            raise RemixError("Fixed source_range is outside this shot")
        return {**interval,"speed":1.0,"motion_peak_known":False,"peak_error_frames":None}
    frames = slot["end_f"]-slot["start_f"]
    wanted = frame_to_us(frames,fps)
    source_length = min(wanted,shot["end_us"]-shot["start_us"])
    speed = source_length*fps/(frames*1_000_000)
    cfg = thresholds()["cast"]
    if not cfg["min_speed"] <= speed <= cfg["max_speed"]:
        raise RemixError("Shot cannot fit the output slot within speed limits")
    peak = shot.get("features",{}).get("action_peak_us",shot.get("features",{}).get("peak_us"))
    if not isinstance(peak,int) or not shot["start_us"] <= peak < shot["end_us"]:
        peak = (shot["start_us"]+shot["end_us"])//2
        peak_known = False
    else:
        peak_known = True
    anchor = slot.get("accent_f",frames//2)
    desired = peak-round_half_up(Fraction(source_length*anchor,frames))
    start = max(shot["start_us"],min(shot["end_us"]-source_length,desired))
    return {"in_us":start,"out_us":start+source_length,"speed":speed,
        "motion_peak_known":peak_known,
        "peak_error_frames":round_half_up(Fraction((peak-start)*frames,source_length))-anchor if peak_known else None}


def beam_search(candidates, pairs=None, beam=32, reuse_penalty=None, same_source_penalty=None,
                transition_weight=None, budgets=None):
    """Maximum-score feasible sequence; beam >= product yields exhaustive optimum.

    Each candidate has shot_id, source, fused, duration_frames, optional title.
    Title budgets are cumulative output frames shared by all mapped sources.
    """
    cfg = thresholds()["cast"]
    reuse_penalty = cfg["reuse_penalty"] if reuse_penalty is None else reuse_penalty
    same_source_penalty = cfg["same_source_penalty"] if same_source_penalty is None else same_source_penalty
    transition_weight = cfg["transition_weight"] if transition_weight is None else transition_weight
    pairs, budgets = pairs or {}, budgets or {}
    states = [(0.0,[],Counter(),Counter())]
    for options in candidates:
        next_states = []
        for total,path,uses,title_frames in states:
            for candidate in options:
                title = candidate.get("title")
                length = candidate["duration_frames"]
                if title in budgets and title_frames[title]+length > budgets[title]:
                    continue
                score = total+candidate["fused"]-reuse_penalty*uses[candidate["shot_id"]]
                if path:
                    prev = path[-1]
                    score -= same_source_penalty*(prev["source"] == candidate["source"])
                    score += transition_weight*pairs.get((candidate["slot"],prev["shot_id"],candidate["shot_id"]),0)
                next_uses,title_next = uses.copy(),title_frames.copy()
                next_uses[candidate["shot_id"]] += 1
                if title:
                    title_next[title] += length
                next_states.append((score,path+[candidate],next_uses,title_next))
        states = sorted(next_states,key=lambda x:(-x[0],tuple(c["shot_id"] for c in x[1])))[:beam]
        if not states:
            return None
    return {"score":states[0][0],"items":states[0][1]} if states else None


def cast(run, beam=None, allow_mock=True):
    from .judging import fused, pair_value
    from .originality import catalog_limits
    run = Path(run)
    check_sources(run)
    slot_obj = read_json(run/"slots.json")
    ensure_snapshot(run,slot_obj.get("dependencies",{}))
    meta = read_json(run/"score-meta.json")
    ensure_snapshot(run,meta.get("dependencies",{}))
    ensure_snapshot(run,meta.get("score_files",{}))
    if meta.get("status") != "complete":
        raise RemixError("Scoring is partial; finish score before cast")
    if meta.get("question_library_hash") != file_hash(ROOT/"questions"/"remix.yaml"):
        raise RemixError("Question library changed; rerun score",3)
    if meta.get("thresholds_hash") and meta["thresholds_hash"] != file_hash(ROOT/"thresholds.toml"):
        raise RemixError("Thresholds changed; rerun score",3)
    if meta.get("mock") and not allow_mock:
        raise RemixError("Mock judgments are testing only",4)
    shots = {x["shot_id"]:x for x in read_jsonl(run/"shots.jsonl")}
    cards = {x["shot_id"]:x for x in read_jsonl(run/"cards.jsonl")}
    judgments = read_jsonl(run/"slot-scores.jsonl")
    pairs = read_jsonl(run/"pair-scores.jsonl")
    cfg = thresholds()["cast"]
    beam = beam or cfg["beam"]
    if not isinstance(beam,int) or isinstance(beam,bool) or not 1 <= beam <= 10000:
        raise RemixError("beam must be an integer from 1 to 10000")
    locks = read_json(run/"locks.json") if (run/"locks.json").is_file() else {"items":[]}
    ensure_snapshot(run,locks.get("dependencies",{}))
    locked = {x["slot"]:x for x in locks["items"]}
    if set(locked)-{x["id"] for x in slot_obj["items"]}:
        raise RemixError("Locked slot no longer exists; release stale locks",3)
    brief,intent = read_json(run/"brief.json"),read_json(run/"intent.json")
    excluded = brief.get("feedback",{}).get("exclude",[])
    if not isinstance(excluded,list) or not all(isinstance(sid,str) for sid in excluded):
        raise RemixError("brief.feedback.exclude must be an array of shot IDs")
    excluded = set(excluded)
    format_name = intent.get("format",brief.get("format","remix"))
    title_for, budgets, unavailable = {}, {}, set()
    for entry in catalog_limits(run):
        for sid in entry["source_ids"]:
            title_for[sid] = entry["title"]
        if entry["max_seconds_total"] is not None:
            budgets[entry["title"]] = Fraction(str(entry["max_seconds_total"]))*slot_obj["fps"]
        if ((entry["expires"] and date.today()>date.fromisoformat(entry["expires"])) or
            (entry["allowed_formats"] is not None and format_name not in entry["allowed_formats"])):
            unavailable.update(entry["source_ids"])
    candidates,alternatives_by_slot,gaps = [],{},[]
    for slot in slot_obj["items"]:
        choices = []
        for row in judgments:
            if (row["shot_id"] in excluded or row["slot"] != slot["id"] or row["answers"]["slot_fit"]["value"]+1e-9 < thresholds()["judge"]["min_slot_fit"]
                    or row["answers"]["excluded"]["value"]>thresholds()["judge"]["max_excluded"]):
                continue
            shot = shots.get(row["shot_id"])
            if shot is None or shot["source"] in unavailable or shot["shot_id"] not in cards or not feasible_shot(shot,cards[shot["shot_id"]],slot,slot_obj["fps"]):
                continue
            timing = solve_in_point(shot,slot,slot_obj["fps"])
            if slot["id"] in locked and row["shot_id"] == locked[slot["id"]]["shot_id"]:
                old = locked[slot["id"]]
                if not shot["start_us"] <= old["in_us"] < old["out_us"] <= shot["end_us"]:
                    raise RemixError("Locked source interval is outside its shot",3)
                if "source_range" in slot and any(old[k] != slot["source_range"][k] for k in ("in_us","out_us")):
                    raise RemixError("Locked source interval conflicts with the slot source_range; release the stale lock",3)
                timing.update({"in_us":old["in_us"],"out_us":old["out_us"],
                    "speed":1.0 if "source_range" in slot else
                            (old["out_us"]-old["in_us"])*slot_obj["fps"]/(1e6*(slot["end_f"]-slot["start_f"]))})
                if not cfg["min_speed"] <= timing["speed"] <= cfg["max_speed"]:
                    raise RemixError("Locked range cannot fit this slot within speed limits",3)
            choices.append({"slot":slot["id"],"shot_id":shot["shot_id"],"source":shot["source"],
                **timing,"out":{"start_f":slot["start_f"],"end_f":slot["end_f"]},
                "duration_frames":slot["end_f"]-slot["start_f"],"fused":fused(row),"judgments":row["answers"],
                "locked":slot["id"] in locked,"title":title_for.get(shot["source"]),
                "reasons":["Jev slot fit, mood and character judgments" if not meta["mock"] else "MOCK judgments: testing only",
                           "Declared source range preserved at constant forward 1x" if "source_range" in slot else
                           "Measured motion-peak anchor constrained by available source handles"]})
        choices.sort(key=lambda x:(-x["fused"],x["shot_id"]))
        alternatives_by_slot[slot["id"]] = choices
        if slot["id"] in locked:
            choices = [x for x in choices if x["shot_id"] == locked[slot["id"]]["shot_id"]]
        candidates.append(choices)
        if not choices:
            gaps.append({"slot":slot["id"],"need":slot["need"],"reason":"No scored candidate satisfies hard fit, character, speed, lock and authorization constraints",
                "actions":["Add supported source evidence", "Review intent or release the slot lock", "Rerun slots and score"]})
    pair_map = {(r["slot"],r["prev_shot_id"],r["shot_id"]):pair_value(r) for r in pairs}
    selected = beam_search(candidates,pair_map,beam=beam,budgets=budgets) if not gaps else None
    if selected is None and not gaps:
        gaps.append({"slot":"sequence","reason":"No sequence fits shared authorization budgets within the search beam",
                     "actions":["Add other authorized sources", "Increase beam", "Shorten the target duration"]})
    write_json(run/"gaps.json",{"version":1,"items":gaps,"filled":False})
    if selected is None:
        raise RemixError("Casting has unresolved gaps; inspect gaps.json",2,{"gaps":gaps})
    for i,row in enumerate(selected["items"]):
        row["alternatives"] = [{k:c[k] for k in ("shot_id","source","in_us","out_us","speed","fused","judgments","reasons")}
                                for c in alternatives_by_slot[row["slot"]] if c["shot_id"] != row["shot_id"]]
        if i:
            key = (row["slot"],selected["items"][i-1]["shot_id"],row["shot_id"])
            row["reasons"].append("Jev pair smoothness and meaningful-link score" if key in pair_map else "Adjacent pair was not evaluated; no transition bonus applied")
            row["pair_score"] = pair_map.get(key)
    artifact = {"version":1,"fps":slot_obj["fps"],"items":selected["items"],"sequence_score":selected["score"],"beam":beam,
        "mock":bool(meta["mock"]),"dependencies":snapshot(run,["brief.json","intent.json","slots.json","music.json","narration.json",
            "sources.json","shots.jsonl","cards.jsonl","slot-scores.jsonl","pair-scores.jsonl","score-meta.json","locks.json","licenses/catalog.json"])}
    validate("cast",artifact)
    save_artifact(run,"cast.json",artifact)
    return {"slots":len(artifact["items"]),"sequence_score":selected["score"],"mock":artifact["mock"],"path":str(run/"cast.json")}


def assemble(run):
    run = Path(run)
    sources = check_sources(run)
    brief,slot_obj,selected = read_json(run/"brief.json"),read_json(run/"slots.json"),read_json(run/"cast.json")
    ensure_snapshot(run,slot_obj.get("dependencies",{}))
    ensure_snapshot(run,selected.get("dependencies",{}))
    meta = read_json(run / "score-meta.json")
    expected = {"questions":meta.get("question_library_hash"),"thresholds":meta.get("thresholds_hash")}
    ensure_decision_hashes({"decision_hashes":expected})
    pack = load_pack(slot_obj["pack"])
    if digest(pack) != slot_obj.get("pack_hash"):
        raise RemixError("Pack changed; rebuild slots",3)
    expected.update(pack_id=slot_obj["pack"],pack=digest(pack))
    by_slot = {x["id"]:x for x in slot_obj["items"]}
    items = []
    for row in selected["items"]:
        slot = by_slot[row["slot"]]
        n = slot["end_f"]-slot["start_f"]
        item = {"slot":row["slot"],"shot_id":row["shot_id"],
            "src":{"source":row["source"],"in_us":row["in_us"],"out_us":row["out_us"]},
            "out":dict(row["out"]),"retime":{"speed":row["speed"],"preset":"constant","interp":"nearest"},
            "transition_in":{"type":"hard_cut","frames":0},"look":{"preset":"none"},
            "fx":[],"sfx":[],"reasons":row["reasons"]}
        if pack.get("transform"):
            transform = pack["transform"]
            if transform["type"] != "ken_burns":
                raise RemixError("Unsupported pack transform",5)
            item["transform"] = {"keys":[{"f":0,"scale":transform["scale_start"]},{"f":n-1,"scale":transform["scale_end"]}]} if n>1 else {"keys":[{"f":0,"scale":transform["scale_start"]}]}
        for key in ("retime","transition_in","look","fx","sfx","source_audio","overlays"):
            if key in slot:
                item[key] = slot[key]
        if "source_audio" not in item and brief.get("preserve_source_audio"):
            item["source_audio"] = True
        if slot.get("thread"):
            item["thread"] = slot["thread"]
        items.append(item)
    intent = read_json(run/"intent.json")
    if slot_obj["mode"] == "music":
        m = read_json(run/"music.json")
        _check_audio(m,"Music")
        audio = {"path":m["path"],"fingerprint":m["fingerprint"],"start_us":0,"gain":1.0,
            "duration_us":m["duration_us"],"duck_music":True,"license":brief.get("music_license","unknown"),
            "replaces_source_audio":not any(x.get("source_audio") for x in items)}
        if brief.get("music_from_source_audio") is True:
            if any(x.get("source_audio") for x in items):
                raise RemixError("Source-derived music already contains the source audio; disable per-shot source_audio to avoid duplicate mixing")
            from .originality import _original_speed
            audio["replaces_source_audio"] = False
            # The supplied file is the sole rendered audio track. These source
            # mappings audit the declared synchronous edit, never mix it twice.
            audio["items"] = [{**item["src"], **item["out"], "source_audio":True,
                               "unaltered":_original_speed(item, Fraction(slot_obj["fps"]))}
                              for item in items]
    else:
        m = read_json(run/"narration.json")
        _check_audio(m,"Narration")
        audio = {"duck_music":False,"replaces_source_audio":not any(x.get("source_audio") for x in items),
            "narration":{"path":m["path"],"fingerprint":m["fingerprint"],"start_f":0,
                "end_f":slot_obj["duration_frames"],"gain":1.0,"authored":m["authored"],
                "authored_by":"user" if m["authored"] else "unknown","license":m["license"]}}
    timeline = {"version":2,"fps":slot_obj["fps"],"width":brief["width"],"height":brief["height"],
        "duration_frames":slot_obj["duration_frames"],"items":items,"audio":audio,
        "subtitles":slot_obj.get("subtitles",[]),"mock":selected["mock"],"pack":slot_obj["pack"],
        "mode":slot_obj["mode"],"intent":{"theme":intent["theme"],"arc":intent["arc"]},"decision_hashes":expected,
        "dependencies":snapshot(run,["brief.json","sources.json","shots.jsonl","cards.jsonl","intent.json","slots.json",
            "music.json","narration.json","cast.json","score-meta.json","slot-scores.jsonl","pair-scores.jsonl","locks.json","licenses/catalog.json"])}
    from .rendering import _validate_timeline
    _validate_timeline(timeline,sources)
    from .originality import check_catalog
    check_catalog(run,timeline)
    validate("timeline",timeline)
    save_artifact(run,"timeline.json",timeline)
    write_json(run/"edl.json",export_edl(timeline,sources))
    return {"duration_frames":timeline["duration_frames"],"items":len(items),"mock":timeline["mock"],"path":str(run/"timeline.json")}


def lock(run, slots, locked=True):
    run = Path(run)
    selected = read_json(run/"cast.json")
    ensure_snapshot(run,selected.get("dependencies",{}))
    if isinstance(slots,str):
        slots = [slots]
    targets = set(slots)
    rows = {r["slot"]:r for r in selected["items"]}
    if targets-set(rows):
        raise RemixError("Unknown slot to lock")
    old = read_json(run/"locks.json") if (run/"locks.json").is_file() else {"items":[]}
    locks = {r["slot"]:r for r in old["items"]}
    for sid in targets:
        if locked:
            locks[sid] = {k:rows[sid][k] for k in ("slot","shot_id","in_us","out_us")}
        else:
            locks.pop(sid,None)
        rows[sid]["locked"] = bool(locked)
    save_artifact(run,"locks.json",{"version":1,"items":[locks[sid] for sid in sorted(locks)],
        "dependencies":snapshot(run,["slots.json","shots.jsonl","sources.json"])})
    selected["dependencies"]["locks.json"] = file_hash(run/"locks.json")
    save_artifact(run,"cast.json",selected)
    return {"slots":sorted(targets),"locked":bool(locked)}


def swap(run, slot, alternative=1, shot_id=None):
    run = Path(run)
    selected = read_json(run/"cast.json")
    ensure_snapshot(run,selected.get("dependencies",{}))
    row = next((x for x in selected["items"] if x["slot"] == slot),None)
    if row is None:
        raise RemixError("Unknown slot")
    if row.get("locked"):
        raise RemixError("Slot is locked; release it before swapping")
    options = row.get("alternatives",[])
    if shot_id:
        candidate = next((x for x in options if x["shot_id"] == shot_id),None)
    else:
        candidate = options[alternative-1] if isinstance(alternative,int) and 1<=alternative<=len(options) else None
    if candidate is None:
        raise RemixError("Requested scored alternative does not exist")
    replacement = dict(row)
    replacement.update(candidate)
    replacement["alternatives"] = [{k:row[k] for k in ("shot_id","source","in_us","out_us","speed","fused","judgments","reasons")}]+[x for x in options if x["shot_id"] != candidate["shot_id"]]
    # A swap may consume another title's budget; validate the complete sequence.
    rows = [replacement if x["slot"] == slot else x for x in selected["items"]]
    from .originality import check_catalog
    fps = read_json(run/"slots.json")["fps"]
    check_catalog(run,{"fps":fps,"items":[{"src":{"source":x["source"]},"out":x["out"]} for x in rows]})
    selected["items"] = rows
    selected["edited"] = True
    selected["sequence_score"] = None
    replacement["reasons"].append("User selected a scored alternative; adjacent sequence objective must be reviewed")
    from .judging import pair_value
    paired = {(r["slot"],r["prev_shot_id"],r["shot_id"]):pair_value(r)
              for r in read_jsonl(run/"pair-scores.jsonl")}
    for i,row in enumerate(rows):
        row["pair_score"] = paired.get((row["slot"],rows[i-1]["shot_id"],row["shot_id"])) if i else None
    save_artifact(run,"cast.json",selected)
    return {"slot":slot,"shot_id":replacement["shot_id"],"reassemble_required":True}


def explain(run, slot=None):
    run = Path(run)
    selected = read_json(run/"cast.json")
    ensure_snapshot(run,selected.get("dependencies",{}))
    rows = [x for x in selected["items"] if slot is None or x["slot"] == slot]
    if not rows:
        raise RemixError("Unknown slot")
    return {"mock":selected["mock"],"items":[{k:r.get(k) for k in ("slot","shot_id","fused","judgments","pair_score","reasons","alternatives","locked")} for r in rows]}


def export(run, output=None):
    run = Path(run)
    timeline = read_json(run/"timeline.json")
    ensure_snapshot(run,timeline.get("dependencies",{}))
    ensure_decision_hashes(timeline)
    sources = check_sources(run)
    from .originality import check_catalog
    check_catalog(run,timeline)
    data = export_edl(timeline,sources)
    validate("edl",data)
    path = Path(output).expanduser().resolve() if output else run/"edl.json"
    write_json(path,data)
    return {"path":str(path),"version":2,"items":len(data["items"]),"mock":data["mock"]}
