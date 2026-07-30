"""
Structured logger — mirrors the uc-inventory-migration style.

Emits human-readable lines to stdout AND (optionally) appends structured JSON lines to an
`execution_*.log` file in the run's staging dir. Safe to use before a log file is configured
(stdout-only until `set_log_file` is called).
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
import threading
from typing import Optional

_LOCK = threading.Lock()
_LOG_FILE: Optional[str] = None


def set_log_file(path: Optional[str]) -> None:
    """Point all loggers at an execution log file (append mode). None = stdout only."""
    global _LOG_FILE
    _LOG_FILE = path


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class StructuredLogger:
    """A named logger. `info/warning/error(msg, **fields)` prints a line and, if a log file is
    configured, appends one JSON object per call."""

    _LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}

    def __init__(self, name: str, level: str = "INFO") -> None:
        self.name = name
        self._threshold = self._LEVELS.get(level.upper(), 20)

    def _emit(self, level: str, msg: str, **fields) -> None:
        if self._LEVELS.get(level, 20) < self._threshold:
            return
        ts = _now()
        # Human line to stdout
        extra = "  ".join(f"{k}={v}" for k, v in fields.items())
        line = f"[{ts}] {level:<7} {self.name}: {msg}"
        if extra:
            line += f"  | {extra}"
        with _LOCK:
            print(line, flush=True)
            if _LOG_FILE:
                record = {"ts": ts, "level": level, "logger": self.name, "msg": msg, **fields}
                try:
                    with open(_LOG_FILE, "a", encoding="utf-8") as f:
                        f.write(json.dumps(record, default=str) + "\n")
                except Exception:
                    # Never let logging failures break the pipeline.
                    pass

    def debug(self, msg: str, **fields) -> None:
        self._emit("DEBUG", msg, **fields)

    def info(self, msg: str, **fields) -> None:
        self._emit("INFO", msg, **fields)

    def warning(self, msg: str, **fields) -> None:
        self._emit("WARNING", msg, **fields)

    def error(self, msg: str, **fields) -> None:
        self._emit("ERROR", msg, **fields)


def get_logger(name: str, level: str = "INFO") -> StructuredLogger:
    """Return a StructuredLogger for `name`."""
    return StructuredLogger(name, level)
