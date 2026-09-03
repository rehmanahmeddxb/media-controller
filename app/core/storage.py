"""Storage manager — safe, atomic filesystem access (P1-13 … P1-18).

Guarantees:
  * every path resolved and asserted to stay inside a configured root (GR-18)
  * filename sanitization before touching disk (GR-17)
  * atomic JSON writes (temp -> fsync -> rename)
  * free-space queries, temp-space estimation, output writability checks
  * temp-file lifetime management (TTL sweep)

Original media is never modified, moved or deleted by anything here (GR-07).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import string
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import AppConfig, get_config
from app.core.logging import get_logger

log = get_logger("storage")

# Windows reserved device names (also dangerous when used bare on Linux for SMB shares)
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_UNSAFE_CHARS = re.compile(r"[^\w\-. ()\[\]#@&+,']")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_filename(name: str, fallback: str = "file") -> str:
    """Sanitize a client-supplied filename (P1-15).

    Strips directory separators, control characters, reserved Windows names,
    leading dots/spaces and overlong names. Never returns an empty string.
    """
    name = (name or "").replace("\\", "/")
    base = name.rsplit("/", 1)[-1]  # drop any path components
    base = _CONTROL_CHARS.sub("", base)
    base = _UNSAFE_CHARS.sub("_", base)
    base = base.strip(" .")
    stem, dot, ext = base.partition(".")
    stem = stem.strip() or fallback
    if dot:
        ext = _CONTROL_CHARS.sub("", ext)[:16]
        base = f"{stem}.{ext}"
    else:
        base = stem
    # reserved Windows device names apply to the bare stem (e.g. "COM1.mp4")
    if stem.upper() in _WINDOWS_RESERVED or base.upper() in _WINDOWS_RESERVED:
        base = f"_{base}"
    if len(base) > 180:
        stem = base[:160].rstrip(" .")
        ext = base.rsplit(".", 1)[-1] if "." in base else ""
        base = f"{stem}.{ext}" if ext and len(ext) <= 12 else stem
    return base


def new_id(prefix: str = "") -> str:
    """Random, unguessable identifier for media/jobs/takes (security §23)."""
    raw = uuid.uuid4().hex[:12]
    return f"{prefix}_{raw}" if prefix else raw


class StorageError(Exception):
    pass


class PathEscapeError(StorageError):
    """A requested path attempted to escape its configured root."""


class StorageManager:
    def __init__(self, cfg: Optional[AppConfig] = None) -> None:
        self.cfg = cfg or get_config()
        self.roots = {which: self.cfg.subroot(which) for which in
                      ("projects", "proxies", "recordings", "exports", "temp", "logs")}

    # ------------------------------------------------------------------ paths
    def safe_resolve(self, which: str, *parts: str) -> Path:
        """Resolve ``root/(*parts)`` and assert containment (P1-14, GR-18)."""
        root = self.roots[which]
        candidate = Path(root, *parts)
        resolved = candidate.resolve()
        root_resolved = root.resolve()
        try:
            resolved.relative_to(root_resolved)
        except ValueError as exc:
            raise PathEscapeError(
                f"path {candidate} escapes storage root {root}"
            ) from exc
        return resolved

    def media_dir(self, media_id: str) -> Path:
        media_id = sanitize_filename(media_id, fallback="media")
        return self.safe_resolve("proxies", media_id)

    def proxy_dir(self, media_id: str) -> Path:
        return self.media_dir(media_id)

    def project_dir(self, project_id: str) -> Path:
        project_id = sanitize_filename(project_id, fallback="project")
        return self.safe_resolve("projects", project_id)

    def recording_dir(self, project_id: str, take_id: str) -> Path:
        project_id = sanitize_filename(project_id, fallback="project")
        take_id = sanitize_filename(take_id, fallback="take")
        return self.safe_resolve("recordings", project_id, take_id)

    def export_path(self, filename: str) -> Path:
        return self.safe_resolve("exports", sanitize_filename(filename))

    def temp_dir(self, label: str = "work") -> Path:
        label = sanitize_filename(label, fallback="work")
        d = self.safe_resolve("temp", f"{label}_{new_id()}")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def collision_free(self, path: Path) -> Path:
        """Return a path that does not exist yet (name-1, name-2 …)."""
        if not path.exists():
            return path
        stem, suffix = path.stem, path.suffix
        parent = path.parent
        for i in range(1, 10_000):
            candidate = parent / f"{stem}-{i}{suffix}"
            if not candidate.exists():
                return candidate
        raise StorageError(f"cannot find collision-free name for {path}")

    # ------------------------------------------------------------- disk usage
    def free_space(self, which: str = "exports") -> int:
        """Free bytes on the filesystem holding the given root (P1-16)."""
        root = self.roots[which]
        usage = shutil.disk_usage(root)
        return usage.free

    def dir_size(self, path: Path) -> int:
        total = 0
        if not path.exists():
            return 0
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
        return total

    def estimate_temp_space(self, source_sizes: List[int], factor: float = 1.5) -> int:
        """Rough temp-space need for a render job (P1-16, P9-29)."""
        return int(sum(source_sizes) * factor) + 256 * 1024 * 1024

    def check_output_dir(self, which: str = "exports") -> Tuple[bool, str]:
        """Verify the output directory exists and is writable (P1-16)."""
        root = self.roots[which]
        if not root.exists():
            return False, f"output root {root} does not exist"
        try:
            probe = root / f".write_probe_{new_id()}"
            probe.write_bytes(b"ok")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            return False, f"output root {root} not writable: {exc}"
        return True, "ok"

    # ----------------------------------------------------------- atomic writes
    def atomic_write_json(self, path: Path, data: Any) -> None:
        """Write JSON atomically: temp file -> fsync -> rename (P1-17)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def read_json(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            log.warning("corrupt json %s: %s", path, exc, extra={"event": "corrupt_json", "path": str(path)})
            return None

    # ------------------------------------------------------------ temp sweeper
    def sweep_temp(self, max_age_hours: float = 24.0) -> int:
        """Delete temp artifacts older than TTL (P1-18). Boot + post-job."""
        temp_root = self.roots["temp"]
        cutoff = time.time() - max_age_hours * 3600
        removed = 0
        if not temp_root.exists():
            return 0
        for entry in temp_root.iterdir():
            if entry.name == ".gitkeep":
                continue
            try:
                mtime = entry.stat().st_mtime
                if mtime < cutoff:
                    if entry.is_dir():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        if removed:
            log.info("temp sweep removed %d entries", removed, extra={"event": "temp_sweep", "path": str(temp_root)})
        return removed

    def remove_tree(self, path: Path) -> None:
        """Remove generated artifacts only. Refuses paths outside roots."""
        resolved = path.resolve()
        for root in self.roots.values():
            try:
                resolved.relative_to(root.resolve())
                if resolved == root.resolve():
                    raise StorageError("refusing to remove a storage root itself")
                break
            except ValueError:
                continue
        else:
            raise PathEscapeError(f"refusing to remove {path}: outside storage roots")
        if resolved.is_dir():
            shutil.rmtree(resolved, ignore_errors=True)
        elif resolved.exists():
            resolved.unlink(missing_ok=True)


_storage: Optional[StorageManager] = None


def get_storage() -> StorageManager:
    global _storage
    if _storage is None:
        _storage = StorageManager()
    return _storage


def reset_storage() -> None:  # tests
    global _storage
    _storage = None
