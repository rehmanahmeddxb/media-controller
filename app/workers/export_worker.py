"""Background export worker (P9-06, P9-08).

Runs render jobs off the API request path. Concurrency policy: one heavy
render at a time on Termux-class devices, configurable ceiling on Windows.
The worker loop is started during app lifespan and stopped on shutdown.
"""
from __future__ import annotations

import os
import platform
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.config import get_config
from app.core.logging import get_logger
from app.core.storage import get_storage
from app.media.renderer import FinalRenderer, RenderCancelled
from app.workers.job_manager import Job, JobManager, JobState, get_job_manager

log = get_logger("export_worker")


def is_termux() -> bool:
    return "com.termux" in os.environ.get("PREFIX", "") or \
        platform.system() == "Linux" and os.path.exists("/data/data/com.termux")


class ExportWorker:
    def __init__(self, manager: Optional[JobManager] = None) -> None:
        self.manager = manager or get_job_manager()
        self.cfg = get_config()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._running_jobs: set[str] = set()
        self._lock = threading.Lock()
        # Termux: exactly one heavy job; Windows: configurable ceiling (P9-08)
        self.concurrency = 1 if is_termux() else max(1, self.cfg.export.concurrency_heavy_jobs)
        self.manager.register_executor("export", self.execute_export)
        self.renderer = FinalRenderer()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        for i in range(self.concurrency):
            t = threading.Thread(target=self._loop, name=f"export-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("export worker started (%d lane(s))", self.concurrency,
                 extra={"event": "worker_start"})

    def stop(self) -> None:
        self._stop.set()
        for _ in self._threads:
            self._queue.put(None)

    def enqueue(self, job_id: str) -> None:
        self._queue.put(job_id)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job_id is None:
                break
            job = self.manager.get(job_id)
            if job is None:
                continue
            with self._lock:
                self._running_jobs.add(job_id)
            try:
                self.manager.execute(job)
            finally:
                with self._lock:
                    self._running_jobs.discard(job_id)
                get_storage().sweep_temp(0.0)  # post-job temp sweep (P1-18)

    # --------------------------------------------------------------- export
    def execute_export(self, job: Job) -> Dict[str, Any]:
        params = job.params
        settings: Dict[str, Any] = params.get("settings") or {}
        project: Dict[str, Any] = params.get("project") or {}
        fmt = (settings.get("format") or "mp4").lower()
        base_name = (params.get("name") or project.get("name") or "export")
        safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in base_name).strip() or "export"
        out = get_storage().collision_free(
            get_storage().roots["exports"] / f"{safe}_{time.strftime('%Y%m%d_%H%M%S')}.{fmt}")

        def on_progress(data: Dict[str, Any]) -> None:
            job.set_progress(data)
            stage = data.get("stage", "")
            if stage and stage != job.progress.get("_last_stage"):
                job.progress["_last_stage"] = stage
                job.append_log(f"{stage} — {data.get('pct', 0):.0f}%")

        result = self.renderer.render_take(
            project=project,
            timeline_events=params.get("timeline") or [],
            layers_snapshot=params.get("layers") or [],
            take_start_ms=float(params.get("take_start_ms") or 0),
            take_end_ms=float(params.get("take_end_ms") or 0),
            source_map=params.get("source_map") or {},
            composite_recording=params.get("composite_recording"),
            settings=settings,
            output_path=out,
            job_id=job.id,
            on_progress=on_progress,
            cancel_check=lambda: job.cancel_requested,
        )
        job.output_path = result.get("output")
        return result


_worker: Optional[ExportWorker] = None
_worker_lock = threading.Lock()


def get_export_worker() -> ExportWorker:
    global _worker
    if _worker is None:
        with _worker_lock:
            if _worker is None:
                _worker = ExportWorker()
    return _worker
