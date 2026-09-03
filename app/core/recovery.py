"""Crash-recovery support (P1-19 … P1-21, P6-21).

Keeps a ``last_project.json`` pointer plus a dirty flag in storage root.
On boot, if the previous session died with unsaved state, the last safe
snapshot is re-opened. Attempts are bounded (GR-13) — after ``max_attempts``
the app reports a clear user-facing message and starts clean instead of
looping. Originals are never touched (GR-07): recovery only reads project
JSON files.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.config import AppConfig, get_config
from app.core.logging import get_logger, log_diag
from app.core.storage import StorageManager

log = get_logger("recovery")


class RecoveryManager:
    def __init__(self, cfg: Optional[AppConfig] = None, storage: Optional[StorageManager] = None) -> None:
        self.cfg = cfg or get_config()
        self.storage = storage or StorageManager(self.cfg)
        self.root = self.cfg.storage_root()
        self.pointer = self.root / "last_project.json"
        self.dirty = self.root / "session_dirty"
        self.counter = self.root / "recovery_attempts"
        self.max_attempts = max(1, self.cfg.recovery.max_attempts)

    # ---------------------------------------------------------------- pointer
    def write_pointer(self, project_id: str, project_path: str) -> None:
        """Called on every state change (P1-19)."""
        self.storage.atomic_write_json(self.pointer, {
            "project_id": project_id,
            "project_path": project_path,
            "saved_at": time.time(),
        })

    def read_pointer(self) -> Optional[Dict[str, Any]]:
        return self.storage.read_json(self.pointer)

    def mark_dirty(self) -> None:
        try:
            self.dirty.write_text("1", encoding="utf-8")
        except OSError:
            pass

    def clear_dirty(self) -> None:
        try:
            self.dirty.unlink(missing_ok=True)
        except OSError:
            pass

    def is_dirty(self) -> bool:
        return self.dirty.exists()

    # --------------------------------------------------------------- attempts
    def _attempt_count(self) -> int:
        try:
            return int(self.counter.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            return 0

    def _bump_attempts(self) -> int:
        n = self._attempt_count() + 1
        try:
            self.counter.write_text(str(n), encoding="utf-8")
        except OSError:
            pass
        return n

    def _reset_attempts(self) -> None:
        try:
            self.counter.unlink(missing_ok=True)
        except OSError:
            pass

    # ----------------------------------------------------------------- boot
    def recover(self) -> Dict[str, Any]:
        """Boot recovery. Returns a status dict for /api/health and the UI.

        Never raises, never loops: a single bounded pass per boot (P1-20/21).
        """
        result: Dict[str, Any] = {
            "recovered": False,
            "project_id": None,
            "message": "clean session",
            "attempts": 0,
        }
        if not self.is_dirty():
            self._reset_attempts()
            return result

        attempts = self._attempt_count()
        if attempts >= self.max_attempts:
            log_diag(
                f"Recovery gave up after {attempts} attempts; starting a fresh session. "
                "Your media files were not touched.",
                level="ERROR",
            )
            log.error("recovery attempt cap reached", extra={"event": "recovery_cap", "attempt": attempts})
            self.clear_dirty()
            self._reset_attempts()
            result["message"] = (
                f"Recovery stopped after {attempts} attempts — starting fresh. "
                "No original media was modified."
            )
            return result

        n = self._bump_attempts()
        result["attempts"] = n
        pointer = self.read_pointer()
        if not pointer or not pointer.get("project_path"):
            self.clear_dirty()
            self._reset_attempts()
            result["message"] = "dirty session had no last-project pointer; started fresh"
            return result

        project_path = Path(pointer["project_path"])
        snapshot = self.storage.read_json(project_path)
        if snapshot is None:
            # fall back to the newest snapshot next to the project file (P6-19)
            candidates = sorted(project_path.parent.glob("snapshots/*.json"), reverse=True)
            for cand in candidates[:3]:
                snapshot = self.storage.read_json(cand)
                if snapshot is not None:
                    break
        if snapshot is None:
            log_diag("Last project could not be recovered; started a fresh session.", level="ERROR")
            log.warning("recovery found unreadable last project %s", project_path,
                        extra={"event": "recovery_failed", "path": str(project_path)})
            self.clear_dirty()
            result["message"] = "last project snapshot unreadable; started fresh"
            return result

        self.clear_dirty()
        self._reset_attempts()
        result.update({
            "recovered": True,
            "project_id": pointer.get("project_id"),
            "snapshot": snapshot,
            "message": "recovered last safe project snapshot",
        })
        log.info("recovered project %s", pointer.get("project_id"),
                 extra={"event": "recovery_ok", "project_id": pointer.get("project_id")})
        log_diag("Recovered your last session automatically.")
        return result


_recovery: Optional[RecoveryManager] = None


def get_recovery() -> RecoveryManager:
    global _recovery
    if _recovery is None:
        _recovery = RecoveryManager()
    return _recovery
