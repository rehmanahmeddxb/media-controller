"""Adaptive proxy system (P2-13 … P2-22).

Decision engine -> ladder Original -> 1080p -> 720p -> 480p.
Proxies are always NEW files under storage/proxies/<media_id>/ — originals
are never replaced or modified (GR-07, GR-11). Final export always uses the
original when usable (GR-12); proxies protect preview stability only.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.logging import get_logger, log_diag
from app.core.storage import StorageManager, get_storage
from app.media.ffmpeg import FFmpegRunner, get_ffmpeg
from app.media.ffprobe import FFProbe, get_ffprobe

log = get_logger("proxy")

PROXY_RUNGS = (1080, 720, 480, 360)

# Codecs the browser decodes universally well — no proxy needed for these at
# sensible resolutions.
_BROWSER_FRIENDLY = {"h264", "vp8", "vp9", "av1"}


def device_class(user_agent: str = "") -> str:
    ua = (user_agent or "").lower()
    if "android" in ua:
        return "termux"
    return "windows"


def decide_proxy(md: Dict[str, Any], device: str = "windows",
                 ladder: Optional[List[int]] = None) -> Optional[int]:
    """Decide the proxy rung for a source (P2-13, P2-14).

    Returns None when the device should handle the original directly (P2-15).
    """
    from app.core.config import get_config
    cfg = get_config()
    thresholds = cfg.media.proxy_thresholds
    hints = cfg.media.device_hints.get(device) or cfg.media.device_hints["windows"]
    ladder = ladder or cfg.media.proxy_ladder

    video = md.get("video") or {}
    pixels = int(video.get("display_width") or 0) * int(video.get("display_height") or 0)
    codec = (video.get("codec") or "").lower()
    bitrate_kbps = int(video.get("bitrate") or 0) // 1000 or (int(md.get("format_bitrate") or 0) // 1000)
    fps = float(video.get("fps") or 0)

    reasons: List[str] = []
    heavy = False

    if pixels > thresholds.heavy_pixels:
        reasons.append(f"resolution {video.get('display_width')}x{video.get('display_height')}")
        heavy = True
    if pixels > hints.max_preview_pixels:
        reasons.append(f"exceeds device preview budget ({hints.max_preview_pixels}px)")
        heavy = True
    if bitrate_kbps > thresholds.heavy_bitrate_kbps:
        reasons.append(f"bitrate {bitrate_kbps} kbps")
        heavy = True
    if codec not in _BROWSER_FRIENDLY:
        reasons.append(f"codec '{codec or 'unknown'}' not browser-friendly")
        heavy = True
    if video.get("hdr") and thresholds.hdr_tonemap:
        reasons.append(f"HDR ({video['hdr']})")
        heavy = True
    if "10le" in (video.get("pix_fmt") or "") or "12le" in (video.get("pix_fmt") or ""):
        reasons.append(f"deep pixel format {video.get('pix_fmt')}")
        heavy = True
    if md.get("vfr") and thresholds.vfr_normalize:
        reasons.append("variable frame rate")
        heavy = True
    if fps > hints.max_preview_fps:
        reasons.append(f"fps {fps} above device budget {hints.max_preview_fps}")
        heavy = True

    if not heavy:
        return None  # device handles the original (P2-15)

    # Choose the first rung whose pixel count fits the device budget.
    target = None
    for rung in ladder:
        # 16:9-ish assumption for budgeting is fine — rung height dominates
        rung_pixels = rung * int(rung * 16 / 9)
        if rung_pixels <= hints.max_preview_pixels or rung <= 480:
            target = rung
            break
    if target is None:
        target = 480
    log.info("proxy decision: %s -> %dp (%s)", md.get("file"), target, "; ".join(reasons),
             extra={"event": "proxy_decided", "path": str(md.get("file"))})
    return target


def _tonemap_chain(filters_available: List[str]) -> Optional[List[str]]:
    """HDR -> SDR tone mapping chain when zscale/tonemap exist (P2-17)."""
    have = set(filters_available or [])
    if {"zscale", "tonemap", "format"} <= have:
        return [
            "zscale=t=linear:npl=100,format=gbrp",
            "tonemap=hable:desat=0",
            "zscale=t=bt709:m=bt709:r=tv,format=yuv420p",
        ]
    return None


def build_proxy_args(src: Path, dst: Path, rung: int, md: Dict[str, Any],
                     caps: Optional[Dict[str, Any]] = None) -> List[str]:
    """FFmpeg args for proxy generation. Array only — never a shell string (GR-17)."""
    from app.core.config import get_config
    cfg = get_config()
    hints_device = "windows"
    fps_cap = cfg.media.device_hints[hints_device].max_preview_fps

    video = md.get("video") or {}
    filters: List[str] = []
    src_fps = float(video.get("fps") or 30) or 30

    # VFR -> CFR normalization (P2-16) and device fps cap
    if md.get("vfr") or src_fps > fps_cap:
        filters.append(f"fps={min(src_fps, fps_cap)}")
    filters.append(f"scale=-2:'min({rung},ih)'")

    # HDR -> SDR tone mapping (P2-17)
    if video.get("hdr"):
        chain = _tonemap_chain((caps or {}).get("filter_list") or [])
        if chain:
            filters.extend(chain)
        else:
            filters.append("format=yuv420p")

    # Rotation: ffmpeg auto-rotates by default (P2-18) — nothing to add unless
    # we must force it off; we keep autorotate ON so proxies are never sideways.

    args = [
        "-hide_banner", "-nostdin", "-y",
        "-i", str(src),
        "-vf", ",".join(filters),
    ]
    if md.get("has_audio"):
        args += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    else:
        args += ["-an"]
    args += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        # fast-start + fragmented-friendly seeking (P2-19)
        "-movflags", "+faststart",
        str(dst),
    ]
    return args


def proxy_output_path(proxy_dir: Path, rung: int) -> Path:
    return proxy_dir / f"proxy_{rung}p.mp4"


class ProxyGenerator:
    """Proxy generation with progress, cancellation and bounded retry (P2-20, P2-21)."""

    def __init__(self, runner: Optional[FFmpegRunner] = None,
                 probe: Optional[FFProbe] = None,
                 storage: Optional[StorageManager] = None) -> None:
        self.runner = runner or get_ffmpeg()
        self.probe = probe or get_ffprobe()
        self.storage = storage or get_storage()

    async def generate(self, src: Path, media_id: str, rung: int, md: Dict[str, Any],
                       device: str = "windows",
                       on_progress=None, cancel_event: Optional[asyncio.Event] = None) -> Dict[str, Any]:
        """Generate a proxy at ``rung``; on failure retry the next lower rung,
        bounded by the bottom of the ladder (GR-13)."""
        proxy_dir = self.storage.proxy_dir(media_id)
        proxy_dir.mkdir(parents=True, exist_ok=True)
        caps = await self.runner.detect()
        duration = max(float(md.get("duration") or 0), 0.001)

        try_rungs = [r for r in PROXY_RUNGS if r <= rung] or [360]
        last_err: Optional[str] = None
        for attempt_rung in try_rungs:  # bounded: at most len(ladder) attempts
            if cancel_event is not None and cancel_event.is_set():
                return {"status": "cancelled"}
            dst = proxy_output_path(proxy_dir, attempt_rung)
            args = build_proxy_args(src, dst, attempt_rung, md, caps)
            args += ["-progress", "pipe:1", "-nostats"]

            def on_line(line: str, _rung=attempt_rung) -> None:
                if on_progress and line.startswith("out_time_ms="):
                    try:
                        us = int(line.split("=", 1)[1] or 0)
                    except ValueError:
                        return
                    on_progress({
                        "rung": _rung,
                        "seconds": us / 1_000_000,
                        "pct": min(100.0, 100.0 * (us / 1_000_000) / duration),
                    })

            loop = asyncio.get_running_loop()
            rc, out = await loop.run_in_executor(
                None,
                lambda: self.runner.run_sync(args, timeout=1800, on_line=on_line,
                                             cancel_check=(lambda: cancel_event.is_set())
                                             if cancel_event else None),
            )
            if rc == -2:
                dst.unlink(missing_ok=True)
                return {"status": "cancelled"}
            if rc == 0 and dst.exists() and dst.stat().st_size > 0:
                pmd = await self.probe.probe(dst)
                result = {
                    "status": "ok",
                    "rung": attempt_rung,
                    "path": str(dst),
                    "size": dst.stat().st_size,
                    "metadata": {
                        "width": pmd["video"]["width"], "height": pmd["video"]["height"],
                        "fps": pmd["video"]["fps"], "codec": pmd["video"]["codec"],
                        "duration": pmd["duration"],
                    },
                }
                log.info("proxy ready %s (%dp)", dst.name, attempt_rung,
                         extra={"event": "proxy_ok", "media_id": media_id, "path": str(dst)})
                return result
            last_err = (out or "")[-600:]
            log.warning("proxy rung %d failed rc=%s", attempt_rung, rc,
                        extra={"event": "proxy_retry", "media_id": media_id})
            dst.unlink(missing_ok=True)

        # All rungs failed — bounded failure, preview falls back to original.
        log_diag(f"Proxy generation failed for {src.name}; using the original for preview.", level="ERROR")
        return {"status": "failed", "error": last_err or "all proxy rungs failed"}
