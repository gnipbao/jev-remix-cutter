"""Deterministic contribution accounting; thresholds are advice, never platform guarantees."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from fractions import Fraction
from pathlib import Path
import re
import tomllib

from .common import ROOT, RemixError, finite, read_json, save_artifact, snapshot


def _fps(value):
    try:
        if isinstance(value, dict):
            result = Fraction(value["num"], value["den"])
        else:
            result = Fraction(str(value))
        if result <= 0:
            raise ValueError
        return result
    except (ValueError, KeyError, ZeroDivisionError, TypeError):
        raise RemixError("Invalid timeline fps") from None


def union(intervals, total=None):
    """Half-open integer output frame intervals, sorted and merged without double counting."""
    rows = []
    for start, end in intervals:
        if any(isinstance(x, bool) or not isinstance(x, int) for x in (start, end)) or start < 0 or end < start:
            raise RemixError("Coverage intervals must use nonnegative integer frames")
        if total is not None:
            start, end = min(start, total), min(end, total)
        if end > start:
            rows.append((start, end))
    merged = []
    for start, end in sorted(rows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _length(intervals, total=None):
    return sum(end - start for start, end in union(intervals, total))


def _difference(intervals, exclusions, total):
    """Coverage left after subtracting half-open exclusion intervals."""
    excluded = union(exclusions, total)
    result = []
    for start, end in union(intervals, total):
        cursor = start
        for a, b in excluded:
            if b <= cursor:
                continue
            if a >= end:
                break
            if cursor < a:
                result.append((cursor, min(a, end)))
            cursor = max(cursor, b)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
    return result


def _out(item):
    obj = item.get("out", item)
    return obj["start_f"], obj["end_f"]


def _source_rows(run):
    obj = read_json(Path(run) / "sources.json")
    return obj["items"] if isinstance(obj, dict) else obj


def _catalog(run):
    path = Path(run) / "licenses" / "catalog.json"
    return read_json(path) if path.is_file() else {"titles": []}


def _validate_catalog(catalog, known_sources):
    if not isinstance(catalog, dict) or not isinstance(catalog.get("titles"), list):
        raise RemixError("Catalog requires a titles array")
    seen_titles, seen_sources = set(), set()
    for title in catalog["titles"]:
        name = title.get("title")
        if not isinstance(name, str) or not name.strip() or name in seen_titles:
            raise RemixError("Catalog titles must be nonempty and unique")
        seen_titles.add(name)
        ids = title.get("match", {}).get("source_ids", [])
        if not isinstance(ids, list) or not all(isinstance(s, str) for s in ids) or len(ids) != len(set(ids)):
            raise RemixError("Catalog source_ids must be a unique string array")
        if any(s not in known_sources or s in seen_sources for s in ids):
            raise RemixError("Catalog source is missing or mapped to more than one title")
        seen_sources.update(ids)
        c = title.get("constraints", {})
        if not isinstance(c, dict):
            raise RemixError("Catalog constraints must be an object")
        if "max_seconds_total" in c:
            finite(c["max_seconds_total"], "max_seconds_total", 0)
        if "expires" in c:
            try:
                date.fromisoformat(c["expires"])
            except (TypeError, ValueError):
                raise RemixError("Catalog expires must be YYYY-MM-DD") from None
        if "allowed_formats" in c and (not isinstance(c["allowed_formats"], list) or not all(isinstance(x, str) for x in c["allowed_formats"])):
            raise RemixError("Catalog allowed_formats must be an array of names")
    return catalog


def import_catalog(run, catalog_file, mappings=()):
    """Import user-declared terms; does not authenticate the terms or their publisher."""
    run = Path(run)
    sources = _source_rows(run)
    catalog = read_json(catalog_file)
    if not isinstance(catalog, dict) or not isinstance(catalog.get("titles"), list):
        raise RemixError("Catalog requires a titles array")
    by_title = {x.get("title"): x for x in catalog["titles"]}
    for mapping in mappings:
        try:
            source_id, name = mapping.split("=", 1)
            entry = by_title[name]
        except (ValueError, KeyError, AttributeError):
            raise RemixError("Mappings use source_id=exact catalog title") from None
        for row in catalog["titles"]:
            ids = row.setdefault("match", {}).setdefault("source_ids", [])
            row["match"]["source_ids"] = [x for x in ids if x != source_id]
        entry["match"]["source_ids"].append(source_id)
    _validate_catalog(catalog, {x["id"] for x in sources})
    mapped = {sid: row["title"] for row in catalog["titles"] for sid in row.get("match", {}).get("source_ids", [])}
    # No credential or consent data is copied into the run.
    save_artifact(run, "licenses/catalog.json", catalog)
    source_obj = read_json(run / "sources.json")
    for row in sources:
        if row["id"] in mapped:
            row["license"] = "licensed"
            row["catalog_title"] = mapped[row["id"]]
    if isinstance(source_obj, dict):
        source_obj["items"] = sources
    else:
        source_obj = sources
    save_artifact(run, "sources.json", source_obj)
    return catalog


def catalog_limits(run):
    """Per-title budgets (seconds); cast must share a title's budget across its sources."""
    sources = _source_rows(run)
    catalog = _validate_catalog(_catalog(run), {x["id"] for x in sources})
    return [{"title": row["title"], "source_ids": row.get("match", {}).get("source_ids", []),
             "max_seconds_total": row.get("constraints", {}).get("max_seconds_total"),
             "allowed_formats": row.get("constraints", {}).get("allowed_formats"),
             "expires": row.get("constraints", {}).get("expires")} for row in catalog["titles"]]


