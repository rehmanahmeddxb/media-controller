"""Media ingest API (P2-07 … P2-12).

  POST /api/media/probe     — upload a copy or probe a registered path
  POST /api/media/register  — register a local file inside a storage root
  GET  /api/media/{id}      — metadata + proxy status
  GET  /api/media/{id}/file — range-capable streaming (original or proxy)
  POST /api/media/{id}/proxy— generate proxy (decision engine)
  DELETE /api/media/{id}    — removes generated proxies/temp ONLY (GR-07)

Uploads stream to disk in chunks — no full-file RAM buffering (P2-11).
All client filenames sanitized (P2-12, GR-17/18).
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from app.core.config import get_config
from app.core.database import get_db
from app.core.logging import get_logger, log_diag
from app.core.storage import PathEscapeError, get_storage, new_id, sanitize_filename
from app.media.ffprobe import get_ffprobe
from app.media.proxy import ProxyGenerator, decide_proxy, device_class
from app.media.validators import validate_file, validate_probe

router = APIRouter(prefix="/api/media", tags=["media"])
log = get_logger("api.media")

CHUNK = 1024 * 1024


async def _stream_to_disk(upload: UploadFile, dest: Path, max_bytes: int) -> int:
    """Stream upload to disk chunk-wise (P2-11)."""
    written = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await upload.read(CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(413, (
                        f"File too large: exceeds {max_bytes / 1e9:.1f} GB limit."))
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    return written


def _require_media(media_id: str) -> Dict[str, Any]:
    row = get_db().get_media(media_id)
    if not row:
        raise HTTPException(404, f"media '{media_id}' not registered")
    return row


@router.post("/probe")
async def probe_media(request: Request,
                      file: Optional[UploadFile] = File(None)) -> Dict[str, Any]:
    """Probe an uploaded copy or {\"path\": ...} of an already-registered file (P2-07)."""
    probe = get_ffprobe()
    if file is not None:
        cfg = get_config()
        storage = get_storage()
        safe = sanitize_filename(file.filename or "upload", fallback="upload")
        media_id = new_id("media")
        dest_dir = storage.proxy_dir(media_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / safe
        size = await _stream_to_disk(file, dest, cfg.media.max_upload_bytes)
        try:
            md = await probe.probe(dest)
        except Exception as exc:  # noqa: BLE001
            dest.unlink(missing_ok=True)
            raise HTTPException(422, f"Probe failed: {exc}") from exc
        ok, issues = validate_probe(md)
        get_db().upsert_media(media_id, str(dest), safe, size, dest.stat().st_mtime, md)
        log.info("media uploaded+probed %s", safe, extra={"event": "media_ingest", "media_id": media_id})
        return {"media_id": media_id, "metadata": md, "valid": ok, "issues": issues,
                "registered": True, "upload": {"size": size, "name": safe}}
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    path = body.get("path") or body.get("media_id")
    if not path:
        raise HTTPException(400, "Provide a file upload or a JSON body with 'path'")
    if isinstance(path, str) and path.startswith("media_"):
        row = _require_media(path)
        path = row["original_path"]
    p = Path(path)
    for issue in validate_file(p):
        if issue.severity == "error":
            raise HTTPException(404, issue.message)
    try:
        md = await probe.probe(p)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, f"Probe failed: {exc}") from exc
    ok, issues = validate_probe(md)
    return {"media_id": None, "metadata": md, "valid": ok, "issues": issues, "registered": False}


@router.post("/register")
async def register_media(request: Request) -> Dict[str, Any]:
    """Register a file that lives inside a configured root (P2-08)."""
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "invalid JSON body") from exc
    path_s = body.get("path")
    if not path_s:
        raise HTTPException(400, "'path' required")
    storage = get_storage()
    p = Path(path_s).expanduser().resolve()
    inside = False
    for root in storage.roots.values():
        try:
            p.relative_to(root.resolve())
            inside = True
            break
        except ValueError:
            continue
    if not inside:
        raise HTTPException(403, (
            "Path is outside the app's storage roots. Use the upload endpoint "
            "or move the file into the studio storage folder (originals are never modified)."))
    for issue in validate_file(p):
        if issue.severity == "error":
            raise HTTPException(404, issue.message)
    probe = get_ffprobe()
    md = await probe.probe(p)
    ok, issues = validate_probe(md)
    media_id = body.get("id") or new_id("media")
    get_db().upsert_media(media_id, str(p), p.name, p.stat().st_size, p.stat().st_mtime, md)
    return {"media_id": media_id, "metadata": md, "valid": ok, "issues": issues}


