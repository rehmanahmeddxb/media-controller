"""Recording upload/list/delete API (P7-11 … P7-15).

Recordings are browser takes (composite + per-camera) uploaded chunk-streamed
into storage/recordings/<project>/<take>/. Take metadata is persisted beside
the files. DELETE removes only generated recordings — never sources (GR-07).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.core.config import get_config
from app.core.logging import get_logger, log_diag
from app.core.storage import get_storage, new_id, sanitize_filename

router = APIRouter(prefix="/api/recording", tags=["recording"])
log = get_logger("api.recording")

CHUNK = 1024 * 1024


async def _stream_upload(upload: UploadFile, dest: Path, max_bytes: int) -> int:
    written = 0
    partial = dest.with_suffix(dest.suffix + ".part")
    with partial.open("wb") as out:
        while True:
            chunk = await upload.read(CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                partial.unlink(missing_ok=True)
                raise HTTPException(413, "Recording exceeds size limit")
            out.write(chunk)
    partial.replace(dest)  # atomic-ish commit; .part kept only if we crash mid-upload (P7-15)
    return written


@router.post("/{project_id}")
async def upload_recording(project_id: str,
                           file: UploadFile = File(...),
                           kind: str = Form("composite"),
                           take_id: Optional[str] = Form(None),
                           wall_start_ms: Optional[float] = Form(None),
                           wall_end_ms: Optional[float] = Form(None),
                           codec: Optional[str] = Form(None),
                           width: Optional[int] = Form(None),
                           height: Optional[int] = Form(None),
                           fps: Optional[float] = Form(None),
                           layer_id: Optional[str] = Form(None)) -> Dict[str, Any]:
    cfg = get_config()
    storage = get_storage()
    take = sanitize_filename(take_id or new_id("take"))
    tdir = storage.recording_dir(project_id, take)
    tdir.mkdir(parents=True, exist_ok=True)
    safe = sanitize_filename(file.filename or f"{kind}.webm", fallback=kind)
    dest = tdir / safe
    size = await _stream_upload(file, dest, cfg.recording.max_take_bytes)

    meta_path = tdir / "take.json"
    meta = {}
    if meta_path.exists():
        meta = storage.read_json(meta_path) or {}
    meta.setdefault("take_id", take)
    meta.setdefault("project_id", project_id)
    meta.setdefault("started_at", time.time())
    files = meta.setdefault("files", [])
    entry = {
        "kind": kind,  # composite | camera | audio
        "layer_id": layer_id,
        "file": safe, "size": size,
        "codec": codec, "width": width, "height": height, "fps": fps,
        "wall_start_ms": wall_start_ms, "wall_end_ms": wall_end_ms,
    }
    files = [f for f in files if not (f.get("kind") == kind and f.get("layer_id") == layer_id)]
    files.append(entry)
    meta["files"] = files
    meta["updated_at"] = time.time()
    storage.atomic_write_json(meta_path, meta)
    log.info("recording uploaded %s/%s/%s (%d bytes)", project_id, take, safe, size,
             extra={"event": "recording_upload", "project_id": project_id})
    return {"take_id": take, "file": safe, "size": size}


@router.post("/{project_id}/{take_id}/meta")
async def attach_take_meta(project_id: str, take_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Persist take metadata (timeline JSON, event log, end wall time) (P7-13)."""
    storage = get_storage()
    tdir = storage.recording_dir(project_id, take_id)
    if not tdir.exists():
        raise HTTPException(404, "take not found")
    meta_path = tdir / "take.json"
    meta = storage.read_json(meta_path) or {}
    for key in ("wall_start_ms", "wall_end_ms", "duration_s", "codec", "resolution",
                "fps", "size_bytes", "event_count"):
        if key in body:
            meta[key] = body[key]
    if isinstance(body.get("timeline"), list):
        storage.atomic_write_json(tdir / "timeline.json", body["timeline"])
        meta["timeline_file"] = "timeline.json"
    storage.atomic_write_json(meta_path, meta)
    return {"saved": True}


@router.get("/{project_id}")
async def list_recordings(project_id: str) -> Dict[str, Any]:
    storage = get_storage()
    base = storage.safe_resolve("recordings", sanitize_filename(project_id, fallback="project"))
    if not base.exists():
        return {"takes": []}
    takes = []
    for tdir in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not tdir.is_dir():
            continue
        meta = storage.read_json(tdir / "take.json") or {}
        total = storage.dir_size(tdir)
        partials = list(tdir.rglob("*.part"))
        takes.append({
            "take_id": tdir.name,
            "meta": meta,
            "total_size": total,
            "status": "INCOMPLETE" if partials else "COMPLETE",  # P7-15
            "files": [f.name for f in tdir.iterdir() if f.is_file() and f.suffix != ".part"],
        })
    return {"takes": takes}


@router.delete("/{project_id}/{take_id}")
async def delete_take(project_id: str, take_id: str) -> Dict[str, Any]:
    storage = get_storage()
    tdir = storage.recording_dir(project_id, take_id)
    if not tdir.exists():
        raise HTTPException(404, "take not found")
    size = storage.dir_size(tdir)
    storage.remove_tree(tdir)
    log_diag("Recording take deleted", take=take_id, freed=f"{size/1e6:.1f}MB")
    return {"deleted": True, "bytes_freed": size, "sources_kept": True}
