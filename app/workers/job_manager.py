"""Job manager — states, retries, cancellation, persistence (P9-01 … P9-09).

States:
    QUEUED -> PREPARING -> RUNNING -> VALIDATING -> COMPLETED
    RUNNING -> RECOVERING -> RETRY -> (RUNNING | FAILED)
    any -> CANCELLING -> CANCELLED

Bounded retries only (GR-13). Random job ids (security §23). Jobs survive a
server restart via SQLite; orphaned jobs are swept on boot (P9-28).
"""
from __future__ import annotations

import threading
import time
import uuid
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from app.core.config import get_config
from app.core.database import Database, get_db
from app.core.logging import get_logger, log_diag
from app.core.storage import get_storage

log = get_logger("jobs")


class JobState(str, Enum):
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    VALIDATING = "VALIDATING"
    RECOVERING = "RECOVERING"
    CANCELLED = "CANCELLED"
    CANCELLING = "CANCELLING"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"  # discovered orphaned on boot


ACTIVE_STATES = {JobState.QUEUED, JobState.PREPARING, JobState.RUNNING,
                 JobState.VALIDATING, JobState.RECOVERING, JobState.CANCELLING}


class Job:
    def __init__(self, kind: str, params: Dict[str, Any],
                 project_id: Optional[str] = None, job_id: Optional[str] = None) -> None:
        self.id = job_id or f"job_{uuid.uuid4().hex[:10]}"  # P9-05 random ids
        self.kind = kind
        self.project_id = project_id
        self.params = params
        self.state = JobState.QUEUED
        self.progress: Dict[str, Any] = {"pct": 0, "stage": "QUEUED"}
        self.attempts = 0
        self.error: Optional[str] = None
        self.created_at = time.time()
        self.updated_at = time.time()
        self.log_lines: List[str] = []
        self.output_path: Optional[str] = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- state
    def set_state(self, state: JobState) -> None:
        with self._lock:
            self.state = state
            self.updated_at = time.time()

    def set_progress(self, data: Dict[str, Any]) -> None:
        with self._lock:
            prev_pct = float(self.progress.get("pct") or 0)
            # monotonic percentage (P9-E1)
            pct = float(data.get("pct") or 0)
            self.progress = {**data, "pct": max(prev_pct, pct)}

    def append_log(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self.log_lines.append(f"[{stamp}] {line}")
            if len(self.log_lines) > 500:
                self.log_lines = self.log_lines[-400:]

    # ------------------------------------------------------------ lifecycle
    def request_cancel(self) -> None:
        self._cancel.set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "project_id": self.project_id,
            "state": self.state.value, "progress": dict(self.progress),
            "attempts": self.attempts, "error": self.error,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "output_path": self.output_path,
            "log_tail": self.log_lines[-30:],
        }


