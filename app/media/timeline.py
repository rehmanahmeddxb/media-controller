"""Timeline reconstruction from the event log (P8-01 … P8-09).

Turns the append-only event log captured during a take into, per layer:

  * ordered content segments against the master (take-relative) timeline
    - ``play``   : source plays [start..end] take-time, media [mediaStart..mediaEnd]
    - ``freeze`` : source intentionally paused — still frame at ``mediaTime``
  * visibility intervals (hidden layers are absent from output frames)
  * volume/mute automation intervals
  * geometry intervals (from geometry_change / preset events)
  * z-order intervals (layer_reorder)
  * source intervals (source_change)

The master clock is wall-clock milliseconds (performance.now()) captured at
record start — never frame numbers (GR-10). All output times are take-relative
seconds. Independent pauses freeze only their own layer (GR-09, §20).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

MEDIA_ACTIONS = {"play", "pause", "seek", "visibility_on", "visibility_off",
                 "mute", "unmute", "volume", "source_change",
                 "geometry_change", "layer_add", "layer_remove", "layer_reorder"}


def _ms_to_s(ms: float) -> float:
    return max(0.0, float(ms)) / 1000.0


def _merge_play_segments(segs: List[Dict[str, float]]) -> List[Dict[str, float]]:
    """Merge contiguous play segments (seek within tolerance joins cleanly)."""
    merged: List[Dict[str, float]] = []
    for s in segs:
        if merged:
            last = merged[-1]
            if (abs(last["end"] - s["start"]) < 0.002
                    and abs(last["mediaEnd"] - s["mediaStart"]) < 0.040):
                last["end"] = s["end"]
                last["mediaEnd"] = s["mediaEnd"]
                continue
        merged.append(dict(s))
    return merged


def _merge_freeze_segments(segs: List[Dict[str, float]]) -> List[Dict[str, float]]:
    merged: List[Dict[str, float]] = []
    for s in segs:
        if merged and merged[-1]["mediaTime"] == s["mediaTime"] and merged[-1]["end"] == s["start"]:
            merged[-1]["end"] = s["end"]
            continue
        merged.append(dict(s))
    return merged


class _LayerState:
    __slots__ = ("active", "playing", "media_pos", "visible", "muted", "volume",
                 "geometry", "z", "source", "kind", "name")

    def __init__(self, layer: Dict[str, Any]) -> None:
        self.kind = layer.get("type", "video")
        self.name = layer.get("name", layer.get("id", "?"))
        self.active = True
        state = layer.get("state") or {}
        self.playing = bool(state.get("playing", self.kind in ("camera", "image")))
        self.media_pos = float(state.get("mediaTime", 0.0) or 0.0)
        self.visible = bool(layer.get("visible", True))
        self.muted = bool(layer.get("muted", False))
        self.volume = float(layer.get("volume", 1.0))
        self.geometry = dict(layer.get("geometry") or {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0})
        self.z = int(layer.get("z", 0))
        self.source = layer.get("source") or layer.get("mediaId") or layer.get("path")


def reconstruct_take(events: List[Dict[str, Any]],
                     layers: List[Dict[str, Any]],
                     take_start_ms: float,
                     take_end_ms: float) -> Dict[str, Dict[str, Any]]:
    """Reconstruct per-layer take timelines (P8-01 … P8-07).

    ``events`` must be the raw log (any order — sorted here by wallMs).
    ``layers`` is the layer snapshot at record start (initial state).
    Returns ``{layerId: {segments, visibility, volume, geometry, zorder, source, kind, name}}``.
    """
    duration_s = max(0.0, _ms_to_s(take_end_ms - take_start_ms))
    by_id = {l.get("id"): _LayerState(l) for l in layers}
    # layers created during the take start inactive
    added_at: Dict[str, float] = {}
    removed_at: Dict[str, float] = {}

    ordered = sorted(
        [e for e in events if e.get("action") in MEDIA_ACTIONS and e.get("layerId") in by_id],
        key=lambda e: float(e.get("wallMs", 0)),
    )
    # events before take start define initial conditions
    pre = [e for e in ordered if float(e.get("wallMs", 0)) < take_start_ms]
    live = [e for e in ordered if float(e.get("wallMs", 0)) >= take_start_ms]

    plan: Dict[str, Dict[str, Any]] = {
        lid: {
            "id": lid, "kind": st.kind, "name": st.name,
            "segments": [], "visibility": [], "volume": [],
            "geometry": [], "zorder": [], "source": st.source,
            "take_duration": duration_s,
        }
        for lid, st in by_id.items()
    }
    cursors: Dict[str, _LayerState] = dict(by_id)
    seg_open: Dict[str, Optional[Dict[str, float]]] = {lid: None for lid in by_id}

    def apply_event(st: _LayerState, ev: Dict[str, Any], t_rel: float, lid: str) -> None:
        action = ev.get("action")
        payload = ev.get("payload") or {}
        media_time = ev.get("mediaTime")
        if action == "play":
            st.playing = True
            if media_time is not None and media_time >= 0:
                st.media_pos = float(media_time)
        elif action == "pause":
            st.playing = False
            if media_time is not None and media_time >= 0:
                st.media_pos = float(media_time)
        elif action == "seek":
            if media_time is not None and media_time >= 0:
                st.media_pos = float(media_time)
        elif action in ("visibility_on", "visibility_off"):
            st.visible = action == "visibility_on"
        elif action == "mute":
            st.muted = True
        elif action == "unmute":
            st.muted = False
        elif action == "volume":
            try:
                st.volume = max(0.0, min(1.0, float(payload.get("volume", st.volume))))
            except (TypeError, ValueError):
                pass
        elif action == "geometry_change":
            geo = payload.get("geometry")
            if isinstance(geo, dict):
                st.geometry = validate_geometry(geo, st.geometry)
        elif action == "layer_add":
            st.active = True
            added_at[lid] = t_rel
        elif action == "layer_remove":
            st.active = False
            removed_at[lid] = t_rel
        elif action == "layer_reorder":
            order = payload.get("order") or []
            if isinstance(order, list) and lid in order:
                st.z = order.index(lid)
        elif action == "source_change":
            st.source = payload.get("source") or payload.get("mediaId") or st.source

    # 1. pre-take events define initial state
    for ev in pre:
        lid = ev.get("layerId")
        apply_event(cursors[lid], ev, 0.0, lid)

    # openers/closers as we sweep
    def close_segment(lid: str, t_end: float) -> None:
        seg = seg_open.get(lid)
        if seg is not None:
            seg["end"] = round(max(t_end, seg["start"]), 4)
            if seg["kind"] == "play":
                seg["mediaEnd"] = round(seg["mediaStart"] + (seg["end"] - seg["start"]), 4)
            plan[lid]["segments"].append(seg)
            seg_open[lid] = None

    def open_play(lid: str, t_start: float) -> None:
        st = cursors[lid]
        seg_open[lid] = {"kind": "play", "start": round(t_start, 4), "end": t_start,
                         "mediaStart": round(st.media_pos, 4), "mediaEnd": st.media_pos}

    def open_freeze(lid: str, t_start: float) -> None:
        st = cursors[lid]
        seg_open[lid] = {"kind": "freeze", "start": round(t_start, 4), "end": t_start,
                         "mediaTime": round(st.media_pos, 4)}

    def tick(lid: str, t_from: float, t_to: float) -> None:
        """Advance layer from t_from to t_to (take seconds), emitting segments."""
        st = cursors[lid]
        if not st.active:
            close_segment(lid, t_from)
            return
        if st.kind == "image":
            # images are a permanent freeze frame
            seg = seg_open.get(lid)
            if seg is None or seg["kind"] != "freeze":
                close_segment(lid, t_from)
                open_freeze(lid, t_from)
            return
        if st.playing:
            seg = seg_open.get(lid)
            if seg is None or seg["kind"] != "play":
                close_segment(lid, t_from)
                open_play(lid, t_from)
        else:  # paused -> freeze at current position (P8-04, P8-05)
            seg = seg_open.get(lid)
            if seg is None or seg["kind"] != "freeze":
                close_segment(lid, t_from)
                open_freeze(lid, t_from)

    # track automation breakpoints
    last_t = {lid: 0.0 for lid in by_id}

    def capture_automation(t_rel: float) -> None:
        for lid, st in cursors.items():
            entry = plan[lid]
            entry["visibility"].append({"t": round(t_rel, 4), "value": bool(st.active and st.visible)})
            entry["volume"].append({"t": round(t_rel, 4),
                                    "value": 0.0 if st.muted else st.volume})
            entry["geometry"].append({"t": round(t_rel, 4), "value": dict(st.geometry)})
            entry["zorder"].append({"t": round(t_rel, 4), "value": st.z})

    capture_automation(0.0)

    for ev in live:
        wall = float(ev.get("wallMs", take_start_ms))
        t_rel = round(_ms_to_s(wall - take_start_ms), 4)
        lid = ev.get("layerId")
        if lid not in cursors:
            continue
        for l2 in cursors:
            tick(l2, last_t[l2], t_rel)
            last_t[l2] = t_rel
        apply_event(cursors[lid], ev, t_rel, lid)
        # a seek during playback changes media position — close and reopen
        if ev.get("action") in ("play", "pause", "seek", "layer_add", "layer_remove"):
            close_segment(lid, t_rel)
        capture_automation(t_rel)

    for l2 in cursors:
        tick(l2, last_t[l2], duration_s)
        close_segment(l2, duration_s)

    # polish output
    for lid, entry in plan.items():
        segs = entry["segments"]
        plays = _merge_play_segments([s for s in segs if s["kind"] == "play"])
        freezes = _merge_freeze_segments([s for s in segs if s["kind"] == "freeze"])
        # re-interleave chronologically
        all_segs = sorted(plays + freezes, key=lambda s: (s["start"], 0 if s["kind"] == "play" else 1))
        entry["segments"] = all_segs
        entry["active_from"] = added_at.get(lid, 0.0)
        entry["removed_at"] = removed_at.get(lid)
        entry["visibility"] = _compact_booleans(entry["visibility"])
        entry["volume"] = _compact_values(entry["volume"])
        entry["geometry"] = _compact_values(entry["geometry"])
        entry["zorder"] = _compact_values(entry["zorder"])
        # total media time consumed (for preflight)
        entry["media_duration_needed"] = round(
            sum(s["mediaEnd"] - s["mediaStart"] for s in plays), 4)
    return plan


def validate_geometry(geo: Dict[str, Any], fallback: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """Reject NaN/negative and clamp to 0..1 (P3-39)."""
    out = dict(fallback or {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0})
    for key in ("x", "y", "w", "h"):
        try:
            val = float(geo.get(key, out[key]))
        except (TypeError, ValueError):
            continue
        if val != val:  # NaN
            continue
        out[key] = max(0.0, min(1.0, val))
    out["w"] = max(0.01, out["w"])
    out["h"] = max(0.01, out["h"])
    return out


def _compact_booleans(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in points:
        if not out or out[-1]["value"] != p["value"]:
            out.append(dict(p))
    return out


def _compact_values(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in points:
        if not out or out[-1]["value"] != p["value"]:
            out.append(dict(p))
    return out


def visible_during(entry: Dict[str, Any], t: float) -> bool:
    val = True
    for p in entry["visibility"]:
        if p["t"] <= t:
            val = bool(p["value"])
        else:
            break
    return val


def value_at(intervals: List[Dict[str, Any]], t: float, default: Any = None) -> Any:
    val = default
    for p in intervals:
        if p["t"] <= t:
            val = p["value"]
        else:
            break
    return val


def pieces_from_plan(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Group contiguous visible content into render pieces (P8-03).

    A piece is a maximal take interval where the layer is visible and has
    content (play or freeze). Hidden gaps split pieces — hidden periods are
    absent from output frames entirely. Segments are clipped at visibility
    transitions so a segment that straddles a hide yields only its visible
    portion.
    """
    vis_changes = [float(p["t"]) for i, p in enumerate(entry["visibility"])
                   if i == 0 or p["value"] != entry["visibility"][i - 1]["value"]]
    clipped: List[Dict[str, Any]] = []
    for seg in entry["segments"]:
        bounds = [seg["start"]] + [t for t in vis_changes if seg["start"] < t < seg["end"]] + [seg["end"]]
        for a, b in zip(bounds, bounds[1:]):
            if b - a <= 0.004:
                continue
            mid = (a + b) / 2
            if not visible_during(entry, mid):
                continue
            sub = dict(seg)
            sub["start"], sub["end"] = round(a, 4), round(b, 4)
            if sub["kind"] == "play":
                sub["mediaEnd"] = round(sub["mediaStart"] + (b - a), 4)
            clipped.append(sub)
    pieces: List[Dict[str, Any]] = []
    for seg in clipped:
        if pieces and pieces[-1]["end"] >= seg["start"] - 0.002:
            pieces[-1]["segments"].append(seg)
            pieces[-1]["end"] = max(pieces[-1]["end"], seg["end"])
        else:
            pieces.append({"start": seg["start"], "end": seg["end"], "segments": [seg]})
    for p in pieces:
        p["start"] = round(p["start"], 4)
        p["end"] = round(p["end"], 4)
    return [p for p in pieces if p["end"] - p["start"] > 0.004]


def total_take_duration(events: List[Dict[str, Any]], fallback: float = 0.0) -> float:
    if not events:
        return fallback
    wall = [float(e.get("wallMs", 0)) for e in events]
    return max(fallback, (max(wall) - min(wall)) / 1000.0)
