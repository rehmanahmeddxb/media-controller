"""Unit tests for timeline reconstruction (P8-09) — synthetic event logs."""
import pytest

from app.media.timeline import (pieces_from_plan, reconstruct_take,
                                validate_geometry, visible_during)


def make_layers():
    return [
        {"id": "main", "type": "video", "name": "Main", "visible": True, "muted": False,
         "volume": 1.0, "z": 0, "geometry": {"x": 0, "y": 0, "w": 1, "h": 1},
         "state": {"playing": True, "mediaTime": 0.0}},
        {"id": "cam", "type": "camera", "name": "Cam", "visible": True, "muted": False,
         "volume": 1.0, "z": 1, "geometry": {"x": 0.7, "y": 0.7, "w": 0.25, "h": 0.25},
         "state": {}},
        {"id": "clip", "type": "video", "name": "Clip", "visible": True, "muted": False,
         "volume": 1.0, "z": 2, "geometry": {"x": 0.05, "y": 0.05, "w": 0.3, "h": 0.3},
         "state": {"playing": True, "mediaTime": 0.0}},
    ]


T0 = 500_000
T1 = 510_000  # 10s take


def test_plain_playthrough():
    plan = reconstruct_take([], make_layers(), T0, T1)
    for lid in ("main", "cam", "clip"):
        segs = plan[lid]["segments"]
        assert len(segs) == 1 and segs[0]["kind"] == "play"
        assert segs[0]["start"] == 0.0 and segs[0]["end"] == 10.0
        assert segs[0]["mediaStart"] == 0.0 and segs[0]["mediaEnd"] == 10.0


def test_independent_pause_freezes_only_that_layer():
    events = [{"layerId": "clip", "action": "pause", "wallMs": T0 + 2000, "mediaTime": 2.0},
              {"layerId": "clip", "action": "play", "wallMs": T0 + 5000, "mediaTime": 2.0}]
    plan = reconstruct_take(events, make_layers(), T0, T1)
    clip = [s for s in plan["clip"]["segments"]]
    kinds = [s["kind"] for s in clip]
    assert kinds == ["play", "freeze", "play"]
    assert clip[1]["mediaTime"] == 2.0
    assert clip[1]["start"] == 2.0 and clip[1]["end"] == 5.0
    # other layers were NOT paused (AC-12)
    assert all(s["kind"] == "play" for s in plan["main"]["segments"])
    assert len(plan["main"]["segments"]) == 1


def test_hide_keeps_playing_state():
    events = [{"layerId": "main", "action": "visibility_off", "wallMs": T0 + 4000, "mediaTime": 4.0}]
    plan = reconstruct_take(events, make_layers(), T0, T1)
    assert visible_during(plan["main"], 2.0) is True
    assert visible_during(plan["main"], 5.0) is False
    # §10 semantics: hidden ≠ paused — media position keeps advancing
    assert len(plan["main"]["segments"]) == 1  # continuous play behind the scenes
    pieces = pieces_from_plan(plan["main"])
    assert len(pieces) == 1 and abs(pieces[0]["end"] - 4.0) < 0.01


def test_seek_jumps_media_position():
    events = [{"layerId": "main", "action": "seek", "wallMs": T0 + 6000, "mediaTime": 5.0}]
    plan = reconstruct_take(events, make_layers(), T0, T1)
    segs = plan["main"]["segments"]
    assert len(segs) == 2
    assert segs[0]["mediaEnd"] == 6.0
    assert segs[1]["mediaStart"] == 5.0  # jump (P8-02)


def test_visibility_and_geometry_automation():
    events = [
        {"layerId": "clip", "action": "geometry_change", "wallMs": T0 + 1000,
         "payload": {"geometry": {"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.4}}},
        {"layerId": "cam", "action": "volume", "wallMs": T0 + 3000, "payload": {"volume": 0.25}},
        {"layerId": "cam", "action": "mute", "wallMs": T0 + 7000},
    ]
    plan = reconstruct_take(events, make_layers(), T0, T1)
    assert plan["clip"]["geometry"][-1]["value"]["w"] == 0.4
    vols = [p["value"] for p in plan["cam"]["volume"]]
    assert vols == [1.0, 0.25, 0.0]  # volume -> mute automation (P8-21)


def test_layer_added_mid_take():
    events = [{"layerId": "clip", "action": "layer_add", "wallMs": T0 + 3000, "mediaTime": 0}]
    layers = make_layers()
    plan = reconstruct_take(events, layers, T0, T1)
    # clip existed from start in the snapshot; layer_add keeps it active
    assert plan["clip"]["segments"]


def test_geometry_validation_rejects_nan_and_clamps():
    g = validate_geometry({"x": float("nan"), "y": -0.5, "w": 2.0, "h": "abc"},
                          {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4})
    assert g["x"] == 0.1          # NaN rejected -> fallback kept
    assert g["y"] == 0.0          # negative clamped
    assert g["w"] == 1.0          # >1 clamped
    assert g["h"] == 0.4          # non-numeric rejected -> fallback kept


def test_freeze_before_first_play():
    layers = make_layers()
    layers[2]["state"] = {"playing": False, "mediaTime": 1.5}
    plan = reconstruct_take([], layers, T0, T1)
    segs = plan["clip"]["segments"]
    assert segs[0]["kind"] == "freeze" and segs[0]["mediaTime"] == 1.5
    assert segs[0]["end"] == 10.0


def test_events_before_take_start_define_initial_state():
    events = [{"layerId": "clip", "action": "pause", "wallMs": T0 - 5000, "mediaTime": 3.0}]
    plan = reconstruct_take(events, make_layers(), T0, T1)
    segs = plan["clip"]["segments"]
    assert segs[0]["kind"] == "freeze" and segs[0]["mediaTime"] == 3.0