class JobManager:
    """Thread-safe job registry driving the export worker."""

    def __init__(self, db: Optional[Database] = None) -> None:
        self.db = db or get_db()
        self.cfg = get_config()
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.RLock()
        self._executors: Dict[str, Callable[[Job], Dict[str, Any]]] = {}
        self._max_attempts = max(1, self.cfg.export.max_attempts)

    # ------------------------------------------------------------ registry
    def register_executor(self, kind: str, fn: Callable[[Job], Dict[str, Any]]) -> None:
        self._executors[kind] = fn

    def submit(self, kind: str, params: Dict[str, Any],
               project_id: Optional[str] = None) -> Job:
        job = Job(kind, params, project_id)
        with self._lock:
            self._jobs[job.id] = job
            self.db.insert_job({
                "id": job.id, "project_id": project_id, "kind": kind,
                "state": job.state.value, "params": params,
                "progress": job.as_dict()["progress"], "attempts": 0,
                "created_at": job.created_at,
            })
        log.info("job submitted %s (%s)", job.id, kind,
                 extra={"event": "job_submit", "job_id": job.id})
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            live = [j.as_dict() for j in self._jobs.values()
                    if project_id is None or j.project_id == project_id]
        known = {j["id"] for j in live}
        for row in self.db.list_jobs(project_id, limit=50):
            if row["id"] not in known:
                live.append({**row, "log_tail": []})
        live.sort(key=lambda j: j["created_at"], reverse=True)
        return live

    # ------------------------------------------------------------ execution
    def execute(self, job: Job) -> Dict[str, Any]:
        """Run a job to completion with bounded retries (P9-02/04)."""
        executor = self._executors.get(job.kind)
        if executor is None:
            job.set_state(JobState.FAILED)
            job.error = f"no executor registered for kind '{job.kind}'"
            self._persist(job)
            return {"ok": False, "error": job.error}

        while True:
            job.attempts += 1
            job._cancel.clear()
            try:
                job.set_state(JobState.RUNNING)
                job.append_log(f"attempt {job.attempts} starting")
                self._persist(job)
                result = executor(job)
                if job.cancel_requested:
                    self._mark_cancelled(job)
                    return {"ok": False, "cancelled": True}
                job.output_path = result.get("output")
                job.set_state(JobState.COMPLETED)
                job.append_log("completed")
                self._persist(job)
                return {"ok": True, "result": result}
            except _Cancelled:
                self._mark_cancelled(job)
                return {"ok": False, "cancelled": True}
            except Exception as exc:  # noqa: BLE001 — worker boundary
                if job.cancel_requested:
                    self._mark_cancelled(job)
                    return {"ok": False, "cancelled": True}
                log.error("job %s failed attempt %d: %s", job.id, job.attempts, exc,
                          extra={"event": "job_error", "job_id": job.id, "attempt": job.attempts})
                job.append_log(f"error: {exc}")
                from app.media.renderer import RenderValidationError
                deterministic = isinstance(exc, RenderValidationError)
                if deterministic or job.attempts >= self._max_attempts:
                    job.set_state(JobState.FAILED)
                    job.error = str(exc)[:800]
                    self._persist(job)
                    log_diag("Export failed", job=job.id, error=str(exc)[:120], level="ERROR")
                    return {"ok": False, "error": job.error}
                job.set_state(JobState.RECOVERING)
                self._persist(job)
                time.sleep(min(2 ** job.attempts, 10))  # bounded backoff

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            return False
        if job.state in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED):
            return False
        job.set_state(JobState.CANCELLING)
        job.request_cancel()
        job.append_log("cancellation requested")
        self._persist(job)
        return True

    def _mark_cancelled(self, job: Job) -> None:
        job.set_state(JobState.CANCELLED)
        job.append_log("cancelled — temporary files cleaned")
        self._persist(job)
        log.info("job cancelled %s", job.id, extra={"event": "job_cancelled", "job_id": job.id})

    def _persist(self, job: Job) -> None:
        with self._lock:
            try:
                self.db.update_job(
                    job.id, state=job.state.value, progress=job.progress,
                    attempts=job.attempts, error=job.error, output_path=job.output_path,
                )
            except Exception:  # noqa: BLE001 — DB errors must not kill renders
                log.exception("failed to persist job %s", job.id)

    # ------------------------------------------------------------ boot sweep
    def sweep_orphans(self) -> int:
        """Mark jobs that were mid-flight during a crash (P9-28, P10-12)."""
        swept = 0
        for row in self.db.list_jobs(limit=200):
            if row["state"] in ACTIVE_STATES:
                self.db.update_job(row["id"], state=JobState.INTERRUPTED.value,
                                   error="server restarted while this job was running")
                swept += 1
                log.warning("orphaned job swept %s", row["id"],
                            extra={"event": "job_orphaned", "job_id": row["id"]})
        if swept:
            self.storage_sweep()
            log_diag(f"{swept} interrupted export(s) cleaned up after restart.", level="WARN")
        return swept

    def storage_sweep(self) -> None:
        try:
            get_storage().sweep_temp(0.0)
        except Exception:  # noqa: BLE001
            log.exception("temp sweep failed")


class _Cancelled(Exception):
    pass


_manager: Optional[JobManager] = None


def get_job_manager() -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager


def reset_job_manager() -> None:  # tests
    global _manager
    _manager = None
