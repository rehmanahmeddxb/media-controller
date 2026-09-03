"""Health & system endpoints (P1-25, P1-26)."""
from __future__ import annotations

import os
import platform
import shutil
import sys
import time
from typing import Any, Dict

from fastapi import APIRouter, Request

from app.core.config import get_config
from app.core.logging import get_logger, log_paths
from app.core.recovery import get_recovery
from app.core.storage import get_storage
from app.media.ffmpeg import get_ffmpeg
from app.media.ffprobe import get_ffprobe

router = APIRouter(tags=["health"])
log = get_logger("api.health")
_BOOT_TIME = time.time()


@router.get("/api/health")
async def health() -> Dict[str, Any]:
    cfg = get_config()
    storage = get_storage()
    recovery = get_recovery()
    rec = recovery.recover() if not health._recovery_done else {"recovered": False, "message": "checked"}
    health._recovery_done = True
    return {
        "status": "ok",
        "app": "Ahmed Reaction Studio",
        "version": "1.0.0",
        "uptime_s": round(time.time() - _BOOT_TIME, 1),
        "python": sys.version.split()[0],
        "storage_roots": {k: str(v) for k, v in storage.roots.items()},
        "disk_free_bytes": storage.free_space("exports"),
        "recovery": {k: v for k, v in rec.items() if k != "snapshot"},
        "log_paths": {k: str(v) for k, v in log_paths(cfg).items()},
        "local_only": True,  # GR-01: no external calls anywhere
    }


health._recovery_done = False


@router.get("/api/system")
async def system_info(request: Request) -> Dict[str, Any]:
    """Platform + FFmpeg/FFprobe capability report with exact remediation (P1-26, P1-E3)."""
    runner = get_ffmpeg()
    probe = get_ffprobe()
    caps = await runner.detect()
    total_mem = "unknown"
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            stat = MEMORYSTATUSEX(dwLength=ctypes.sizeof(MEMORYSTATUSEX))
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
            total_mem = f"{stat.ullTotalPhys / 1e9:.1f} GB"
        else:
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemTotal"):
                        total_mem = f"{int(line.split()[1]) / 1e6:.1f} GB"
                        break
    except Exception:  # noqa: BLE001
        pass

    storage = get_storage()
    return {
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "termux": "com.termux" in os.environ.get("PREFIX", ""),
            "python": sys.version.split()[0],
            "cpu_count": os.cpu_count(),
            "ram_total": total_mem,
        },
        "ffmpeg": {
            "available": bool(caps.get("available")),
            "path": caps.get("path"),
            "version": caps.get("version"),
            "remediation": caps.get("remediation"),
            "encoders_count": len(caps.get("encoder_list") or []),
            "hw_encoders": caps.get("hw_encoders"),
            "has_libx264": "libx264" in (caps.get("encoder_list") or []),
            "has_libvpx_vp9": "libvpx-vp9" in (caps.get("encoder_list") or []),
            "has_libx265": "libx265" in (caps.get("encoder_list") or []),
            "has_aac": any(e in (caps.get("encoder_list") or []) for e in ("aac", "libfdk_aac")),
            "has_libopus": "libopus" in (caps.get("encoder_list") or []),
            "has_scale_filter": "scale" in (caps.get("filter_list") or []),
            "has_overlay_filter": "overlay" in (caps.get("filter_list") or []),
            "has_amix_filter": "amix" in (caps.get("filter_list") or []),
        },
        "ffprobe": {
            "available": bool(probe.ffprobe_path),
            "path": probe.ffprobe_path,
            "version": await probe.version(),
            "remediation": None if probe.ffprobe_path else probe.missing_msg,
        },
        "storage": {
            "free_bytes": {k: storage.free_space(k) for k in ("projects", "proxies", "exports", "temp")},
            "writable": storage.check_output_dir("exports")[0],
        },
        "cameras_hint": (
            "Camera devices are enumerated by the browser (getUserMedia). "
            "Android is capped at 2 simultaneous sources by policy; Windows is capability-based."
        ),
    }
