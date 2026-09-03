"""FFmpeg executable resolution, safe runner and capability cache (P1-28…P1-34).

Rules honored here:
  * subprocess argument arrays only — never ``shell=True`` (GR-17)
  * no network access (GR-01)
  * self-healing: precise remediation text when FFmpeg is missing (P1-33)
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import AppConfig, get_config
from app.core.logging import get_logger

log = get_logger("ffmpeg")

REMEDIATION = (
    "FFmpeg was not found. Install it, then restart the studio:\n"
    "  Windows:  winget install Gyan.FFmpeg   (or download from https://ffmpeg.org/download.html)\n"
    "  Termux:   pkg install ffmpeg\n"
    "You can also set the ARS_FFMPEG environment variable to the full path of the ffmpeg executable."
)

_COMMON_LOCATIONS = [
    # Windows
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    # Termux / Android
    "/data/data/com.termux/files/usr/bin/ffmpeg",
    # Linux common
    "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/snap/bin/ffmpeg",
    # macOS
    "/opt/homebrew/bin/ffmpeg", "/usr/local/opt/ffmpeg/bin/ffmpeg",
]


def resolve_executable(name: str, env_var: str, extra_locations: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """Resolution order: env override -> config -> PATH -> common locations."""
    cfg = get_config()
    cfg_val = getattr(cfg.media, f"{name}_path", None)
    env_val = os.environ.get(env_var)
    candidates: List[Optional[str]] = [env_val, cfg_val, shutil.which(name), *extra_locations]
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p.resolve()), None
        which = shutil.which(cand)
        if which:
            return which, None
    return None, REMEDIATION if name == "ffmpeg" else (
        "FFprobe was not found (it ships together with FFmpeg). "
        "Install FFmpeg (Windows: winget install Gyan.FFmpeg, Termux: pkg install ffmpeg) "
        "or set ARS_FFPROBE to the ffprobe executable path."
    )


class FFmpegNotFound(RuntimeError):
    pass


class FFmpegRunner:
    """Runs ffmpeg/ffprobe with argument arrays, timeouts and cancellation."""

    def __init__(self, cfg: Optional[AppConfig] = None) -> None:
        self.cfg = cfg or get_config()
        exe, missing = resolve_executable("ffmpeg", "ARS_FFMPEG", _COMMON_LOCATIONS)
        self.ffmpeg_path = exe
        self.ffmpeg_missing_msg = missing
        self._caps: Optional[Dict[str, Any]] = None
        self._version: Optional[str] = None

    # ------------------------------------------------------------------ run
    async def run(self, args: List[str], timeout: float = 600.0,
                  cancel_event: Optional[asyncio.Event] = None) -> Tuple[int, str, str]:
        """Run ffmpeg with an argument array. Returns (rc, stdout, stderr)."""
        if not self.ffmpeg_path:
            raise FFmpegNotFound(self.ffmpeg_missing_msg or REMEDIATION)
        proc = await asyncio.create_subprocess_exec(
            self.ffmpeg_path, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            if cancel_event is not None:
                stdout_task = asyncio.create_task(proc.stdout.read())
                stderr_task = asyncio.create_task(proc.stderr.read())
                done, pending = await asyncio.wait(
                    [stdout_task, stderr_task], timeout=0.5
                )
                while proc.returncode is None and not cancel_event.is_set():
                    await asyncio.sleep(0.2)
                if cancel_event.is_set():
                    await self._terminate(proc)
                    out = (await stdout_task).decode("utf-8", "replace") if not stdout_task.done() else stdout_task.result().decode("utf-8", "replace")
                    err = (await stderr_task).decode("utf-8", "replace") if not stderr_task.done() else stderr_task.result().decode("utf-8", "replace")
                    return -2, out, err
                stdout, stderr = await asyncio.wait_for(
                    asyncio.gather(stdout_task, stderr_task), timeout=timeout
                )
                return proc.returncode or 0, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode or 0, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")
        except asyncio.TimeoutError:
            await self._terminate(proc)
            raise

    def run_sync(self, args: List[str], timeout: float = 600.0,
                 on_line=None, cancel_check=None) -> Tuple[int, str]:
        """Blocking run for worker threads. ``on_line`` gets each stdout line
        (used with ``-progress pipe:1``). ``cancel_check`` is polled; when it
        returns True the process is terminated and (-2, output) is returned."""
        if not self.ffmpeg_path:
            raise FFmpegNotFound(self.ffmpeg_missing_msg or REMEDIATION)
        import subprocess
        proc = subprocess.Popen(
            [self.ffmpeg_path, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        collected: List[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                collected.append(line)
                if on_line is not None:
                    try:
                        on_line(line)
                    except Exception:  # noqa: BLE001 — progress parsing must never kill a render
                        pass
                if cancel_check is not None and cancel_check():
                    self._terminate_sync(proc)
                    return -2, "\n".join(collected)
                if proc.returncode is not None:
                    break
            try:
                _, err = proc.communicate(timeout=max(1.0, timeout))
            except subprocess.TimeoutExpired:
                self._terminate_sync(proc)
                raise
            return proc.returncode or 0, "\n".join(collected) + ("\n" + (err or ""))
        finally:
            if proc.poll() is None:
                self._terminate_sync(proc)

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process) -> None:
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass

    @staticmethod
    def _terminate_sync(proc: Any) -> None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()
        except ProcessLookupError:
            pass

    # ------------------------------------------------------------ detection
    async def detect(self) -> Dict[str, Any]:
        """Version + capability cache (P1-30, P1-31). Cached in storage/temp."""
        if self._caps is not None:
            return self._caps
        caps: Dict[str, Any] = {"available": bool(self.ffmpeg_path), "path": self.ffmpeg_path}
        if not self.ffmpeg_path:
            caps["remediation"] = self.ffmpeg_missing_msg
            self._caps = caps
            return caps
        rc, out, _err = await self.run(["-version"], timeout=30)
        if rc == 0:
            first = (out or "").splitlines()[0] if out else ""
            caps["version_line"] = first.strip()
            caps["version"] = (first.split("version", 1)[-1].strip().split(" ", 1)[0]
                               if "version" in first else "unknown")
        for key, flag in (
            ("encoders", ["-hide_banner", "-encoders"]),
            ("decoders", ["-hide_banner", "-decoders"]),
            ("hwaccels", ["-hide_banner", "-hwaccels"]),
            ("muxers", ["-hide_banner", "-muxers"]),
            ("filters", ["-hide_banner", "-filters"]),
            ("pix_fmts", ["-hide_banner", "-pix_fmts"]),
        ):
            rc, out, _ = await self.run(flag, timeout=60)
            caps[key] = out if rc == 0 else ""
        caps["encoder_list"] = self._parse_column(caps["encoders"])
        caps["filter_list"] = self._parse_filters(caps["filters"])
        caps["muxer_list"] = self._parse_muxers(caps["muxers"])
        caps["hwaccel_list"] = [l.strip() for l in (caps["hwaccels"] or "").splitlines()[1:] if l.strip()]
        caps["hw_encoders"] = self._detect_hw_encoders(caps["encoder_list"])
        self._caps = caps
        log.info(
            "ffmpeg detected %s at %s (%d encoders, hw: %s)",
            caps.get("version"), self.ffmpeg_path, len(caps["encoder_list"]),
            ",".join(caps["hw_encoders"].keys()),
            extra={"event": "ffmpeg_detected", "path": self.ffmpeg_path},
        )
        # persist a cache copy for diagnostics
        try:
            from app.core.storage import get_storage
            get_storage().atomic_write_json(
                get_storage().roots["temp"] / "ffmpeg_caps.json", caps
            )
        except Exception:  # noqa: BLE001 — cache write must never break boot
            pass
        return caps

    @staticmethod
    def _parse_column(text: str) -> List[str]:
        """Parse `-encoders/-decoders` style columns: ' V....D libx264  Desc'."""
        names = []
        for line in (text or "").splitlines():
            m = re.match(r"^\s+[VASFDI][.A-Z]*\s+([A-Za-z0-9_@\-]+)\s+\S", line)
            if m:
                names.append(m.group(1))
        return names

    @staticmethod
    def _parse_filters(text: str) -> List[str]:
        """Parse `-filters` lines: ' ... acopy     A->A  Desc' / ' T.C loop'."""
        names = []
        for line in (text or "").splitlines():
            m = re.match(r"^\s+[TSC.]{2,3}\s+([A-Za-z0-9_]+)\s+", line)
            if m:
                names.append(m.group(1))
        return names

    @staticmethod
    def _parse_muxers(text: str) -> List[str]:
        """Parse `-muxers` lines: '  E 3g2' / ' DE  matroska'."""
        names = []
        for line in (text or "").splitlines():
            m = re.match(r"^\s{1,3}[DE.]{2}\s+([A-Za-z0-9_+\-]+)\s+\S", line)
            if m:
                names.append(m.group(1))
        return names

    @staticmethod
    def _detect_hw_encoders(encoders: List[str]) -> Dict[str, Dict[str, Any]]:
        """Hardware encoder detection with 'available but unverified' flag (P1-31)."""
        mapping = {
            "nvenc": ["h264_nvenc", "hevc_nvenc"],
            "qsv": ["h264_qsv", "hevc_qsv"],
            "amf": ["h264_amf", "hevc_amf"],
            "vaapi": ["h264_vaapi", "hevc_vaapi"],
            "videotoolbox": ["h264_videotoolbox", "hevc_videotoolbox"],
        }
        found: Dict[str, Dict[str, Any]] = {}
        for vendor, names in mapping.items():
            present = [n for n in names if n in encoders]
            if present:
                found[vendor] = {"encoders": present, "verified": False}
        return found


_runner: Optional[FFmpegRunner] = None


def get_ffmpeg() -> FFmpegRunner:
    global _runner
    if _runner is None:
        _runner = FFmpegRunner()
    return _runner


def reset_ffmpeg() -> None:  # tests
    global _runner
    _runner = None