def check_catalog(run, timeline=None, raise_on_error=True, as_of=None):
    run = Path(run)
    timeline = timeline or read_json(run / "timeline.json")
    fps = _fps(timeline["fps"])
    brief = read_json(run / "brief.json")
    intent = read_json(run / "intent.json") if (run / "intent.json").is_file() else {}
    format_name = intent.get("format", brief.get("format", "remix"))
    today = date.fromisoformat(as_of) if as_of else date.today()
    violations = []
    for entry in catalog_limits(run):
        rows = [x for x in timeline["items"] if x["src"]["source"] in entry["source_ids"]]
        if not rows:
            continue
        frames = sum(_out(x)[1] - _out(x)[0] for x in rows)
        seconds = Fraction(frames, 1) / fps
        reasons = []
        if entry["max_seconds_total"] is not None and seconds > Fraction(str(entry["max_seconds_total"])):
            reasons.append("max_seconds_total")
        if entry["expires"] and today > date.fromisoformat(entry["expires"]):
            reasons.append("expired")
        if entry["allowed_formats"] is not None and format_name not in entry["allowed_formats"]:
            reasons.append("format_not_allowed")
        for reason in reasons:
            violations.append({"title": entry["title"], "reason": reason, "used_seconds": float(seconds),
                               "slots": [x.get("slot") for x in rows], "constraints": entry})
    if violations and raise_on_error:
        raise RemixError("Authorization catalog conditions exceeded", 4, {"catalog_violations": violations})
    return violations


def _intervals(obj, fallback, total):
    if not isinstance(obj, dict):
        return [fallback]
    rows = obj.get("intervals", [])
    if not rows:
        return [_out(obj)] if "start_f" in obj or "out" in obj else [fallback]
    return [_out(x) for x in rows]


def _tau(items):
    """Kendall tau-a over comparable shots within each source; ties are neutral."""
    groups = defaultdict(list)
    for row in items:
        groups[row["src"]["source"]].append(row["src"]["in_us"])
    score = pairs = 0
    for values in groups.values():
        for i, a in enumerate(values):
            for b in values[i + 1:]:
                pairs += 1
                score += (b > a) - (b < a)
    return score / pairs if pairs else 1.0


