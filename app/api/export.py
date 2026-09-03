"""Export jobs API (P9-10 … P9-33).

  POST   /api/export                — validate + queue a render job
  GET    /api/export                — list jobs
  GET    /api/export/{job_id}       — status + progress
  POST   /api/export/{job_id}/cancel— graceful cancellation (P9-27)
  GET    /api/export/{job_id}/log   — structured progress log (polling/SSE)
  GET    /api/export/formats        — codecs actually available locally
  DELETE /api/export/{job_id}       — cleanup temp
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.core.storage import get_storage
from app.media.compositor import FORMAT_CODECS, FPS_OPTIONS, RESOLUTIONS, validate_export_settings
from app.media.ffmpeg import get_ffmpeg
from app.workers.export_worker import get_export_worker
from app.workers.job_manager import ACTIVE_STATES, get_job_manager

router = APIRouter(prefix="/api/export", tags=["export"])


@router.get("/formats")
async def available_formats() -> Dict[str, Any]:
    """Only expose what the local FFmpeg build actually supports (P9-14)."""
    runner = get_ffmpeg()
    caps = await runner.detect()
    encoders = set(caps.get("encoder_list") or [])
    formats = {}
    for fmt, spec in FORMAT_CODECS.items():
        vcodec = next((c for c in spec["video"] if c in encoders), None)
        acodec = next((c for c in spec["audio"] if c in encoders), None)
        formats[fmt] = {
            "available": bool(vcodec and acodec),
            "video_codec": vcodec, "audio_codec": acodec,
            "label": spec["vcodec_report"],
        }
    future = {}
    for c in ("hevc", "libx265", "libsvtav1", "libaom-av1"):
        if c in encoders:
            future[c] = True  # P9-15: guarded by capability detection
    return {
        "formats": formats,
        "resolutions": list(RESOLUTIONS.keys()),
        "fps": list(FPS_OPTIONS),
        "future_codecs": future,
        "hw_encoders": caps.get("hw_encoders"),
    }


@router.post("")
async def create_export(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "invalid JSON body") from exc

    settings = body.get("settings") or {}
    project = body.get("project") or {}
    runner = get_ffmpeg()
    caps = await runner.detect()
    if not caps.get("available"):
        raise HTTPException(503, caps.get("remediation") or "FFmpeg not available")
    ok, reason = validate_export_settings(settings, caps.get("encoder_list") or [])
    if not ok:
        raise HTTPException(422, reason)  # impossible combos rejected (P9-18)

    # ---- resolve layer sources server-side ------------------------------- (P8)
    # media_ids -> original paths; camera layers -> per-camera take files;
    # composite recording from the take as fallback source.
    from app.core.database import get_db

    source_map: Dict[str, str] = {}
    media_ids = body.get("media_ids") or {}
    for layer_id, media_id in media_ids.items():
        row = get_db().get_media(str(media_id))
        if row and row.get("original_path"):
            source_map[str(layer_id)] = row["original_path"]

    composite_recording = None
    take_id = body.get("take_id")
    project_id = body.get("project_id")
    if take_id and project_id:
        storage = get_storage()
        try:
            tdir = storage.recording_dir(str(project_id), str(take_id))
            meta = storage.read_json(tdir / "take.json") or {}
            for f in meta.get("files", []):
                path = tdir / f.get("file", "")
                if f.get("kind") == "composite" and path.exists():
                    composite_recording = str(path)
                elif f.get("kind") == "camera" and f.get("layer_id"):
                    source_map.setdefault(str(f["layer_id"]), str(path))
        except Exception:  # noqa: BLE001 — recording lookup must never block export
            pass
    body["source_map"] = source_map
    body["composite_recording"] = composite_recording


    # preflight disk space before queueing anything (P9-29/30)
    storage = get_storage()
    sources = [Path(p) for p in (body.get("source_map") or {}).values() if p]
    if body.get("composite_recording"):
        sources.append(Path(body["composite_recording"]))
    duration_s = max(0.0, ((body.get("take_end_ms") or 0) - (body.get("take_start_ms") or 0)) / 1000.0)
    if duration_s <= 0:
        raise HTTPException(422, "Take duration is zero — record a take before exporting.")
    source_sizes = [s.stat().st_size for s in sources if s.exists()]
    need = storage.estimate_temp_space(source_sizes)
    free = storage.free_space("exports")
    if free < need:
        raise HTTPException(507, (
            f"Not enough disk space: ~{need/1e9:.2f} GB required, {free/1e9:.2f} GB free. "
            "The export was NOT started."))

    manager = get_job_manager()
    job = manager.submit("export", body, project_id=body.get("project_id"))
    get_export_worker().enqueue(job.id)
    return {"job_id": job.id, "state": job.state.value}


@router.get("")
async def list_exports(project_id: Optional[str] = None) -> Dict[str, Any]:
    return {"jobs": get_job_manager().list_jobs(project_id)}


@router.get("/{job_id}")
async def export_status(job_id: str) -> Dict[str, Any]:
    job = get_job_manager().get(job_id)
    if job:
        return job.as_dict()
    row = get_job_manager().db.get_job(job_id)
    if not row:
        raise HTTPException(404, f"job '{job_id}' not found")
    return {**row, "log_tail": []}


@router.post("/{job_id}/cancel")
async def cancel_export(job_id: str) -> Dict[str, Any]:
    ok = get_job_manager().cancel(job_id)
    if not ok:
        raise HTTPException(409, "job not active or not found")
    return {"cancelling": True, "job_id": job_id}


@router.delete("/{job_id}")
async def delete_export(job_id: str) -> Dict[str, Any]:
    manager = get_job_manager()
    job = manager.get(job_id)
    if job and job.state in ACTIVE_STATES:
        manager.cancel(job_id)
    get_storage().sweep_temp(0.0)  # clean leftovers (P9-11)
    return {"cleaned": True}


@router.get("/{job_id}/log")
async def export_log(job_id: str, sse: bool = False):
    """Structured progress stream — polling JSON or SSE (P9-12)."""
    manager = get_job_manager()

    def current() -> Dict[str, Any]:
        job = manager.get(job_id)
        if job:
            return job.as_dict()
        row = manager.db.get_job(job_id)
        if not row:
            raise HTTPException(404, "job not found")
        return {**row, "log_tail": []}

    if not sse:
        return current()

    async def event_stream():
        last = None
        for _ in range(60 * 60):  # bounded: 1h max stream
            data = current()
            if isinstance(data, dict) and data != last:
                yield f"data: {json.dumps(data)}\n\n"
                last = data
                if data.get("state") not in ACTIVE_STATES:
                    return
            await asyncio.sleep(1.0)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
