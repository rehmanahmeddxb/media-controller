#!/usr/bin/env python3
"""One-shot capability report (P1-37): FFmpeg/FFprobe, encoders, codecs,
storage writability, RAM/disk, camera hints. Run standalone:

    python3 scripts/diagnostics.py
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_config            # noqa: E402
from app.core.storage import get_storage          # noqa: E402
from app.media.ffmpeg import get_ffmpeg           # noqa: E402
from app.media.ffprobe import get_ffprobe         # noqa: E402

OK, BAD, WARN = "✅", "❌", "⚠️ "


def main() -> int:
    print("─" * 62)
    print(" Ahmed Reaction Studio — diagnostics")
    print("─" * 62)
    cfg = get_config()
    storage = get_storage()
    termux = "com.termux" in os.environ.get("PREFIX", "")

    print(f" {OK} Python        {sys.version.split()[0]} ({platform.system()} {platform.machine()})"
          + ("  [Termux]" if termux else ""))
    if tuple(int(x) for x in sys.version.split()[0].split(".")[:2]) < (3, 11):
        print(f" {WARN} Python 3.11+ recommended")

    ram = "?"
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal"):
                    ram = f"{int(line.split()[1]) / 1e6:.1f} GB"
                    break
    except OSError:
        pass
    print(f" {OK} CPU / RAM     {os.cpu_count()} cores / {ram}")

    # FFmpeg ------------------------------------------------------------
    runner = get_ffmpeg()
    caps = None
    if runner.ffmpeg_path:
        print(f" {OK} FFmpeg        {runner.ffmpeg_path}")
        import asyncio
        caps = asyncio.run(runner.detect())
        print(f"    version: {caps.get('version')}  encoders: {len(caps.get('encoder_list') or [])}")
        enc_list = caps.get("encoder_list") or []
        for name, checks in (("libx264 (MP4/MKV/MOV)", ["libx264"]),
                             ("aac (audio)", ["aac", "libfdk_aac"]),
                             ("libvpx-vp9 (WebM)", ["libvpx-vp9"]),
                             ("libopus", ["libopus"])):
            if checks:
                present = any(c in enc_list for c in checks)
                print(f"    {'✅' if present else '⚠️ '} {name}")
        hw = caps.get("hw_encoders") or {}
        print(f"    hardware encoders: {', '.join(hw) if hw else 'none (software encoding will be used — fine)'}")
        for f in ("scale", "overlay", "amix", "concat", "tonemap"):
            missing = f not in (caps.get("filter_list") or [])
            if missing:
                print(f"    {WARN} filter '{f}' missing — export needs a fuller FFmpeg build")
    else:
        print(f" {BAD} FFmpeg        NOT FOUND")
        print("    Fix:  Windows → winget install Gyan.FFmpeg   |   Termux → pkg install ffmpeg")
        print("    Or set ARS_FFMPEG to the ffmpeg executable path.")

    # FFprobe -------------------------------------------------------------
    probe = get_ffprobe()
    if probe.ffprobe_path:
        print(f" {OK} FFprobe       {probe.ffprobe_path}")
    else:
        print(f" {BAD} FFprobe       NOT FOUND (ships with FFmpeg)")

    # storage -------------------------------------------------------------
    for name in ("projects", "proxies", "recordings", "exports", "temp", "logs"):
        root = storage.roots[name]
        ok = root.exists()
        print(f" {'✅' if ok else '❌'} storage/{name:<10} {root}")
    ok, msg = storage.check_output_dir("exports")
    print(f" {'✅' if ok else '❌'} exports writable  {msg}")
    free = storage.free_space("exports")
    print(f" {OK} disk free      {free / 1e9:.2f} GB")
    if free < 2 * 1024 ** 3:
        print(f" {WARN} less than 2 GB free — exports may be refused (by design, P9-30)")

    # cameras ---------------------------------------------------------------
    print(" ℹ️  cameras       enumerated by the browser (getUserMedia).")
    print("    Android: max 2 simultaneous camera sources (policy).")
    print("    Windows: capability-based — add as many as your hardware allows.")
    print("    Tip: open the studio on the device itself; cameras need a secure context.")

    print("─" * 62)
    healthy = bool(runner.ffmpeg_path and probe.ffprobe_path)
    print(" Result: " + ("READY — full studio functionality available." if healthy
                         else "PARTIAL — preview works; install FFmpeg/FFprobe for export."))
    print("─" * 62)
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
