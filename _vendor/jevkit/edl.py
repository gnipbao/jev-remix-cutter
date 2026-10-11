"""JSON EDL v2 with the highlight v1 clip-time aliases for compatible readers.

The highlight renderer needs its own index/status and does not interpret remix
effects or multiple sources. The clips aliases are an interchange contract,
not a claim that its business-specific renderer accepts arbitrary remix EDL.
"""
def export(timeline, sources):
    source_map = {s["id"]: s for s in sources}
    items = []
    for item in timeline["items"]:
        src, out = item["src"], item["out"]
        row = {"source": src["source"], "source_path": source_map[src["source"]]["path"],
               "in_us": src["in_us"], "out_us": src["out_us"],
               "start_f": out["start_f"], "end_f": out["end_f"],
               "src": dict(src), "out": dict(out), "slot": item["slot"]}
        for field in ("retime", "transform", "transition_in", "look", "fx", "sfx", "source_audio", "reasons", "face_ops", "overlays", "thread"):
            if field in item:
                row[field] = item[field]
        items.append(row)
    clips = [{**row,"id":row["slot"],"start_us":row["in_us"],"end_us":row["out_us"],
              "frames":row["end_f"]-row["start_f"],
              "timeline_start_frame":row["start_f"],"timeline_end_frame":row["end_f"]}
             for row in items]
    return {"version": 2, "schema_version": 2, "fps": timeline["fps"],
            "total_frames": timeline["duration_frames"], "duration_frames": timeline["duration_frames"],
            "width": timeline["width"], "height": timeline["height"], "items": items,"clips":clips,
            "sources": sources, "audio": timeline.get("audio"),"subtitles":timeline.get("subtitles",[]),
            "mock": timeline.get("mock", False)}
