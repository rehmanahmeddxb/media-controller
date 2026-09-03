"""Source validation (P2-04, P2-05).

Rejects / flags unusable sources before they reach preview or a render job,
and validates probe results before any render is queued.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.logging import get_logger

log = get_logger("validators")

KNOWN_VIDEO_CODECS = {
    "h264", "hevc", "vp8", "vp9", "av1", "mpeg4", "mpeg2video", "mpeg1video",
    "theora", "prores", "dnxhd", "mjpeg", "ffv1", "h263", "wmv1", "wmv2",
    "wmv3", "wvc1", "vc1", "flv1", "rv40", "msmpeg4v3", "uncompressed",
}
KNOWN_AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis", "flac", "pcm_s16le", "ac3",
                      "eac3", "alac", "wavpack", "amr_nb", "truehd", "dts"}


class ValidationIssue:
    def __init__(self, severity: str, code: str, message: str) -> None:
        self.severity = severity  # "error" | "warning"
        self.code = code
        self.message = message

    def as_dict(self) -> Dict[str, str]:
        return {"severity": self.severity, "code": self.code, "message": self.message}


def validate_file(path: Path) -> List[ValidationIssue]:
    """File-level checks: existence, non-empty, readable (P2-04)."""
    issues: List[ValidationIssue] = []
    if not path.exists():
        issues.append(ValidationIssue("error", "missing", f"File not found: {path.name}"))
        return issues
    if not path.is_file():
        issues.append(ValidationIssue("error", "not_a_file", f"Not a regular file: {path.name}"))
        return issues
    try:
        if path.stat().st_size == 0:
            issues.append(ValidationIssue("error", "empty", f"File is empty: {path.name}"))
    except OSError as exc:
        issues.append(ValidationIssue("error", "unreadable", f"Cannot stat file: {exc}"))
    try:
        with path.open("rb") as fh:
            fh.read(16)
    except OSError as exc:
        issues.append(ValidationIssue("error", "unreadable", f"Cannot read file: {exc}"))
    return issues


def validate_probe(md: Dict[str, Any]) -> Tuple[bool, List[Dict[str, str]]]:
    """Validate normalized probe metadata (P2-04, P2-05).

    Returns (ok, issues) where ok is False only for severity=error issues.
    """
    issues: List[ValidationIssue] = []
    if not isinstance(md, dict):
        return False, [ValidationIssue("error", "invalid_metadata", "Probe metadata is not an object").as_dict()]

    if not md.get("has_video") and not md.get("has_audio"):
        issues.append(ValidationIssue("error", "no_streams", "Media contains no streams"))
    duration = float(md.get("duration") or 0)
    if duration <= 0:
        issues.append(ValidationIssue("error", "zero_duration", "Media duration is zero or unknown"))
    if int(md.get("size") or 0) <= 0:
        issues.append(ValidationIssue("error", "empty_container", "Container reports zero size"))

    video = md.get("video") or {}
    if md.get("has_video"):
        codec = (video.get("codec") or "").lower()
        if not codec:
            issues.append(ValidationIssue("error", "unknown_codec", "Video codec could not be identified"))
        elif codec not in KNOWN_VIDEO_CODECS:
            issues.append(ValidationIssue(
                "warning", "unusual_codec",
                f"Unusual video codec '{codec}' — preview or export may not be supported"))
        if not video.get("width") or not video.get("height"):
            issues.append(ValidationIssue("error", "bad_resolution", "Video resolution missing or zero"))
        if video.get("hdr"):
            issues.append(ValidationIssue(
                "warning", "hdr_source",
                f"HDR source ({video['hdr']}) — an SDR preview proxy will be generated"))
        if md.get("vfr"):
            issues.append(ValidationIssue(
                "warning", "vfr_source",
                "Variable frame rate detected — CFR normalization will be applied"))
        if video.get("rotation") not in (0, None):
            issues.append(ValidationIssue(
                "warning", "rotated_source",
                f"Source is rotated {video['rotation']}° — rotation will be applied"))

    for a in md.get("audio_streams") or []:
        codec = (a.get("codec") or "").lower()
        if codec and codec not in KNOWN_AUDIO_CODECS:
            issues.append(ValidationIssue(
                "warning", "unusual_audio_codec",
                f"Unusual audio codec '{codec}' — will be normalized at export"))

    rendered = [i.as_dict() for i in issues]
    ok = not any(i["severity"] == "error" for i in rendered)
    if not ok:
        log.warning("source rejected: %s", rendered, extra={"event": "source_rejected"})
    return ok, rendered


def validate_for_render(md: Dict[str, Any], path: Optional[Path] = None) -> Tuple[bool, List[Dict[str, str]]]:
    """Full gate before a render is queued: file + probe (P2-05)."""
    issues: List[Dict[str, str]] = []
    if path is not None:
        issues.extend(i.as_dict() for i in validate_file(path))
    ok, probe_issues = validate_probe(md)
    issues.extend(probe_issues)
    ok = ok and not any(i["severity"] == "error" for i in issues)
    return ok, issues
