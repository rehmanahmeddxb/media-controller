"""FFprobe integration — authoritative media inspection (P1-32, P2-01…P2-06).

Produces a normalized metadata dict for every source. Results are cached
keyed by file identity (path + size + mtime) to avoid repeat spawns.
Argument arrays only; no shell.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.logging import get_logger
from app.media.ffmpeg import FFmpegNotFound, FFmpegRunner, resolve_executable

log = get_logger("ffprobe")

_HDR_TRANSFERS = {"smpte2084": "hdr10", "arib-std-b67": "hlg"}


def _frac(value: str) -> float:
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rotation_from_stream(stream: Dict[str, Any]) -> int:
    tags = stream.get("tags") or {}
    side_data = stream.get("side_data_list") or []
    for sd in side_data:
        if sd.get("rotation") is not None:
            try:
                return int(float(sd["rotation"])) % 360
            except (TypeError, ValueError):
                pass
    for key in ("rotate", "rotation"):
        if key in tags:
            try:
                return int(float(tags[key])) % 360
            except (TypeError, ValueError):
                pass
    return 0


def normalize_probe(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Convert ffprobe JSON into the studio's normalized metadata object."""
    fmt = raw.get("format") or {}
    streams: List[Dict[str, Any]] = raw.get("streams") or []
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    v = video_streams[0] if video_streams else {}
    width, height = int(v.get("width") or 0), int(v.get("height") or 0)
    r_fps = _frac(v.get("r_frame_rate") or "0/1")
    avg_fps = _frac(v.get("avg_frame_rate") or "0/1")
    duration = 0.0
    try:
        duration = float(fmt.get("duration") or v.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    try:
        nb_frames = int(v.get("nb_frames") or 0)
    except (TypeError, ValueError):
        nb_frames = 0

    # VFR/CFR heuristic (P2-03): compare frame-count/duration vs r_frame_rate.
    vfr = False
    vfr_confidence = 0.0
    if duration > 0 and nb_frames > 0 and r_fps > 0:
        measured = nb_frames / duration
        delta = abs(measured - r_fps) / r_fps
        vfr = delta > 0.02
        vfr_confidence = min(1.0, delta * 5)

    pix_fmt = v.get("pix_fmt") or ""
    color_transfer = (v.get("color_transfer") or "").lower()
    hdr = _HDR_TRANSFERS.get(color_transfer, "")
    if not hdr and ("10le" in pix_fmt or "12le" in pix_fmt):
        hdr = "unknown-wider"

    rotation = _rotation_from_stream(v)
    display_w, display_h = width, height
    if rotation in (90, 270):
        display_w, display_h = display_h, display_w

    audio: List[Dict[str, Any]] = []
    for a in audio_streams:
        audio.append({
            "index": a.get("index"),
            "codec": a.get("codec_name") or "",
            "sample_rate": int(a.get("sample_rate") or 0),
            "channels": int(a.get("channels") or 0),
            "channel_layout": a.get("channel_layout") or "",
            "duration": float(a.get("duration") or fmt.get("duration") or 0.0),
            "language": ((a.get("tags") or {}).get("language") or ""),
            "bitrate": int(a.get("bit_rate") or 0),
        })

    return {
        "file": fmt.get("filename") or "",
        "container": fmt.get("format_name") or "",
        "size": int(fmt.get("size") or 0),
        "duration": duration,
        "format_bitrate": int(fmt.get("bit_rate") or 0),
        "has_video": bool(video_streams),
        "has_audio": bool(audio_streams),
        "video": {
            "codec": v.get("codec_name") or "",
            "profile": v.get("profile") or "",
            "width": width,
            "height": height,
            "display_width": display_w,
            "display_height": display_h,
            "rotation": rotation,
            "sar": v.get("sample_aspect_ratio") or "1:1",
            "dar": v.get("display_aspect_ratio") or "",
            "fps": round(r_fps, 3),
            "avg_fps": round(avg_fps, 3),
            "nb_frames": nb_frames,
            "pix_fmt": pix_fmt,
            "bitrate": int(v.get("bit_rate") or 0),
            "hdr": hdr,
            "color_transfer": color_transfer,
            "level": v.get("level"),
        },
        "audio_streams": audio,
        "vfr": vfr,
        "vfr_confidence": round(vfr_confidence, 3),
        "probed_at": __import__("time").time(),
    }


class FFProbe:
    def __init__(self, runner: Optional[FFmpegRunner] = None) -> None:
        self.runner = runner or FFmpegRunner()
        exe, missing = resolve_executable(
            "ffprobe", "ARS_FFPROBE",
            [str(Path(self.runner.ffmpeg_path or "").parent / "ffprobe"),
             r"C:\ffmpeg\bin\ffprobe.exe",
             r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
             "/data/data/com.termux/files/usr/bin/ffprobe",
             "/usr/bin/ffprobe", "/usr/local/bin/ffprobe",
             "/opt/homebrew/bin/ffprobe"],
        )
        self.ffprobe_path = exe
        self.missing_msg = missing
        self._cache: Dict[Tuple[str, int, float], Dict[str, Any]] = {}
        self._version: Optional[str] = None

    async def version(self) -> str:
        if self._version is None:
            if not self.ffprobe_path:
                self._version = "unavailable"
            else:
                proc = await asyncio.create_subprocess_exec(
                    self.ffprobe_path, "-version",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, _ = await proc.communicate()
                first = out.decode("utf-8", "replace").splitlines()
                self._version = first[0].strip() if first else "unknown"
        return self._version

    def cache_key(self, path: Path) -> Optional[Tuple[str, int, float]]:
        try:
            st = path.stat()
            return (str(path.resolve()), st.st_size, st.st_mtime)
        except OSError:
            return None

    async def probe(self, path: str | Path, use_cache: bool = True) -> Dict[str, Any]:
        """Probe a media file -> normalized metadata (P2-01, P2-02)."""
        p = Path(path)
        key = self.cache_key(p)
        if use_cache and key and key in self._cache:
            return dict(self._cache[key])

        if not self.ffprobe_path:
            raise FFmpegNotFound(
                self.missing_msg or
                "FFprobe not found. Install FFmpeg (Termux: pkg install ffmpeg; "
                "Windows: winget install Gyan.FFmpeg) or set ARS_FFPROBE."
            )
        proc = await asyncio.create_subprocess_exec(
            self.ffprobe_path,
            "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(p),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        import json
        text = out.decode("utf-8", "replace")
        if proc.returncode != 0 or not text.strip():
            raise RuntimeError(
                f"ffprobe failed on {p.name}: {err.decode('utf-8', 'replace')[:400]}"
            )
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"ffprobe returned invalid JSON for {p.name}: {exc}") from exc

        md = normalize_probe(raw)
        md["file"] = str(p)
        if key:
            self._cache[key] = dict(md)
        log.info("probed %s (%s %dx%d %.2fs)", p.name, md["video"]["codec"],
                 md["video"]["width"], md["video"]["height"], md["duration"],
                 extra={"event": "probe_ok", "path": str(p)})
        return md


_probe: Optional[FFProbe] = None


def get_ffprobe() -> FFProbe:
    global _probe
    if _probe is None:
        _probe = FFProbe()
    return _probe


def reset_ffprobe() -> None:  # tests
    global _probe
    _probe = None
