"""Compositor / export-settings / encoder strategy tests (P9-18, P9-21/22)."""
import pytest

from app.media.compositor import (FORMAT_CODECS, FPS_OPTIONS, RenderPlanBuilder,
                                  encoder_args, pick_encoder,
                                  resolve_resolution, validate_export_settings)

CAPS = {
    "encoder_list": ["libx264", "libvpx-vp9", "aac", "libopus", "h264_nvenc"],
    "hw_encoders": {"nvenc": {"encoders": ["h264_nvenc"], "verified": False}},
}
CAPS_NO_HW = {"encoder_list": ["libx264", "libvpx-vp9"], "hw_encoders": {}}


def test_resolution_table():
    assert resolve_resolution("1080p") == (1920, 1080)
    assert resolve_resolution("720p") == (1280, 720)
    assert resolve_resolution("1080x1920") == (1080, 1920)   # vertical
    assert resolve_resolution("1080x1080") == (1080, 1080)   # square
    assert resolve_resolution("custom", "16:9", (1001, 501)) == (1000, 500)  # even-forced


@pytest.mark.parametrize("settings,ok", [
    ({"format": "mp4", "fps": 30, "resolution": "1080p"}, True),
    ({"format": "webm", "fps": 60, "resolution": "2160p"}, True),
    ({"format": "avi", "fps": 30}, False),                    # unknown format
    ({"format": "mp4", "fps": 120}, False),                   # unsupported fps
    ({"format": "webm", "video_codec": "libx264"}, False),    # h264 can't mux to webm
    ({"format": "mp4", "video_codec": "libvpx-vp9"}, False),  # vp9 not in mp4 path
    ({"format": "mp4", "resolution": "weird"}, False),
])
def test_validate_export_settings(settings, ok):
    valid, _ = validate_export_settings(settings, CAPS["encoder_list"])
    assert valid is ok


def test_codec_availability_guard():  # P9-14: only expose what exists
    valid, reason = validate_export_settings(
        {"format": "mkv", "video_codec": "libx265", "fps": 30},
        ["libx264"])  # local build has no libx265
    assert not valid and "not available" in reason


def test_encoder_strategy_prefers_hw_then_software():  # P9-21/22
    enc = pick_encoder("mp4", {}, CAPS)
    assert enc["encoder"] == "h264_nvenc" and enc["kind"] == "hw"
    assert enc["hw_verified"] is False  # available but unverified until first run
    enc2 = pick_encoder("mp4", {}, CAPS_NO_HW)
    assert enc2["encoder"] == "libx264" and enc2["kind"] == "software"
    enc3 = pick_encoder("webm", {}, CAPS)
    assert enc3["encoder"] == "libvpx-vp9"
    enc4 = pick_encoder("mp4", {"video_codec": "libx264"}, CAPS)  # explicit request wins
    assert enc4["encoder"] == "libx264"


def test_encoder_args():
    args = encoder_args("libx264", 30, crf=19)
    assert "-crf" in args and "19" in args and "-r" in args
    assert "-pix_fmt" in args
    hw = encoder_args("h264_nvenc", 30, bitrate=8_000_000)
    assert hw[:2] == ["-c:v", "h264_nvenc"] and "-b:v" in hw


def test_format_table_covers_plan():
    assert set(FORMAT_CODECS) == {"mp4", "webm", "mkv", "mov"}
    assert set(FPS_OPTIONS) == {24, 25, 30, 50, 60}


def test_filter_graph_never_uses_shell_strings():
    """GR-17/P8-20: plan steps carry argument arrays only."""
    builder = RenderPlanBuilder(
        work_dir=__import__("pathlib").Path("."), out_path=__import__("pathlib").Path("out.mp4"),
        width=1280, height=720, fps=30, encoder="libx264")
    piece = {
        "start": 0.0, "end": 4.0, "layer": "l1", "kind": "video", "name": "L",
        "geometry": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}, "z": 0, "fit": "contain",
        "segments": [{"kind": "play", "start": 0.0, "end": 4.0, "mediaStart": 1.0, "mediaEnd": 5.0}],
    }
    builder.piece_render_step(piece, __import__("pathlib").Path("src.mp4"))
    builder.final_composite_step(
        [{"path": "piece.mp4", "piece": piece, "entry": {"volume": [{"t": 0, "value": 1.0}]}}],
        4.0)
    for step in builder.steps:
        assert isinstance(step["args"], list)
        assert all(isinstance(a, str) for a in step["args"])
        # arrays only; each arg is one argv element — subprocess exec, no shell
        for a in step["args"]:
            assert "sh -c" not in a and "&&" not in a and "$(" not in a and "`" not in a


def test_silence_for_audioless_source():
    """P8-26: piece with no audio source still gets a full-length audio stream."""
    builder = RenderPlanBuilder(
        work_dir=__import__("pathlib").Path("."), out_path=__import__("pathlib").Path("out.mp4"),
        width=640, height=360, fps=30, encoder="libx264")
    piece = {
        "start": 0.0, "end": 3.0, "layer": "l1", "kind": "video", "name": "L",
        "geometry": {"x": 0, "y": 0, "w": 1, "h": 1}, "z": 0,
        "segments": [{"kind": "play", "start": 0.0, "end": 3.0, "mediaStart": 0.0, "mediaEnd": 3.0}],
    }
    builder.piece_render_step(piece, __import__("pathlib").Path("src.mp4"), has_audio=False)
    fc = builder.steps[0]["args"][builder.steps[0]["args"].index("-filter_complex") + 1]
    assert "anullsrc" in fc
