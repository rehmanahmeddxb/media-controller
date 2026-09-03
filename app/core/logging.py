"""Structured logging for Ahmed Reaction Studio.

Two channels (P1-11, P1-12):
  * technical log — JSON lines, rotating file handler in storage/logs/app.log
  * diagnostics log — concise, user-facing lines in storage/logs/diagnostics.log

No network handlers, fully local.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.config import AppConfig, get_config

_TECH_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DIAG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Structured extras (log.info("...", extra={"ffmpeg": path}))
        for key in ("event", "job_id", "media_id", "project_id", "layer_id", "path",
                    "exit_code", "duration_ms", "state", "attempt", "device_id"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _resolve_level(name: str) -> int:
    return getattr(logging, name.upper(), logging.INFO)


def setup_logging(cfg: Optional[AppConfig] = None) -> None:
    cfg = cfg or get_config()
    logs_dir = cfg.subroot("logs")

    root = logging.getLogger("ars")
    root.setLevel(_resolve_level(cfg.logging.level))
    # idempotent re-setup
    for h in list(root.handlers):
        root.removeHandler(h)

    tech_file = logging.handlers.RotatingFileHandler(
        logs_dir / "app.log",
        maxBytes=cfg.logging.max_bytes,
        backupCount=cfg.logging.backups,
        encoding="utf-8",
    )
    tech_file.setFormatter(JsonLineFormatter())
    root.addHandler(tech_file)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(JsonLineFormatter())
    root.addHandler(console)

    # Separate user-facing diagnostics channel (P1-12): concise, non-technical.
    diag = logging.getLogger("ars.diag")
    diag.setLevel(logging.INFO)
    diag.propagate = False
    for h in list(diag.handlers):
        diag.removeHandler(h)
    diag_file = logging.handlers.RotatingFileHandler(
        logs_dir / "diagnostics.log",
        maxBytes=1 * 1024 * 1024,
        backupCount=2,
        encoding="utf-8",
    )
    diag_file.setFormatter(logging.Formatter(_DIAG_FORMAT))
    diag.addHandler(diag_file)


def get_logger(name: str) -> logging.Logger:
    """Technical structured logger (child of 'ars')."""
    if not name.startswith("ars."):
        name = f"ars.{name}"
    return logging.getLogger(name)


def log_diag(message: str, level: str = "INFO", **fields: Any) -> None:
    """Write a concise user-facing diagnostic line."""
    diag = logging.getLogger("ars.diag")
    extra = ""
    if fields:
        extra = " " + " ".join(f"{k}={v}" for k, v in fields.items())
    diag.log(_resolve_level(level), f"{message}{extra}")


def log_paths(cfg: Optional[AppConfig] = None) -> Dict[str, Path]:
    cfg = cfg or get_config()
    return {"app_log": cfg.subroot("logs") / "app.log", "diagnostics": cfg.subroot("logs") / "diagnostics.log"}
