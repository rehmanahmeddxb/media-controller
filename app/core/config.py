"""Configuration loading for Ahmed Reaction Studio.

Loads ``config.json`` (falling back to ``config.example.json``), applies
``ARS_*`` environment overrides, validates via Pydantic and creates missing
storage directories on boot. Fully local — no network access anywhere.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

APP_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG_NAME = "config.json"
EXAMPLE_CONFIG_NAME = "config.example.json"


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8642


class StorageConfig(BaseModel):
    root: str = "storage"
    projects_dir: str = "projects"
    proxies_dir: str = "proxies"
    recordings_dir: str = "recordings"
    exports_dir: str = "exports"
    temp_dir: str = "temp"
    logs_dir: str = "logs"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    max_bytes: int = 5 * 1024 * 1024
    backups: int = 3


class DeviceHints(BaseModel):
    max_preview_pixels: int = 2_073_600
    max_preview_fps: int = 60


class ProxyThresholds(BaseModel):
    heavy_pixels: int = 2_300_000
    heavy_bitrate_kbps: int = 30_000
    heavy_codecs: List[str] = Field(default_factory=lambda: ["hevc", "av1", "prores", "vp9"])
    hdr_tonemap: bool = True
    vfr_normalize: bool = True


class MediaConfig(BaseModel):
    max_upload_bytes: int = 2 * 1024**3
    ffmpeg_path: Optional[str] = None
    ffprobe_path: Optional[str] = None
    probe_cache: bool = True
    proxy_ladder: List[int] = Field(default_factory=lambda: [1080, 720, 480])
    device_hints: Dict[str, DeviceHints] = Field(
        default_factory=lambda: {
            "windows": DeviceHints(max_preview_pixels=2_073_600, max_preview_fps=60),
            "termux": DeviceHints(max_preview_pixels=921_600, max_preview_fps=30),
        }
    )
    proxy_thresholds: ProxyThresholds = Field(default_factory=ProxyThresholds)


class ConstraintRung(BaseModel):
    width: int
    height: int
    frameRate: float = 30


class CamerasConfig(BaseModel):
    android_max_sources: int = 2
    windows_max_sources: int = 8
    constraint_ladder: List[ConstraintRung] = Field(
        default_factory=lambda: [
            ConstraintRung(width=1920, height=1080, frameRate=30),
            ConstraintRung(width=1280, height=720, frameRate=30),
            ConstraintRung(width=640, height=480, frameRate=30),
        ]
    )

    @field_validator("android_max_sources")
    @classmethod
    def _cap_android(cls, v: int) -> int:
        # GR-15 / NN-16: Android camera sources are hard-capped at 2.
        return min(int(v), 2)


class RecordingConfig(BaseModel):
    countdown_seconds: int = 3
    timeslice_ms: int = 1000
    codec_preference: List[str] = Field(
        default_factory=lambda: [
            "video/webm;codecs=vp9,opus",
            "video/webm;codecs=vp8,opus",
            "video/mp4;codecs=h264,aac",
            "video/webm",
        ]
    )
    video_bitrate_factor: float = 0.12
    audio_bitrate: int = 128_000
    max_take_bytes: int = 10 * 1024**3


class ExportConfig(BaseModel):
    default_format: str = "mp4"
    default_resolution: str = "1080p"
    default_fps: int = 30
    formats: List[str] = Field(default_factory=lambda: ["mp4", "webm", "mkv", "mov"])
    crf_default: Dict[str, int] = Field(default_factory=lambda: {"libx264": 19, "libx265": 24, "libvpx-vp9": 31})
    max_attempts: int = 3
    concurrency_heavy_jobs: int = 1
    space_safety_factor: float = 1.5


class AudioConfig(BaseModel):
    drift_tolerance_ms: int = 45
    target_latency_hint: float = 0.05


class RecoveryConfig(BaseModel):
    max_attempts: int = 3
    snapshot_keep: int = 5


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    cameras: CamerasConfig = Field(default_factory=CamerasConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    recovery: RecoveryConfig = Field(default_factory=RecoveryConfig)
    source_path: Optional[str] = None

    def storage_root(self) -> Path:
        root = Path(self.storage.root)
        if not root.is_absolute():
            root = APP_ROOT / root
        return root.resolve()

    def subroot(self, which: str) -> Path:
        mapping = {
            "projects": self.storage.projects_dir,
            "proxies": self.storage.proxies_dir,
            "recordings": self.storage.recordings_dir,
            "exports": self.storage.exports_dir,
            "temp": self.storage.temp_dir,
            "logs": self.storage.logs_dir,
        }
        if which not in mapping:
            raise KeyError(f"unknown storage subroot {which!r}")
        p = self.storage_root() / mapping[which]
        p.mkdir(parents=True, exist_ok=True)
        return p


def _env_overrides(data: Dict[str, Any]) -> Dict[str, Any]:
    """Apply ARS_* environment overrides (flat dotted keys)."""
    for env_name, path in {
        "ARS_HOST": ("server", "host"),
        "ARS_PORT": ("server", "port"),
        "ARS_LOG_LEVEL": ("logging", "level"),
        "ARS_FFMPEG": ("media", "ffmpeg_path"),
        "ARS_FFPROBE": ("media", "ffprobe_path"),
        "ARS_MAX_UPLOAD_BYTES": ("media", "max_upload_bytes"),
    }.items():
        val = os.environ.get(env_name)
        if val is None:
            continue
        section, key = path
        cur: Any = data.setdefault(section, {})
        if isinstance(cur, dict):
            cur[key] = int(val) if val.isdigit() else val
    return data


def load_config(path: Optional[str] = None) -> AppConfig:
    cfg_path: Optional[Path] = None
    if path:
        cfg_path = Path(path)
    elif os.environ.get("ARS_CONFIG"):
        cfg_path = Path(os.environ["ARS_CONFIG"])
    else:
        cfg_path = APP_ROOT / DEFAULT_CONFIG_NAME
        if not cfg_path.exists():
            cfg_path = APP_ROOT / EXAMPLE_CONFIG_NAME

    data: Dict[str, Any] = {}
    if cfg_path and cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON in config file {cfg_path}: {exc}") from exc

    data = _env_overrides(data)
    cfg = AppConfig.model_validate(data)
    cfg.source_path = str(cfg_path) if cfg_path else None

    # Resolve + create storage roots on boot (P1-10).
    for which in ("projects", "proxies", "recordings", "exports", "temp", "logs"):
        cfg.subroot(which)
    return cfg


_config: Optional[AppConfig] = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:  # for tests
    global _config
    _config = None
