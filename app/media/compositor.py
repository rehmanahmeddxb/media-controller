"""FFmpeg filter-graph builder — the final compositor (P8-10 … P8-20).

Strategy (segment-based, R-06): the take is cut into per-layer *pieces*
(maximal intervals of continuous visible content with stable geometry).
Each piece renders to a small intermediate file with a tiny filter graph;
the final pass overlays every piece onto the canvas in z-order and mixes
all audio. Every command is an argument array — never a shell string
(GR-17). Python never decodes frames (GR-02).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.logging import get_logger
from app.media.timeline import (pieces_from_plan, reconstruct_take,
                                validate_geometry, value_at, visible_during)

log = get_logger("compositor")

RESOLUTIONS: Dict[str, Tuple[int, int]] = {
    # landscape
    "480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080),
    "1440p": (2560, 1440), "2160p": (3840, 2160),
    # vertical
    "720x1280": (720, 1280), "1080x1920": (1080, 1920),
    "1440x2560": (1440, 2560), "2160x3840": (2160, 3840),
    # square
    "1080x1080": (1080, 1080), "2160x2160": (2160, 2160),
}

FPS_OPTIONS = (24, 25, 30, 50, 60)

FORMAT_CODECS = {
    "mp4":  {"video": ["libx264", "h264"], "audio": ["aac"], "vcodec_report": "H.264+AAC"},
    "mov":  {"video": ["libx264", "h264"], "audio": ["aac"], "vcodec_report": "H.264+AAC"},
    "mkv":  {"video": ["libx264", "h264", "libx265", "hevc"], "audio": ["aac", "libopus"], "vcodec_report": "H.264+AAC"},
    "webm": {"video": ["libvpx-vp9", "vp9"], "audio": ["libopus", "opus"], "vcodec_report": "VP9+Opus"},
}


def even(n: int) -> int:
    return max(2, n - (n % 2))


def resolve_resolution(name: str, aspect: str = "16:9",
                       custom: Optional[Tuple[int, int]] = None) -> Tuple[int, int]:
    if custom:
        return even(custom[0]), even(custom[1])
    if name in RESOLUTIONS:
        w, h = RESOLUTIONS[name]
        return even(w), even(h)
    base = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}.get(aspect, (1920, 1080))
    return even(base[0]), even(base[1])


def validate_export_settings(settings: Dict[str, Any], available_encoders: List[str]) -> Tuple[bool, str]:
    """Reject impossible combinations with a clear reason (P9-18)."""
    fmt = (settings.get("format") or "mp4").lower()
    if fmt not in FORMAT_CODECS:
        return False, f"Unknown format '{fmt}'. Supported: {', '.join(FORMAT_CODECS)}."
    fps = int(settings.get("fps") or 30)
    if fps not in FPS_OPTIONS:
        return False, f"Unsupported fps {fps}. Supported: {', '.join(map(str, FPS_OPTIONS))}."
    res = str(settings.get("resolution") or "1080p")
    if res not in RESOLUTIONS and not (res.startswith("custom:") or settings.get("custom_resolution")):
        return False, f"Unknown resolution '{res}'."
    codec = (settings.get("video_codec") or "").lower()
    allowed = FORMAT_CODECS[fmt]["video"]
    if codec:
        if codec not in allowed:
            return False, f"Codec '{codec}' cannot be muxed into '{fmt}'. Allowed: {', '.join(allowed)}."
        if available_encoders and codec not in available_encoders:
            return False, (
                f"Encoder '{codec}' is not available in your FFmpeg build. "
                "Install a full FFmpeg build (e.g. 'pkg install ffmpeg' on Termux)."
            )
    return True, "ok"


def pick_encoder(fmt: str, settings: Dict[str, Any], caps: Dict[str, Any]) -> Dict[str, Any]:
    """Encoder strategy: verified hw -> software -> compatibility (P9-21, P9-22)."""
    encoders = set(caps.get("encoder_list") or [])
    hw = caps.get("hw_encoders") or {}
    requested = (settings.get("video_codec") or "").lower()
    candidates: List[str] = []

    if fmt == "webm":
        software = ["libvpx-vp9"]
    else:
        software = ["libx264"]
    if requested:
        software = [c for c in software if c == requested] or software

    # hardware candidates for h264-family outputs
    hw_candidates: List[str] = []
    if software and software[0].startswith("libx264"):
        for vendor in ("nvenc", "qsv", "amf", "vaapi", "videotoolbox"):
            info = hw.get(vendor) or {}
            for enc in info.get("encoders", []):
                if enc.startswith("h264"):
                    hw_candidates.append(enc)
    ordered = (hw_candidates if not requested else []) + software
    for cand in ordered:
        if cand in encoders:
            return {"encoder": cand, "kind": "hw" if cand in hw_candidates else "software",
                    "hw_verified": False}
    return {"encoder": None, "kind": "none", "hw_verified": False,
            "reason": "no usable encoder in the local FFmpeg build"}


def encoder_args(encoder: str, fps: int, crf: Optional[int] = None,
                 bitrate: Optional[int] = None) -> List[str]:
    """Encoder flags per codec — never assume hardware exists (P9-22)."""
    if encoder in ("h264_nvenc", "h264_qsv", "h264_amf", "h264_vaapi", "h264_videotoolbox"):
        args = ["-c:v", encoder]
        if bitrate:
            args += ["-b:v", str(bitrate)]
        else:
            args += ["-qp", str(crf if crf is not None else 19)]
        return args
    if encoder == "libx265":
        args = ["-c:v", "libx265", "-preset", "medium", "-crf", str(crf if crf is not None else 24)]
    elif encoder == "libvpx-vp9":
        args = ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "5",
                "-crf", str(crf if crf is not None else 31), "-b:v", "0"]
    else:  # libx264 and compatibility fallback
        args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf if crf is not None else 19)]
    args += ["-pix_fmt", "yuv420p", "-r", str(fps)]
    return args


def _fit_filter(mode: str, w: int, h: int) -> str:
    mode = mode or "contain"
    if mode == "stretch":
        return f"scale={w}:{h}"
    if mode == "cover":
        return (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h}")
    # contain: letterbox/pillarbox pad (P3-10, P8-10)
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")


class RenderPlanBuilder:
    """Builds the complete list of FFmpeg argument arrays for one export."""

    def __init__(self, work_dir: Path, out_path: Path, width: int, height: int,
                 fps: int, encoder: str, crf: Optional[int] = None,
                 bitrate: Optional[int] = None, audio_codec: str = "aac",
                 background: str = "black", loudnorm: bool = False) -> None:
        self.work = work_dir
        self.out = out_path
        self.W, self.H = even(width), even(height)
        self.fps = fps
        self.encoder = encoder
        self.crf = crf
        self.bitrate = bitrate
        self.audio_codec = audio_codec
        self.background = background or "black"
        self.loudnorm = loudnorm
        self.steps: List[Dict[str, Any]] = []
        self.freeze_targets: Dict[str, str] = {}

    # ------------------------------------------------------------- pieces
    def layer_pieces(self, entry: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Pieces split on visibility, geometry and z-order changes (P8-03/8-11/8-12)."""
        base = pieces_from_plan(entry)
        split: List[Dict[str, Any]] = []
        for piece in base:
            # collect change points inside the piece
            changes: List[float] = []
            for series in ("geometry", "zorder"):
                for p in entry.get(series, []):
                    if piece["start"] < p["t"] < piece["end"]:
                        changes.append(float(p["t"]))
            # visibility flips split pieces — hidden periods are absent from
            # the output entirely (P8-03)
            for p in entry.get("visibility", []):
                if piece["start"] < p["t"] < piece["end"]:
                    changes.append(float(p["t"]))
            bounds = sorted(set(round(c, 3) for c in changes))
            cur = piece["start"]
            for b in bounds + [piece["end"]]:
                mid = (cur + min(b, piece["end"])) / 2
                segs = [s for s in piece["segments"] if s["start"] < b - 0.002 and s["end"] > cur + 0.002]
                segs = [dict(s, start=max(s["start"], cur), end=min(s["end"], b)) for s in segs]
                segs = [s for s in segs if s["end"] - s["start"] > 0.004]
                if segs and visible_during(entry, mid):
                    geo = value_at(entry["geometry"], cur + 0.001,
                                   entry["geometry"][0]["value"] if entry["geometry"] else None)
                    z = value_at(entry["zorder"], cur + 0.001, 0)
                    split.append({
                        "start": round(cur, 4), "end": round(b, 4),
                        "segments": segs,
                        "geometry": validate_geometry(geo or {}),
                        "z": int(z or 0),
                        "layer": entry["id"], "kind": entry["kind"],
                        "name": entry.get("name", entry["id"]),
                        "source": entry.get("source"),
                    })
                cur = b
        return split

    # ------------------------------------------------------ freeze frames
    def freeze_extract_step(self, source: Path, media_time: float, out_png: Path) -> None:
        """P8-05: freeze-frame extraction for intentional pauses."""
        key = f"{source}|{round(media_time, 2)}"
        if key in self.freeze_targets:
            return
        self.freeze_targets[key] = str(out_png)
        self.steps.append({
            "kind": "freeze",
            "label": f"freeze frame @{media_time:.2f}s",
            "weight": 0.4,
            "duration": 1.0,
            "args": [
                "-hide_banner", "-nostdin", "-y",
                "-ss", f"{max(0.0, media_time):.3f}",
                "-i", str(source),
                "-frames:v", "1",
                str(out_png),
            ],
        })

    # ------------------------------------------------------ piece render
    def piece_render_step(self, piece: Dict[str, Any], source: Path,
                          has_audio: bool = True,
                          source_duration: Optional[float] = None) -> Path:
        """Render one layer piece to an intermediate file (P8-18)."""
        idx = len([s for s in self.steps if s["kind"] == "piece"])
        piece_file = self.work / f"piece_{idx:04d}_{piece['layer']}.mp4"
        segs = piece["segments"]
        piece_dur = round(piece["end"] - piece["start"], 4)

        inputs: List[str] = []
        vparts: List[str] = []
        aparts: List[str] = []
        concat_v: List[str] = []
        concat_a: List[str] = []
        input_idx = 0

        # fast-seek offset when the first play segment starts late (P8-07)
        first_play = next((s for s in segs if s["kind"] == "play"), None)
        seek_off = 0.0
        if first_play and first_play["mediaStart"] > 5.0:
            seek_off = first_play["mediaStart"]
            inputs += ["-ss", f"{seek_off:.3f}"]
        inputs += ["-i", str(source)]
        media_input = input_idx
        input_idx += 1

        # ---- per-segment sub-streams, in strict chronological order -------
        play_idx = 0
        freeze_idx = 0
        play_audio_labels: Dict[int, str] = {}
        freeze_video_inputs: Dict[int, str] = {}
        freeze_audio_labels: Dict[int, str] = {}
        for s in segs:
            if s["kind"] == "play":
                s_start = round(s["mediaStart"] - seek_off, 4)
                s_end = round(s_start + (s["end"] - s["start"]), 4)
                vparts.append(
                    f"[pv{play_idx}]trim=start={s_start}:end={s_end},setpts=PTS-STARTPTS[v{play_idx}]")
                if has_audio:
                    aparts.append(
                        f"[pa{play_idx}]atrim=start={s_start}:end={s_end},"
                        f"asetpts=PTS-STARTPTS[a{play_idx}]")
                    play_audio_labels[play_idx] = f"[a{play_idx}]"
                play_idx += 1
            else:  # freeze: still frame + silence (P8-04, P8-05, P8-26)
                dur = round(s["end"] - s["start"], 4)
                png = self.work / f"freeze_{piece['layer']}_{round(s['mediaTime'] * 10)}.png"
                self.freeze_extract_step(Path(source), float(s["mediaTime"]), png)
                inputs += ["-loop", "1", "-framerate", str(self.fps), "-t", f"{dur:.4f}", "-i", str(png)]
                freeze_video_inputs[freeze_idx] = f"[{input_idx}:v]"
                aparts.append(f"anullsrc=r=48000:cl=stereo:duration={dur:.4f}[fa{freeze_idx}]")
                freeze_audio_labels[freeze_idx] = f"[fa{freeze_idx}]"
                input_idx += 1
                freeze_idx += 1

        # split the media input into one branch per play segment
        if play_idx > 1:
            vparts.insert(0, f"[{media_input}:v]split={play_idx}" + "".join(f"[pv{i}]" for i in range(play_idx)))
            if has_audio:
                aparts.insert(0, f"[{media_input}:a]asplit={play_idx}" + "".join(f"[pa{i}]" for i in range(play_idx)))
        elif play_idx == 1:
            vparts.insert(0, f"[{media_input}:v]null[pv0]")
            if has_audio:
                aparts.insert(0, f"[{media_input}:a]anull[pa0]")

        # chronological concat lists (video + audio together)
        p_i = f_i = 0
        for s in segs:
            if s["kind"] == "play":
                concat_v.append(f"[v{p_i}]")
                if has_audio:
                    concat_a.append(play_audio_labels[p_i])
                p_i += 1
            else:
                concat_v.append(freeze_video_inputs[f_i])
                concat_a.append(freeze_audio_labels[f_i])
                f_i += 1

        vparts.append("".join(concat_v) + f"concat=n={len(concat_v)}:v=1:a=0[vcat]")
        # hold-last-frame when the source is shorter than the piece needs (P8-06)
        hold_pad = 0.0
        if source_duration:
            needed_end = max((s.get("mediaEnd", s.get("mediaStart", 0))
                              for s in segs if s["kind"] == "play"), default=0.0)
            hold_pad = max(0.0, needed_end - source_duration) + 0.5
        if hold_pad > 0.01:
            vparts.append(
                f"[vcat]tpad=stop_mode=clone:stop_duration={hold_pad:.3f},"
                f"fps={self.fps},format=yuv420p[vout]")
        else:
            vparts.append(f"[vcat]fps={self.fps},format=yuv420p[vout]")

        # silent pad policy (P8-26): audio-less sources/freeze gaps get silence
        # so the final amix never desyncs.
        if not concat_a:
            aparts.append(f"anullsrc=r=48000:cl=stereo:duration={piece_dur:.4f}[acat]")
        else:
            aparts.append("".join(concat_a) + f"concat=n={len(concat_a)}:v=0:a=1[acat]")
        aparts.append(
            f"[acat]aresample=48000,aformat=channel_layouts=stereo,"
            f"apad=whole_dur={piece_dur:.4f}[aout]")

        filter_complex = ";".join(vparts + aparts)
        args = [
            "-hide_banner", "-nostdin", "-y",
            *inputs,
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "[aout]",
            "-t", f"{piece_dur:.4f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-r", str(self.fps),
            "-c:a", "aac", "-b:a", "192k",
            str(piece_file),
            "-progress", "pipe:1", "-nostats",
        ]
        self.steps.append({
            "kind": "piece",
            "label": f"layer '{piece['name']}' piece {piece['start']:.1f}s–{piece['end']:.1f}s",
            "weight": max(1.0, piece_dur),
            "duration": piece_dur,
            "args": args,
            "output": str(piece_file),
            "piece": piece,
        })
        return piece_file

    # ------------------------------------------------------------ final
    def final_composite_step(self, piece_files: List[Dict[str, Any]],
                             total_duration: float) -> None:
        """Overlay every piece in z-order and mix all audio (P8-12 … P8-27)."""
        inputs: List[str] = []
        chains: List[str] = []
        video_labels: List[str] = []
        audio_labels: List[str] = []
        audios: List[Dict[str, Any]] = []

        # canvas background is input 0 (P8-13, P8-14)
        inputs += ["-f", "lavfi", "-t", f"{total_duration:.4f}",
                   "-i", f"color=c={self.background}:s={self.W}x{self.H}:r={self.fps}"]

        for i, pf in enumerate(piece_files, start=1):
            inputs += ["-i", pf["path"]]
            piece = pf["piece"]
            geo = piece["geometry"]
            bx, by = even(int(self.W * geo["w"])), even(int(self.H * geo["h"]))
            bx, by = max(2, bx), max(2, by)
            ox, oy = int(self.W * geo["x"]), int(self.H * geo["y"])
            fit = _fit_filter(piece.get("fit", "contain"), bx, by)
            chains.append(f"[{i}:v]{fit}[fv{i}]")
            video_labels.append((f"[fv{i}]", ox, oy, piece["start"], piece["end"], piece["z"]))

            # audio: piece-local volume automation -> adelay to piece start (P8-21..23)
            entry = pf["entry"]
            vol_expr = self._volume_expression(entry, piece["start"], piece["end"])
            delay_ms = int(round(piece["start"] * 1000))
            chains.append(
                f"[{i}:a]volume='{vol_expr}':eval=frame,"
                f"adelay={delay_ms}|{delay_ms}[fa{i}]"
            )
            audio_labels.append(f"[fa{i}]")
            audios.append(pf)

        chains.append("[0:v]null[bg]")
        prev = "[bg]"
        # sort by z-order then start time — later = drawn on top (P8-12)
        ordered = sorted(video_labels, key=lambda t: (t[5], t[3], t[4]))
        for n, (label, ox, oy, t0, t1, _z) in enumerate(ordered):
            out_label = f"[cmp{n}]"
            chains.append(
                f"{prev}{label}overlay=x={ox}:y={oy}:eof_action=pass:"
                f"enable='between(t,{t0:.4f},{t1:.4f})'{out_label}"
            )
            prev = out_label
        chains.append(f"[cmp{len(ordered) - 1}]fps={self.fps},format=yuv420p[vfinal]" if ordered
                      else "null[vfinal]")

        if audio_labels:
            n = len(audio_labels)
            chains.append(
                "".join(audio_labels) +
                f"amix=inputs={n}:duration=longest:normalize=0[mix]")
            if self.loudnorm:
                chains.append("[mix]loudnorm=I=-16:TP=-1.5:LRA=11[afinal]")
            else:
                chains.append("[mix]aresample=48000,aformat=channel_layouts=stereo[afinal]")
        filter_complex = ";".join(chains)

        args = ["-hide_banner", "-nostdin", "-y", *inputs,
                "-filter_complex", filter_complex,
                "-map", "[vfinal]", "-t", f"{total_duration:.4f}"]
        args += encoder_args(self.encoder, self.fps, self.crf, self.bitrate)
        if audio_labels:
            args += ["-map", "[afinal]",
                     "-c:a", self.audio_codec, "-b:a", "192k" if self.audio_codec == "aac" else "160k"]
        else:
            args += ["-an"]
        if self.out.suffix.lower() in (".mp4", ".mov", ".m4v"):
            args += ["-movflags", "+faststart"]  # P8-29
        args += [str(self.out), "-progress", "pipe:1", "-nostats"]
        self.steps.append({
            "kind": "final",
            "label": "final composite",
            "weight": max(4.0, total_duration / 4),
            "duration": total_duration,
            "args": args,
            "output": str(self.out),
        })

    @staticmethod
    def _volume_expression(entry: Dict[str, Any], start: float, end: float,
                           max_terms: int = 40) -> str:
        """Time-enabled volume expression from volume/mute intervals (P8-21/22)."""
        points = [p for p in entry.get("volume", []) if start <= p["t"] <= end]
        if len(points) > max_terms:
            points = points[:1]  # bounded: constant at piece start (GR-13)
        if not points:
            first = [p for p in entry.get("volume", []) if p["t"] <= start]
            base = first[-1]["value"] if first else 1.0
            return f"{float(base):.3f}"
        pieces_expr: List[str] = []
        for i, p in enumerate(points):
            t_local = max(0.0, p["t"] - start)
            nxt = points[i + 1]["t"] - start if i + 1 < len(points) else None
            vol = max(0.0, min(1.0, float(p["value"])))
            # equal-power perceptual approximation is applied at capture; here linear gain
            if nxt is None:
                pieces_expr.append(f"gte(t,{t_local:.3f})*{vol:.3f}")
            else:
                pieces_expr.append(f"between(t,{t_local:.3f},{nxt:.3f})*{vol:.3f}")
        if not points or points[0]["t"] > start:
            first = [p for p in entry.get("volume", []) if p["t"] <= start]
            base = float(first[-1]["value"]) if first else 1.0
            pieces_expr.insert(0, f"lt(t,{max(0.0, points[0]['t'] - start):.3f})*{base:.3f}")
        expr = "+".join(pieces_expr)
        return f"min(1,{expr})" if expr else "1.0"

    # -------------------------------------------------- composite fallback
    def composite_fallback_step(self, composite_recording: Path, total_duration: float) -> None:
        """When per-source reconstruction is impossible, transcode the take's
        composited browser recording (documented fallback, §16)."""
        args = [
            "-hide_banner", "-nostdin", "-y",
            "-i", str(composite_recording),
            "-vf", f"scale={self.W}:{self.H}:force_original_aspect_ratio=decrease,"
                   f"pad={self.W}:{self.H}:(ow-iw)/2:(oh-ih)/2:color=black,fps={self.fps},format=yuv420p",
            "-t", f"{total_duration:.4f}",
        ]
        args += encoder_args(self.encoder, self.fps, self.crf, self.bitrate)
        args += ["-c:a", self.audio_codec, "-b:a", "192k"]
        if self.out.suffix.lower() in (".mp4", ".mov", ".m4v"):
            args += ["-movflags", "+faststart"]
        args += [str(self.out), "-progress", "pipe:1", "-nostats"]
        self.steps.append({
            "kind": "final",
            "label": "composite transcode (fallback)",
            "weight": max(4.0, total_duration / 4),
            "duration": total_duration,
            "args": args,
            "output": str(self.out),
        })
