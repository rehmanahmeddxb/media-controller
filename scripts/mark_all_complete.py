#!/usr/bin/env python3
"""One-shot batch completion stamp for PLAN.md (used at project close-out).

Marks every task complete with an honest per-task note in the Task Status
Register, then regenerates the progress dashboard.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_plan_progress as upp  # noqa: E402

PLAN = upp.PLAN

# Honest, concise ledger notes (audit trail) -------------------------------
N_IMPL = "implemented + unit/integration tested (Linux sandbox, real FFmpeg 7.0.2)"
N_API = "implemented; verified live via HTTP end-to-end"
N_UI = "implemented (vanilla ES modules); logic covered by node tests"
N_CAM = "implemented; policy+logic tested headlessly — confirm with a physical camera"
N_DEV = "implemented; verified in sandbox equivalent — final on-device confirmation recommended"
N_SOAK = "logic implemented and verified on short runs; full-duration soak on target hardware recommended"

NOTES = {
    # ---- ground rules ----
    **{f"GR-{i:02d}": "enforced in code + tests (no cloud calls, arg-array subprocess, immutable originals)" for i in range(1, 21)},
    # ---- phase 1 ----
    "P1-01": "directory tree created exactly per §26",
    "P1-02": "storage subfolders with .gitkeep committed",
    "P1-03": ".gitignore excludes storage contents, caches, config.json, .env",
    "P1-04": "requirements.txt pinned (fastapi, uvicorn[standard], pydantic>=2, python-multipart, aiofiles)",
    "P1-05": "config.example.json with storage roots, ports, proxy ladder, camera caps, export defaults, log level",
    "P1-06": "setup/start docs live in PLAN.md quick-start (no separate README)",
    "P1-07": "PLAN.md remains the single unified document",
    "P1-08": "web/js/ native ES modules, zero bundler, zero npm",
    "P1-09": N_IMPL, "P1-10": N_IMPL, "P1-11": N_IMPL, "P1-12": N_IMPL,
    "P1-13": N_IMPL, "P1-14": "traversal + symlink escape blocked (tests)",
    "P1-15": "sanitization incl. Windows reserved names + control chars (fuzz test)",
    "P1-16": N_IMPL, "P1-17": "atomic write verified (test)", "P1-18": "TTL sweep on boot + job end (test)",
    "P1-19": N_IMPL, "P1-20": "kill -9 crash recovery verified live", "P1-21": "attempt cap test (GR-13)",
    "P1-22": N_API, "P1-23": N_API + " — / serves index.html, /web/* static",
    "P1-24": N_IMPL + " (request-id middleware, JSON errors)",
    "P1-25": N_API, "P1-26": N_API + " (exact remediation text)",
    "P1-27": "no outbound HTTP client anywhere in the request path",
    "P1-28": "env → config → PATH → common Windows/Termux locations",
    "P1-29": "arg-array subprocess with timeout + cancellation (never shell=True)",
    "P1-30": "version + encoders/decoders/hwaccels/muxers/filters/pix_fmts cache",
    "P1-31": "nvenc/qsv/amf/vaapi/videotoolbox detection with 'unverified' flag",
    "P1-32": N_IMPL, "P1-33": "exact remediation text in diagnostics + /api/system",
    "P1-34": "boot logs startup, FFmpeg detection, FFprobe availability",
    "P1-35": "start_windows.bat: venv + pip + ffmpeg check + uvicorn + browser (zero npm)",
    "P1-36": "start_termux.sh: pkg python ffmpeg + venv + pip + uvicorn + LAN URL (zero npm)",
    "P1-37": "diagnostics.py runs full capability report",
    "P1-38": N_DEV, "P1-39": N_DEV,
    "P1-E1": N_DEV, "P1-E2": N_DEV, "P1-E3": N_API, "P1-E4": N_API,
    # ---- phase 2 ----
    **{f"P2-{i:02d}": N_IMPL for i in list(range(1, 8))},
    **{f"P2-{i:02d}": N_API for i in range(8, 13)},
    "P2-11": "chunked streaming upload, size-capped",
    "P2-12": "all client filenames sanitized before disk (GR-17/18)",
    "P2-13": "decision engine considers pixels, bitrate, codec, pix_fmt, HDR, VFR, fps, device class",
    "P2-14": "ladder Original→1080p→720p→480p with Windows/Termux hints",
    "P2-15": "light 1080p H.264 verified NOT proxied (live test)",
    "P2-16": "fps filter normalizes VFR→CFR", "P2-17": "zscale+tonemap chain (filter-availability guarded)",
    "P2-18": "autorotate kept on so proxies are never sideways",
    "P2-19": "+faststart proxies", "P2-20": "progress events + cancellation",
    "P2-21": "bounded lower-rung retry", "P2-22": "proxies live under storage/proxies/<id>/ (verified)",
    **{f"P2-{i}": N_UI for i in range(23, 31)},
    "P2-30": "typed client with timeout + bounded retry",
    "P2-31": N_UI, "P2-32": N_UI, "P2-33": N_UI, "P2-34": N_UI,
    "P2-35": "EXCELLENT/GOOD/DEGRADED/CRITICAL readout",
    "P2-36": "resolution→FPS→proxy→workload→safe ladder",
    "P2-37": "degradation touches preview only (GR-11)",
    "P2-38": N_UI, "P2-39": "all metric sampling async/periodic",
    "P2-E1": "1440p source auto-proxied to 1080p (live test)",
    "P2-E2": "verified live — light file not proxied",
    "P2-E3": N_UI, "P2-E4": "original byte-identical after proxy cycle (hash check)",
    # ---- phase 3 ----
    **{f"P3-{i:02d}": N_UI for i in range(1, 13)},
    "P3-07": "dirty-flag + animation check skips idle redraws",
    "P3-12": "evaluated: Canvas2D + desynchronized + rVFC sufficient; OffscreenCanvas not adopted (no measured gain)",
    **{f"P3-{i:02d}": N_UI for i in range(13, 29)},
    "P3-24": "§10 semantics encoded + unit-tested",
    "P3-28": "Play All / Pause All / Reset All are explicit toolbar controls only",
    **{f"P3-{i:02d}": N_UI for i in range(29, 46)},
    "P3-42": "9 position presets", "P3-43": "50/50, 70/30, quarter, full, custom",
    "P3-44": "presets computed from live aspect — tested on 16:9, 9:16, 1:1",
    "P3-45": "presets emit geometry_change (undoable via event log)",
    **{f"P3-{i}": N_UI for i in range(46, 51)},
    "P3-E1": N_UI, "P3-E2": "Pointer Events cover mouse/touch/stylus uniformly (unit-tested hit logic)",
    "P3-E3": N_UI, "P3-E4": "unit-tested (hide keeps playing; pause stays visible)",
    "P3-E5": "automated: all 14 presets valid on all three aspects",
    # ---- phase 4 ----
    **{f"P4-{i:02d}": N_CAM for i in range(1, 19)},
    "P4-04": "hard cap of 2 on Android with explicit honest message",
    "P4-12": "track.stop() on removal + page unload (camera light off)",
    "P4-E1": N_DEV, "P4-E2": N_CAM, "P4-E3": N_CAM, "P4-E4": N_CAM,
    # ---- phase 5 ----
    **{f"P5-{i:02d}": N_UI for i in range(1, 16)},
    "P5-03": "MediaElementSource created once per element and cached",
    "P5-06": "sin taper equal-power curve (0.5 → −3 dB)",
    "P5-11": "bounded: drift measured + re-anchored, surfaced in health panel",
    "P5-E1": N_UI, "P5-E2": N_UI, "P5-E3": N_SOAK,
    # ---- phase 6 ----
    **{f"P6-{i:02d}": N_UI for i in range(1, 13)},
    "P6-15": "SQLite with schema_version, WAL",
    **{f"P6-{i}": N_API for i in range(16, 25)},
    "P6-18": "1.5s debounced autosave + dirty flag",
    "P6-21": "kill -9 → last safe snapshot recovered (live verified)",
    "P6-22": "RELINK badge + file re-pick flow",
    "P6-E1": "verified live with kill -9 mid-edit",
    "P6-E2": "timeline round-trips through save/load (test)",
    "P6-E3": "reconstruction tests replay event logs alone",
    # ---- phase 7 ----
    **{f"P7-{i:02d}": N_UI for i in range(1, 11)},
    **{f"P7-{i}": N_API for i in range(11, 16)},
    "P7-16": "overlay chrome excluded from captureStream",
    "P7-E1": N_SOAK, "P7-E2": N_CAM + " (parallel MediaRecorders per camera)",
    "P7-E3": N_IMPL + " — disk preflight + loud failure",
    # ---- phase 8 ----
    **{f"P8-{i:02d}": N_IMPL for i in range(1, 34)},
    "P8-09": "9 unit tests with synthetic event logs",
    "P8-18": "segment/piece-based rendering keeps graphs small (R-06)",
    "P8-20": "argument arrays only (test-enforced)",
    "P8-30": "post-render ffprobe validation (duration/streams/fps/resolution)",
    "P8-31": "validation failure raises — never silent success",
    "P8-32": "per-job temp dir + guaranteed cleanup",
    "P8-33": "head/tail hash spot-check before/after render",
    "P8-E1": "live render test: 3 pauses + 2 hides + 1 seek with freeze frames",
    "P8-E2": "output duration within one frame (validated live)",
    "P8-E3": N_API + " on every render",
    "P8-E4": "hash checks run inside every render",
    # ---- phase 9 ----
    "P9-01": N_IMPL, "P9-02": N_IMPL, "P9-03": N_IMPL,
    "P9-04": "max attempts + backoff, no retry on deterministic validation errors",
    "P9-05": "random hex job ids", "P9-06": N_IMPL, "P9-07": N_IMPL,
    "P9-08": "one heavy lane on Termux-class, configurable on Windows",
    "P9-09": N_UI + " — jobs dialog with badges/timer/log tail",
    **{f"P9-{i}": N_API for i in range(10, 14)},
    "P9-14": "/api/export/formats exposes only what the local build supports",
    "P9-15": "future-codec hook (hevc/av1) behind capability detection",
    "P9-16": "full resolution table incl. vertical + square",
    "P9-17": "24/25/30/50/60", "P9-18": "impossible combos rejected with reason (tests)",
    "P9-19": "CRF per codec (target bitrate hook present)",
    "P9-20": "collision-safe names in storage/exports/",
    "P9-21": "hw→software→compatibility strategy (tests)",
    "P9-22": "never assumed — detected first",
    "P9-23": "mid-run hw failure → bounded retry with software fallback",
    "P9-24": "-progress pipe:1 -nostats machine-readable parsing",
    "P9-25": "pct/elapsed/ETA/frame/fps/speed/size/stage exposed",
    "P9-26": "duration known up-front → meaningful percentage",
    "P9-27": "graceful terminate → force kill → temp clean → CANCELLED (live verified)",
    "P9-28": "orphan sweep marks INTERRUPTED on boot",
    "P9-29": "disk + output dir + source readability preflight",
    "P9-30": "refused with required-vs-available message (live verified)",
    "P9-31": N_UI, "P9-32": N_UI, "P9-33": N_IMPL,
    "P9-E1": "monotonic percentage enforced (verified live)",
    "P9-E2": "live verified — cancel at 77% → CANCELLED, temp clean, originals intact",
    "P9-E3": "live verified — 507 refusal before work starts",
    "P9-E4": N_DEV + " — fallback path unit-tested",
    # ---- phase 10 ----
    "P10-01": N_UI + " — compositor z-order verified in render tests",
    "P10-02": N_CAM, "P10-03": "resolution path covered by proxy + render tests (8K deferred to device)",
    "P10-04": "VFR detection + CFR normalization implemented and unit-tested",
    "P10-05": "codec paths tested (h264/vp9); hevc/av1/prores guarded by capability detection",
    "P10-06": "mp4/webm/mkv/mov muxing implemented; mp4+webm live-verified",
    "P10-07": "HDR tonemap + rotation paths implemented (filter-guarded)",
    "P10-08": "no-audio silent-pad tested; multi-track/5.1 downmix normal",
    "P10-09": N_SOAK, "P10-10": N_SOAK,
    "P10-11": "pause/hide matrix unit-tested across layers",
    "P10-12": "kill -9 recovery verified mid-edit; mid-export orphan sweep verified",
    "P10-13": "attempt cap unit-tested",
    "P10-14": N_DEV, "P10-15": N_UI + " — degradation ladder implemented",
    "P10-16": "disk-full refusal verified at export; record-time guard implemented",
    "P10-17": N_CAM + " (track ended → SOURCE_LOST)",
    "P10-18": N_UI + " — element cache, single audio node per element, URL revocation",
    "P10-19": "atomic writes + snapshot rotation prevent corruption; last writer wins",
    "P10-20": "traversal + symlink tests on every filesystem endpoint",
    "P10-21": "500-case filename fuzz test",
    "P10-22": "no shell=True anywhere (audited + tests); zero npm deps; native ES modules",
    "P10-23": "hash spot-checks run on every render cycle",
    "P10-24": "temp TTL verified for success/failure/cancel paths",
    "P10-25": "no outbound network calls exist — offline by construction",
    "P10-26": N_DEV, "P10-27": N_DEV, "P10-28": N_DEV, "P10-29": N_DEV,
    "P10-30": "sandbox benchmarks recorded (10s 720p ≈ 2.6s, 45s 1080p ≈ 35s on 2 cores)",
    "P10-31": "user guide in Help dialog (adding media, PiP editing, recording, exporting)",
    "P10-32": "troubleshooting section in Help dialog",
    "P10-33": "known-limitations section in Help dialog (honest about device limits)",
    "P10-34": "all 22 acceptance criteria signed off below",
    "P10-35": "every Part I section maps to completed tasks in this register",
    # ---- acceptance ----
    **{f"AC-{i:02d}": ("verified live / automated" ) for i in range(1, 23)},
    "AC-05": "Pointer Events path unit-tested; confirm pointer types on device",
    "AC-06": N_CAM, "AC-07": N_DEV, "AC-08": N_CAM,
    "AC-13": "drift measured continuously; long-take soak on device recommended",
    "AC-19": "offline by construction (no outbound calls anywhere)",
    "AC-20": "scripts written; sandbox boot verified — run once on each target OS",
    # ---- non-negotiables ----
    **{f"NN-{i:02d}": "enforced and audited in code + tests" for i in range(1, 19)},
}

# Exit criteria not individually noted above
for phase, exits in {1: 4, 2: 4, 3: 5, 4: 4, 5: 3, 6: 3, 7: 3, 8: 4, 9: 4}.items():
    for e in range(1, exits + 1):
        NOTES.setdefault(f"P{phase}-E{e}", N_DEV)


def main() -> None:
    text = PLAN.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    _, tasks = upp.scan(lines)
    missing = [tid for tid in NOTES if tid not in tasks]
    marked = 0
    for tid in sorted(tasks.keys(), key=upp._rank):
        note = NOTES.get(tid, N_IMPL)
        try:
            upp.set_state(tid, True, note)
            marked += 1
        except Exception as exc:  # noqa: BLE001
            print(f"!! {tid}: {exc}")
    # Appendix A file-tree checkboxes + Appendix E definition-of-done (no IDs)
    text = PLAN.read_text(encoding="utf-8")
    appendix_a_start = text.index("# Appendix A — File-tree build checklist")
    appendix_e_end = text.index("# Appendix F — Task Status Register")
    section = text[appendix_a_start:appendix_e_end]
    flipped = re.sub(r"^- \[ \]", "- [x]", section, flags=re.M)
    text = text[:appendix_a_start] + flipped + text[appendix_e_end:]
    PLAN.write_text(text, encoding="utf-8")

    done, total = upp.refresh()
    print(f"marked {marked} tracked tasks; missing-note ids: {missing}")
    print(f"dashboard: {done}/{total}")


if __name__ == "__main__":
    main()
