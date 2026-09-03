"""Project persistence API (P6-16 … P6-24).

Project JSON schema (§26, GR-20):
    { "version": 1, "canvas": {...}, "layers": [...], "timeline": [...],
      "audio": {...}, "export": {...} }

Atomic writes + debounced autosave happen client-side; snapshots rotate with
a keep-N policy (P6-19). DELETE removes project data only — never sources.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from app.core.config import get_config
from app.core.database import get_db
from app.core.logging import get_logger
from app.core.recovery import get_recovery
from app.core.storage import PathEscapeError, get_storage, new_id, sanitize_filename

router = APIRouter(prefix="/api/projects", tags=["projects"])
log = get_logger("api.projects")

PROJECT_VERSION = 1


def _new_project_body(name: str) -> Dict[str, Any]:
    return {
        "version": PROJECT_VERSION,
        "name": name,
        "canvas": {"aspect": "16:9", "width": 1920, "height": 1080, "fps": 30,
                   "background": "black"},
        "layers": [],
        "timeline": [],
        "audio": {"master_volume": 1.0},
        "export": {
            "format": get_config().export.default_format,
            "resolution": get_config().export.default_resolution,
            "fps": get_config().export.default_fps,
        },
    }


def _project_path(project_id: str) -> Path:
    storage = get_storage()
    d = storage.project_dir(project_id)
    return d / "project.json"


def _snapshot_rotate(project_id: str) -> None:
    """Keep the N most recent snapshots; never overwrite last-known-good (P6-19)."""
    storage = get_storage()
    d = storage.project_dir(project_id)
    snaps = d / "snapshots"
    if not snaps.exists():
        return
    keep = max(2, get_config().recovery.snapshot_keep)
    files = sorted(snaps.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def _migrate(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Schema migrations — version-gated, refuse unknown future versions (P6-20)."""
    ver = int(doc.get("version") or 1)
    if ver > PROJECT_VERSION:
        raise HTTPException(422, (
            f"Project was saved by a newer version (v{ver}); this build supports v{PROJECT_VERSION}."))
    # v1 is current
    doc.setdefault("canvas", _new_project_body("x")["canvas"])
    doc.setdefault("layers", [])
    doc.setdefault("timeline", [])
    doc.setdefault("audio", {"master_volume": 1.0})
    doc.setdefault("export", {})
    doc["version"] = PROJECT_VERSION
    return doc


@router.post("")
async def create_project(request: Request) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    name = str(body.get("name") or f"Project {time.strftime('%Y-%m-%d %H:%M')}")
    project_id = new_id("proj")
    storage = get_storage()
    pdir = storage.project_dir(project_id)
    pdir.mkdir(parents=True, exist_ok=True)
    doc = _new_project_body(name)
    doc["id"] = project_id
    storage.atomic_write_json(_project_path(project_id), doc)
    get_db().upsert_project(project_id, name, str(pdir))
    get_recovery().write_pointer(project_id, str(_project_path(project_id)))
    log.info("project created %s", project_id, extra={"event": "project_create", "project_id": project_id})
    return {"project_id": project_id, "project": doc}


@router.get("")
async def list_projects() -> Dict[str, Any]:
    rows = get_db().list_projects()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({"id": r["id"], "name": r["name"], "updated_at": r["updated_at"]})
    return {"projects": out}


@router.get("/{project_id}")
async def load_project(project_id: str) -> Dict[str, Any]:
    p = _project_path(project_id)
    doc = get_storage().read_json(p)
    if doc is None:
        raise HTTPException(404, f"project '{project_id}' not found")
    doc = _migrate(doc)
    return {"project_id": project_id, "project": doc}


@router.put("/{project_id}")
async def save_project(project_id: str, request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "invalid JSON body") from exc
    doc = body.get("project") or body
    if not isinstance(doc, dict):
        raise HTTPException(400, "'project' object required")
    doc = _migrate(doc)
    doc["id"] = project_id
    storage = get_storage()
    p = _project_path(project_id)
    if not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        get_db().upsert_project(project_id, doc.get("name", project_id), str(p.parent))
    # snapshot current before overwrite (P6-19)
    snaps = p.parent / "snapshots"
    snaps.mkdir(exist_ok=True)
    if p.exists():
        shutil.copy2(p, snaps / f"snap_{int(time.time()*1000)}.json")
    storage.atomic_write_json(p, doc)
    _snapshot_rotate(project_id)
    get_db().upsert_project(project_id, doc.get("name", project_id), str(p.parent))
    recovery = get_recovery()
    recovery.write_pointer(project_id, str(p))
    recovery.clear_dirty()
    return {"saved": True, "saved_at": time.time(), "project_id": project_id}


@router.delete("/{project_id}")
async def delete_project(project_id: str) -> Dict[str, Any]:
    """Deletes project data ONLY — sources and recordings are never touched (P6-17, GR-07)."""
    storage = get_storage()
    try:
        pdir = storage.project_dir(project_id)
    except PathEscapeError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not pdir.exists():
        raise HTTPException(404, f"project '{project_id}' not found")
    storage.remove_tree(pdir)
    get_db().delete_project(project_id)
    log.info("project deleted %s (data only)", project_id,
             extra={"event": "project_delete", "project_id": project_id})
    return {"deleted": True, "sources_kept": True}


@router.post("/{project_id}/dirty")
async def mark_dirty(project_id: str) -> Dict[str, Any]:
    """Autosave heartbeat: session is dirty until the next save (P6-18, P1-19)."""
    recovery = get_recovery()
    pointer = recovery.read_pointer()
    if not pointer or pointer.get("project_id") != project_id:
        p = _project_path(project_id)
        if p.exists():
            recovery.write_pointer(project_id, str(p))
    recovery.mark_dirty()
    return {"dirty": True}
