"""Final render orchestration (Phase 8 / 9).

Executes a RenderPlan built by ``compositor.py``:
  preflight (disk space, sources) -> freeze frames -> per-layer pieces ->
  final composite -> ffprobe validation -> temp cleanup.

Progress is machine-readable (``-progress pipe:1``), percentage is monotonic
(P9-25/26), cancellation is graceful-then-force (P9-27), retries are bounded
(GR-13), originals are hash spot-checked (P8-33) and never modified (GR-07).
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.config import get_config
from app.core.logging import get_logger, log_diag
from app.core.storage import StorageManager
from app.media.compositor import (RenderPlanBuilder, pick_encoder,
                                  resolve_resolution, validate_export_settings)
from app.media.ffmpeg import FFmpegRunner, get_ffmpeg
from app.media.ffprobe import FFProbe, get_ffprobe
from app.media.timeline import reconstruct_take
from app.media.validators import validate_for_render

log = get_logger("renderer")

ProgressCb = Callable[[Dict[str, Any]], None]
CancelCheck = Callable[[], bool]


def file_head_tail_hash(path: Path) -> str:
    """Fast identity hash (first+last 1 MiB) for originals-immutability audits."""
    h = hashlib.sha256()
    size = path.stat().st_size
    with path.open("rb") as fh:
        h.update(fh.read(1024 * 1024))
        if size > 2 * 1024 * 1024:
            fh.seek(-1024 * 1024, 2)
            h.update(fh.read(1024 * 1024))
    return h.hexdigest()


class RenderError(RuntimeError):
    pass


class RenderValidationError(RenderError):
    """Deterministic failure (settings/sources/disk) — retrying cannot help."""


class RenderCancelled(RenderError):
    pass


class FinalRenderer:
    def __init__(self, runner: Optional[FFmpegRunner] = None,
                 probe: Optional[FFProbe] = None,
                 storage: Optional[StorageManager] = None) -> None:
        self.runner = runner or get_ffmpeg()
        self.probe = probe or get_ffprobe()
        self.storage = storage or StorageManager()
        self.cfg = get_config()

    # ------------------------------------------------------------- preflight
    def preflight(self, sources: List[Path], output: Path,
                  duration_s: float) -> Tuple[bool, str]:
        """Disk space, output dir, source readability (P9-29, P9-30)."""
        ok, msg = self.storage.check_output_dir("exports")
        if not ok:
            return False, msg
        total_size = 0
        for src in sources:
            if not src.exists():
                return False, f"Source file missing: {src.name}"
            total_size += src.stat().st_size
        need = self.storage.estimate_temp_space(
            [total_size], self.cfg.export.space_safety_factor)
        free = self.storage.free_space("exports")
        if free < need:
            return False, (
                f"Not enough disk space: about {need / 1e9:.2f} GB is needed for a safe render "
                f"(temp + output) but only {free / 1e9:.2f} GB is free. "
                "Free some space and try again."
            )
        for src in sources:
            try:
                with src.open("rb"):
                    pass
            except OSError as exc:
                return False, f"Source not readable: {src.name}: {exc}"
        if duration_s <= 0:
            return False, "Take duration is zero — nothing to render."
        return True, "ok"

    # ------------------------------------------------------------- progress
    @staticmethod
    def parse_progress_line(line: str) -> Dict[str, Any]:
        info: Dict[str, Any] = {}
        if "=" not in line:
            return info
        key, _, val = line.partition("=")
        val = val.strip()
        if key == "out_time_us":
            try:
                info["out_time_s"] = int(val) / 1_000_000
            except ValueError:
                pass
        elif key == "out_time_ms":
            try:
                info["out_time_s"] = int(val) / 1000  # ffmpeg 7: true milliseconds
            except ValueError:
                pass
        elif key == "out_time":
            m = re.match(r"^(\d+):(\d+):(\d+(?:\.\d+)?)$", val)
            if m:
                info["out_time_s"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        elif key == "frame":
            try:
                info["frame"] = int(val)
            except ValueError:
                pass
        elif key == "fps":
            try:
                info["fps"] = float(val)
            except ValueError:
                pass
        elif key == "speed":
            m = re.match(r"^([\d.]+)x", val)
            if m:
                info["speed"] = float(m.group(1))
        elif key == "total_size":
            try:
                info["total_size"] = int(val)
            except ValueError:
                pass
        elif key == "progress":
            info["phase"] = val
        return info

    # --------------------------------------------------------------- render
    def render_take(self, *, project: Dict[str, Any], timeline_events: List[Dict[str, Any]],
                    layers_snapshot: List[Dict[str, Any]], take_start_ms: float,
                    take_end_ms: float, source_map: Dict[str, str],
                    composite_recording: Optional[str],
                    settings: Dict[str, Any], output_path: Path,
                    job_id: str, on_progress: Optional[ProgressCb] = None,
                    cancel_check: Optional[CancelCheck] = None,
                    work_dir: Optional[Path] = None) -> Dict[str, Any]:
        """The full authoritative render of one take."""
        started = time.time()
        cancel_check = cancel_check or (lambda: False)

        def cancelled() -> bool:
            if cancel_check():
                raise RenderCancelled("cancelled by user")
            return False

        def report(pct: float, stage: str, **extra: Any) -> None:
            if on_progress:
                on_progress({"pct": round(min(100.0, max(0.0, pct)), 1),
                             "stage": stage, **extra})

        # ---------------------------------------------------------- prepare
        report(0, "PREPARING")
        caps = self.runner._caps or __import__("asyncio").run(self.runner.detect())
        ok, reason = validate_export_settings(settings, caps.get("encoder_list") or [])
        if not ok:
            raise RenderValidationError(f"Export settings rejected: {reason}")
        enc = pick_encoder(settings.get("format", "mp4"), settings, caps)
        if not enc["encoder"]:
            raise RenderValidationError(f"No usable encoder: {enc.get('reason')}")
        report(2, "PREPARING", encoder=enc["encoder"], encoder_kind=enc["kind"])

        W, H = resolve_resolution(
            str(settings.get("resolution") or "1080p"),
            project.get("canvas", {}).get("aspect", "16:9"),
            tuple(settings["custom_resolution"]) if settings.get("custom_resolution") else None,
        )
        fps = int(settings.get("fps") or 30)
        fmt = (settings.get("format") or "mp4").lower()
        output_path = output_path.with_suffix(f".{fmt}")
        audio_codec = "libopus" if fmt == "webm" else "aac"

        duration_s = max(0.1, (take_end_ms - take_start_ms) / 1000.0)

        # ------------------------------------------- sources & reconstruction
        plan_data = reconstruct_take(timeline_events, layers_snapshot,
                                     take_start_ms, take_end_ms)
        resolved_sources: Dict[str, Path] = {}
        source_hashes: Dict[str, str] = {}
        layer_has_audio: Dict[str, bool] = {}
        layer_source_dur: Dict[str, float] = {}
        missing: List[str] = []
        for lid, entry in plan_data.items():
            src = source_map.get(lid)
            if src and Path(src).exists():
                resolved_sources[lid] = Path(src)
            elif entry.get("kind") == "image":
                missing.append(lid)
            else:
                missing.append(lid)

        # validate probes of available sources before queuing work (P2-05)
        for lid, path in resolved_sources.items():
            cancelled()
            md = __import__("asyncio").run(self.probe.probe(path))
            ok, issues = validate_for_render(md, path)
            if not ok:
                raise RenderValidationError(
                    f"Source for layer '{plan_data[lid].get('name', lid)}' failed validation: "
                    + "; ".join(i["message"] for i in issues if i["severity"] == "error"))
            source_hashes[str(path)] = file_head_tail_hash(path)
            layer_has_audio[lid] = bool(md.get("has_audio"))
            layer_source_dur[lid] = float(md.get("duration") or 0.0)

        sources_list = list(resolved_sources.values())
        if composite_recording and Path(composite_recording).exists():
            sources_list.append(Path(composite_recording))
        ok, reason = self.preflight(sources_list, output_path, duration_s)
        if not ok:
            raise RenderValidationError(reason)
        report(5, "PREPARING")

        work = work_dir or self.storage.temp_dir(f"render_{job_id}")
        builder = RenderPlanBuilder(
            work_dir=work, out_path=output_path, width=W, height=H, fps=fps,
            encoder=enc["encoder"],
            crf=int(settings.get("crf") or self.cfg.export.crf_default.get(enc["encoder"], 20)),
            bitrate=int(settings["bitrate"]) if settings.get("bitrate") else None,
            audio_codec=audio_codec,
            background=str(settings.get("background") or "black"),
            loudnorm=bool(settings.get("loudnorm")),
        )

        # ------------------------------------------------------ plan build
        piece_files: List[Dict[str, Any]] = []
        if missing and not resolved_sources and composite_recording and Path(composite_recording).exists():
            # full fallback: transcode the composited take (§16, documented)
            builder.composite_fallback_step(Path(composite_recording), duration_s)
            fallback_mode = "composite-only"
        else:
            fallback_mode = "sources"
            ordered_layers = sorted(plan_data.items(),
                                    key=lambda kv: value_z(kv[1]))
            for lid, entry in ordered_layers:
                cancelled()
                if lid not in resolved_sources:
                    continue  # missing source layers are skipped (logged below)
                for piece in builder.layer_pieces(entry):
                    pf = builder.piece_render_step(
                        piece, resolved_sources[lid],
                        has_audio=layer_has_audio.get(lid, True),
                        source_duration=layer_source_dur.get(lid))
                    piece_files.append({"path": pf, "piece": piece, "entry": entry, "layer_id": lid})
            if not piece_files:
                if composite_recording and Path(composite_recording).exists():
                    builder.composite_fallback_step(Path(composite_recording), duration_s)
                    fallback_mode = "composite-only"
                else:
                    raise RenderValidationError("No renderable layers: all layer sources are missing.")
            else:
                builder.final_composite_step(piece_files, duration_s)

        if missing:
            log.warning("layers skipped, missing sources: %s",
                        [f"{plan_data[l].get('name', l)}" for l in missing],
                        extra={"event": "render_missing_sources", "job_id": job_id})

        # execute steps with weighted progress
        total_weight = sum(s["weight"] for s in builder.steps) or 1.0
        done_weight = 0.0
        for step in builder.steps:
            cancelled()
            step_dur = float(step.get("duration") or 1.0)
            label = step["label"]
            base_pct = 5 + 90.0 * (done_weight / total_weight)
            span_pct = 90.0 * (step["weight"] / total_weight)
            last_frac_box = [0.0]  # per-step monotonic guard (P9-E1)

            def on_line(line: str, _step=step, _base=base_pct, _span=span_pct,
                        _dur=step_dur, _label=label, _box=last_frac_box) -> None:
                info = self.parse_progress_line(line)
                if not info or "out_time_s" not in info:
                    return
                frac = min(1.0, max(0.0, float(info["out_time_s"]) / max(_dur, 0.001)))
                if frac < _box[0]:
                    return  # stale/duplicate progress line — never move backwards
                _box[0] = frac
                elapsed = time.time() - started
                eta = (elapsed / max(frac, 0.01)) * (1 - frac) if frac > 0.01 else None
                report(_base + _span * frac, _label,
                       frame=info.get("frame"), fps=info.get("fps"),
                       speed=info.get("speed"), size=info.get("total_size"),
                       eta_s=round(eta, 1) if eta is not None else None)

            rc, out = self.runner.run_sync(
                step["args"], timeout=7200, on_line=on_line, cancel_check=cancel_check)
            if rc == -2:
                raise RenderCancelled("cancelled during ffmpeg")
            if rc != 0:
                tail = (out or "").strip().splitlines()[-12:]
                raise RenderError(f"FFmpeg failed during '{label}' (exit {rc}): " + " | ".join(tail[-4:]))
            done_weight += step["weight"]
            report(5 + 90.0 * (done_weight / total_weight), label)

        # ------------------------------------------------------ validation
        cancelled()
        report(96, "VALIDATING")
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RenderError("Render produced no output file")
        md = __import__("asyncio").run(self.probe.probe(output_path))
        problems: List[str] = []
        if not md.get("has_video"):
            problems.append("no video stream in output")
        out_dur = float(md.get("duration") or 0)
        if abs(out_dur - duration_s) > max(0.1, 2.0 / fps):
            problems.append(f"duration mismatch: expected {duration_s:.2f}s got {out_dur:.2f}s")
        v = md.get("video") or {}
        if not v.get("width") or not v.get("height"):
            problems.append("output resolution missing")
        if problems:
            raise RenderError("Post-render validation failed: " + "; ".join(problems))

        # originals immutability spot-check (P8-33, P10-23)
        for path_s, before in source_hashes.items():
            after = file_head_tail_hash(Path(path_s))
            if after != before:
                raise RenderError(f"CRITICAL: source file changed during render: {path_s}")

        # cleanup temp (P8-32, P9-27)
        try:
            import shutil
            shutil.rmtree(work, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
        self.storage.sweep_temp(0.0)

        result = {
            "output": str(output_path),
            "duration_s": round(out_dur, 3),
            "size_bytes": output_path.stat().st_size,
            "resolution": f"{v.get('width')}x{v.get('height')}",
            "fps": v.get("fps"),
            "encoder": enc["encoder"],
            "encoder_kind": enc["kind"],
            "fallback_mode": fallback_mode,
            "skipped_layers": [plan_data[l].get("name", l) for l in missing],
            "elapsed_s": round(time.time() - started, 1),
        }
        report(100, "COMPLETED", **result)
        log.info("render complete %s", result, extra={"event": "render_done", "job_id": job_id})
        log_diag("Export finished", output=output_path.name,
                 duration=f"{out_dur:.1f}s", size=f"{result['size_bytes']/1e6:.1f}MB")
        return result


def value_z(entry: Dict[str, Any]) -> int:
    zs = entry.get("zorder") or []
    return int(zs[0]["value"]) if zs else 0