@router.get("")
async def list_media() -> Dict[str, Any]:
    return {"media": get_db().list_media()}


@router.get("/{media_id}")
async def get_media_info(media_id: str) -> Dict[str, Any]:
    row = _require_media(media_id)
    row.pop("original_path", None)  # do not leak arbitrary disk paths
    return row


@router.delete("/{media_id}")
async def delete_media(media_id: str) -> Dict[str, Any]:
    """Remove generated proxies/temp only — never the original (P2-10, GR-07)."""
    row = _require_media(media_id)
    storage = get_storage()
    removed = 0
    proxy_dir = storage.proxy_dir(media_id)
    if proxy_dir.exists():
        removed = storage.dir_size(proxy_dir)
        storage.remove_tree(proxy_dir)
    get_db().delete_media(media_id)
    log.info("media deleted (proxies only) %s freed %d bytes", media_id, removed,
             extra={"event": "media_delete", "media_id": media_id})
    return {"deleted": True, "original_kept": True,
            "bytes_freed": removed, "original": row.get("original_name")}


@router.post("/{media_id}/proxy")
async def generate_proxy(media_id: str, request: Request) -> Dict[str, Any]:
    row = _require_media(media_id)
    md = row["metadata"]
    ua = request.headers.get("user-agent", "")
    device = device_class(ua)
    rung = decide_proxy(md, device)
    if rung is None:
        return {"status": "not_needed", "reason": "device handles the original (P2-15)"}
    gen = ProxyGenerator()
    result = await gen.generate(Path(row["original_path"]), media_id, rung, md, device)
    if result.get("status") == "ok":
        proxies = row.get("proxies") or {}
        proxies[str(result["rung"])] = {
            "path": result["path"], "size": result["size"], "metadata": result["metadata"]}
        get_db().update_media(media_id, proxies=proxies)
    return result


@router.get("/{media_id}/file")
async def get_media_file(media_id: str,
                         variant: str = Query("original", pattern="^(original|proxy_\\d+p)$"),
                         range_header: Optional[str] = Header(None, alias="Range")):
    """Range-capable streaming so the browser can seek during preview."""
    row = _require_media(media_id)
    path: Optional[Path] = None
    if variant == "original":
        path = Path(row["original_path"])
    else:
        rung = variant.replace("proxy_", "").replace("p", "")
        proxy_info = (row.get("proxies") or {}).get(rung)
        path = Path(proxy_info["path"]) if proxy_info else None
    if not path or not path.exists():
        raise HTTPException(404, f"variant '{variant}' not available")
    stat = path.stat()
    total = stat.st_size
    mime = "video/mp4" if path.suffix.lower() in (".mp4", ".m4v", ".mov") else \
        "video/webm" if path.suffix.lower() == ".webm" else \
        "audio/mpeg" if path.suffix.lower() in (".mp3", ".m4a", ".aac") else \
        "image/png" if path.suffix.lower() == ".png" else "application/octet-stream"

    def iter_file(start: int, end: int):
        with path.open("rb") as fh:  # type: ignore[attr-defined]
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fh.read(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    if range_header:
        try:
            unit, _, rng = range_header.partition("=")
            start_s, _, end_s = rng.partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else total - 1
        except ValueError:
            raise HTTPException(416, "invalid range")
        start = max(0, min(start, total - 1))
        end = max(start, min(end, total - 1))
        return StreamingResponse(
            iter_file(start, end), status_code=206,
            media_type=mime,
            headers={
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(end - start + 1),
            })
    return StreamingResponse(
        iter_file(0, total - 1), media_type=mime,
        headers={"Accept-Ranges": "bytes", "Content-Length": str(total)})
