"""SQLite persistence for the project index and job metadata (P6-15).

Schema is versioned from day one (GR-20). Access is serialized behind a lock
because workers run in background threads while the API serves requests.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import get_config
from app.core.logging import get_logger

log = get_logger("database")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    dir_path TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    progress_json TEXT NOT NULL DEFAULT '{}',
    log_path TEXT,
    output_path TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id);
CREATE TABLE IF NOT EXISTS media (
    id TEXT PRIMARY KEY,
    original_path TEXT NOT NULL,
    original_name TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    proxies_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
"""


class Database:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        if db_path is None:
            db_path = get_config().storage_root() / "studio.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()
            ver = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            if int(ver) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema v{ver} is newer than supported v{SCHEMA_VERSION}; upgrade the app"
                )

    # -------------------------------------------------------------- helpers
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- projects
    def upsert_project(self, project_id: str, name: str, dir_path: str) -> None:
        now = time.time()
        self._exec(
            """INSERT INTO projects(id, name, dir_path, created_at, updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                                             dir_path=excluded.dir_path,
                                             updated_at=excluded.updated_at""",
            (project_id, name, dir_path, now, now),
        )

    def list_projects(self) -> List[Dict[str, Any]]:
        rows = self._query("SELECT * FROM projects ORDER BY updated_at DESC")
        return [dict(r) for r in rows]

    def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM projects WHERE id=?", (project_id,))
        return dict(rows[0]) if rows else None

    def delete_project(self, project_id: str) -> None:
        self._exec("DELETE FROM projects WHERE id=?", (project_id,))

    # ----------------------------------------------------------------- jobs
    def insert_job(self, job: Dict[str, Any]) -> None:
        self._exec(
            """INSERT INTO jobs(id, project_id, kind, state, params_json, progress_json,
                                log_path, output_path, attempts, error, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job["id"], job.get("project_id"), job["kind"], job["state"],
                json.dumps(job.get("params", {})), json.dumps(job.get("progress", {})),
                job.get("log_path"), job.get("output_path"), job.get("attempts", 0),
                job.get("error"), job.get("created_at", time.time()), time.time(),
            ),
        )

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        column_map = {"params": "params_json", "progress": "progress_json"}
        sets, params = [], []
        for key, val in fields.items():
            col = column_map.get(key, key)
            if key in ("params", "progress"):
                val = json.dumps(val)
            sets.append(f"{col}=?")
            params.append(val)
        sets.append("updated_at=?")
        params.append(time.time())
        params.append(job_id)
        self._exec(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", tuple(params))

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not rows:
            return None
        row = dict(rows[0])
        row["params"] = json.loads(row.pop("params_json") or "{}")
        row["progress"] = json.loads(row.pop("progress_json") or "{}")
        return row

    def list_jobs(self, project_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        if project_id:
            rows = self._query(
                "SELECT * FROM jobs WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            )
        else:
            rows = self._query("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["params"] = json.loads(d.pop("params_json") or "{}")
            d["progress"] = json.loads(d.pop("progress_json") or "{}")
            out.append(d)
        return out

    # ---------------------------------------------------------------- media
    def upsert_media(self, media_id: str, original_path: str, original_name: str,
                     size_bytes: int, mtime: float, metadata: Dict[str, Any]) -> None:
        self._exec(
            """INSERT INTO media(id, original_path, original_name, size_bytes, mtime,
                                 metadata_json, proxies_json, created_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET original_path=excluded.original_path,
                                             original_name=excluded.original_name,
                                             size_bytes=excluded.size_bytes,
                                             mtime=excluded.mtime,
                                             metadata_json=excluded.metadata_json""",
            (media_id, original_path, original_name, size_bytes, mtime,
             json.dumps(metadata), "{}", time.time()),
        )

    def get_media(self, media_id: str) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM media WHERE id=?", (media_id,))
        if not rows:
            return None
        d = dict(rows[0])
        d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
        d["proxies"] = json.loads(d.pop("proxies_json") or "{}")
        return d

    def list_media(self) -> List[Dict[str, Any]]:
        rows = self._query("SELECT * FROM media ORDER BY created_at DESC")
        out = []
        for r in rows:
            d = dict(r)
            d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
            d["proxies"] = json.loads(d.pop("proxies_json") or "{}")
            out.append(d)
        return out

    def update_media(self, media_id: str, metadata: Optional[Dict[str, Any]] = None,
                     proxies: Optional[Dict[str, Any]] = None) -> None:
        cur = self.get_media(media_id)
        if not cur:
            return
        md = json.dumps(metadata) if metadata is not None else json.dumps(cur["metadata"])
        px = json.dumps(proxies) if proxies is not None else json.dumps(cur["proxies"])
        self._exec(
            "UPDATE media SET metadata_json=?, proxies_json=?, mtime=? WHERE id=?",
            (md, px, cur.get("mtime", 0), media_id),
        )

    def delete_media(self, media_id: str) -> None:
        self._exec("DELETE FROM media WHERE id=?", (media_id,))


_db: Optional[Database] = None
_db_lock = threading.Lock()


def get_db() -> Database:
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                _db = Database()
    return _db


def reset_db() -> None:  # tests
    global _db
    if _db is not None:
        _db.close()
        _db = None