def originality(run, profile="default"):
    run = Path(run)
    timeline = read_json(run / "timeline.json")
    total, fps = timeline["duration_frames"], _fps(timeline["fps"])
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        raise RemixError("Originality requires a positive duration in output frames")
    items = sorted(timeline["items"], key=lambda x: _out(x))
    sources = {x["id"]: x for x in _source_rows(run)}
    if any(x["src"]["source"] not in sources for x in items):
        raise RemixError("Timeline references an unknown source")
    intent = read_json(run / "intent.json") if (run / "intent.json").is_file() else {}
    audio = timeline.get("audio", {})
    contribution, original_audio, source_audio_playing, licensed = [], [], [], []
    shares = defaultdict(list)
    for item in items:
        interval = _out(item)
        src = sources[item["src"]["source"]]
        shares[src["id"]].append(interval)
        if src.get("license") in ("own", "licensed"):
            licensed.append(interval)
        if src.get("license") == "own" and src.get("self_shot") is True:
            contribution.append(interval)
        for overlay in item.get("overlays", []):
            if (overlay.get("contribution") in ("analysis", "opinion", "original_graphic")
                    and overlay.get("authored_by") in ("user", "agent_user")
                    and (overlay.get("text") or overlay.get("path"))):
                contribution.extend(_intervals(overlay, interval, total))
        # Item flags account for original audio only when not retimed.
        if item.get("source_audio") is True:
            source_audio_playing.append(interval)
            if _original_speed(item, fps):
                original_audio.append(interval)
    if "items" in audio:
        original_audio = [_out(x) for x in audio["items"] if x.get("source_audio") is True and x.get("unaltered", True) is True]
        source_audio_playing = [_out(x) for x in audio["items"] if x.get("source_audio") is True]
    narration = audio.get("narration")
    if isinstance(narration, dict) and narration.get("authored_by") in ("user", "agent_user") and narration.get("path"):
        contribution.extend(_intervals(narration, (0, total), total))
    if audio.get("path") and (audio.get("licensed") is True or audio.get("license") in ("own", "licensed")) and audio.get("replaces_source_audio") is True:
        # A replacement declaration cannot count a region still playing raw
        # source audio as music replacing that source audio.
        contribution.extend(_difference(_intervals(audio, (0, total), total), source_audio_playing, total))
    longest = 0
    longest_where = (0, 0)
    chain = previous = None
    ordered_pairs = same_pairs = 0
    for item in items:
        start, end = _out(item)
        if previous and previous["src"]["source"] == item["src"]["source"]:
            same_pairs += 1
            gap = item["src"]["in_us"] - previous["src"]["out_us"]
            if 0 <= gap < 2_000_000:
                ordered_pairs += 1
        audio_here = any(a < end and b > start for a, b in union(original_audio, total))
        raw = audio_here and _original_speed(item, fps)
        if raw and previous and chain and previous["src"]["source"] == item["src"]["source"] and start == _out(previous)[1] and 0 <= item["src"]["in_us"] - previous["src"]["out_us"] < 500_000:
            chain = (chain[0], end)
        else:
            chain = (start, end) if raw else None
        if chain:
            # An explicit audio track can interrupt otherwise contiguous raw shots.
            for a, b in union(original_audio, total):
                intersection = max(a, chain[0]), min(b, chain[1])
                if intersection[1] - intersection[0] > longest:
                    longest = intersection[1] - intersection[0]
                    longest_where = intersection
        previous = item
    tau = _tau(items)
    narration_mode = intent.get("mode") == "narration" or timeline.get("mode") == "narration"
    new_theme = bool(intent.get("theme_is_original") is True and intent.get("theme") and intent.get("format") not in ("recap", "plot_recap", "剧情速看"))
    metrics = {"contribution_ratio": _length(contribution, total) / total,
               "longest_raw_run_s": float(Fraction(longest, 1) / fps),
               "order_preservation": ordered_pairs / same_pairs if same_pairs else 0.0,
               "source_audio_ratio": _length(original_audio, total) / total,
               "max_source_share": max((_length(rows, total) / total for rows in shares.values()), default=0.0),
               "restructure": new_theme or narration_mode or tau < 0.5,
               "license_coverage": _length(licensed, total) / total,
               "catalog_violations": check_catalog(run, timeline, raise_on_error=False)}
    if re.fullmatch(r"[a-zA-Z0-9_-]+", profile):
        path = ROOT / "packs" / "platform-profiles" / (profile + ".toml")
    else:
        path = Path(profile).expanduser()
    try:
        config = tomllib.loads(path.read_text())["originality"]
    except (OSError, ValueError, KeyError):
        raise RemixError("Cannot load originality profile") from None
    findings = []
    suggestions = {
        "contribution_ratio": ("增加自己的旁白、分析图文或自拍素材", "remix slots --mode narration"),
        "longest_raw_run_s": ("拆开原样连续段，插入对比、反应或分析镜头", "remix explain --slot " + next((str(x.get("slot", "")) for x in items if _out(x)[0] == longest_where[0]), "")),
        "order_preservation": ("按自己的主题编辑 intent.edited.json，重新组织镜头顺序", "remix intent --validate intent.edited.json"),
        "source_audio_ratio": ("提供 mute-source.patch.json（preserve_source_audio 为 false），用授权配乐替换原声，然后重新 slots → score → cast → assemble", "remix configure --patch mute-source.patch.json"),
        "max_source_share": ("增加更多来源或自有素材，并把 self-shot.mp4 替换成实际路径", "remix ingest --license own --self-shot --src self-shot.mp4"),
        "restructure": ("编辑 intent.edited.json，在意图卡中明确自己的主题和观点", "remix intent --validate intent.edited.json"),
        "license_coverage": ("确认未授权素材的许可，或换成已有授权素材", "remix license-catalog --import catalog.json"),
        "catalog_violations": ("缩短使用时长或替换超出授权条件的素材", "remix cast")}
    checks = [("contribution_ratio", metrics["contribution_ratio"] < config["contribution_min"]),
              ("longest_raw_run_s", metrics["longest_raw_run_s"] > config["longest_raw_max_s"]),
              ("order_preservation", metrics["order_preservation"] > config["order_max"]),
              ("source_audio_ratio", metrics["source_audio_ratio"] > config["source_audio_max"]),
              ("max_source_share", metrics["max_source_share"] > config["source_share_max"]),
              ("restructure", not metrics["restructure"]),
              ("license_coverage", metrics["license_coverage"] < 1.0),
              ("catalog_violations", bool(metrics["catalog_violations"]))]
    for metric, fired in checks:
        if fired:
            suggestion, action = suggestions[metric]
            finding = {"metric": metric, "level": "error" if metric == "catalog_violations" else "warn", "suggest": suggestion, "action": action}
            if metric == "longest_raw_run_s":
                finding["where"] = dict(zip(("start_f", "end_f"), longest_where))
            if metric == "license_coverage":
                finding["where"] = [{"slot": x.get("slot"), **x["out"]} for x in items if sources[x["src"]["source"]].get("license") not in ("own", "licensed")]
            if metric == "contribution_ratio":
                finding["where"] = [dict(zip(("start_f", "end_f"), x)) for x in _difference([(0,total)],contribution,total)]
            if metric == "source_audio_ratio":
                finding["where"] = [dict(zip(("start_f", "end_f"), x)) for x in union(original_audio,total)]
            if metric == "max_source_share":
                largest = max(shares, key=lambda s: _length(shares[s], total))
                finding["where"] = [{"source":largest,"slot":x.get("slot"),**x["out"]} for x in items if x["src"]["source"] == largest]
            if "where" not in finding:
                finding["where"] = {"start_f":0,"end_f":total}
            findings.append(finding)
    result = {"version": 1, "profile": profile, "metrics": metrics, "findings": findings,
              "basis": {"kendall_tau": tau, "theme_is_original": new_theme, "narration_mode": narration_mode,
                        "contribution_intervals": [dict(zip(("start_f", "end_f"), x)) for x in union(contribution, total)]},
              "notice": "阈值仅为提示，不代表平台规则，不保证通过审核；授权与新贡献声明由用户负责。",
              "snapshot": snapshot(run, ["timeline.json", "sources.json", "intent.json", "licenses/catalog.json"])}
    return save_artifact(run, "originality.json", result)


def _original_speed(item, fps=None):
    """Read the renderer's actual time map, including ramps and reverse playback."""
    retime = item.get("retime")
    if retime and not isinstance(retime, dict):
        return False
    retime = retime or {}
    if retime.get("reverse") or retime.get("mode", "constant") != "constant":
        return False
    if retime.get("speed", 1) != 1:
        return False
    points = retime.get("points", [])
    if points and (not all(isinstance(p, (list, tuple)) and len(p) == 2 for p in points)
                   or len({p[1] for p in points}) != 1):
        return False
    if fps is not None:
        start, end = _out(item)
        source_us = item["src"]["out_us"] - item["src"]["in_us"]
        # Source timestamps are integer microseconds; allow their rounding error,
        # not a whole output frame, when checking the speed of the rendered map.
        if abs(Fraction(source_us, 1) - Fraction((end - start) * 1_000_000, 1) / fps) > 1:
            return False
    return True


analyze = originality
