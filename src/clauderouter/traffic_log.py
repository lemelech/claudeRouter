"""In-memory ring buffer + JSONL persistence for recent proxy traffic."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path

try:
    from .sessions import SessionInfo
except ImportError:
    @dataclass(frozen=True)
    class SessionInfo:  # type: ignore[no-redef]
        pid: int | None
        cwd: str | None
        label: str

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LogEntry:
    timestamp: str                  # ISO8601 UTC with milliseconds, e.g. "2026-06-12T14:32:01.123Z"
    session: SessionInfo
    provider: str | None            # None only for 503 "no provider available"
    mode: str                        # "auto" or a forced provider name
    requested_model: str
    translated_model: str | None    # None when provider is None
    tried: list[str]                # providers attempted before this result
    request_bytes: int
    response_bytes: int
    response_content_type: str
    status: int
    error_summary: str | None       # truncated to ~200 chars, only for 4xx/503
    usage: dict | None               # {"input_tokens", "output_tokens",
                                      #  "cache_read_input_tokens", "cache_creation_input_tokens"}
    duration_ms: float

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "session": dataclasses.asdict(self.session),
            "provider": self.provider,
            "mode": self.mode,
            "requested_model": self.requested_model,
            "translated_model": self.translated_model,
            "tried": self.tried,
            "request_bytes": self.request_bytes,
            "response_bytes": self.response_bytes,
            "response_content_type": self.response_content_type,
            "status": self.status,
            "error_summary": self.error_summary,
            "usage": self.usage,
            "duration_ms": self.duration_ms,
        }


class TrafficLog:
    def __init__(self, ring_size: int, queue_size: int, log_path: Path | None) -> None:
        self._ring: deque[LogEntry] = deque(maxlen=ring_size)
        self._queue: asyncio.Queue[LogEntry] = asyncio.Queue(maxsize=queue_size)
        self._log_path = self._prepare_log_path(log_path)
        self._write_failed_logged = False

    @staticmethod
    def _prepare_log_path(log_path: Path | None) -> Path | None:
        if not log_path:
            return None
        try:
            path = Path(log_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
        except Exception as e:
            log.warning("Could not prepare traffic log path %s: %s", log_path, e)
            return None

    def emit(self, entry: LogEntry) -> None:
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            log.debug("Traffic log queue full; dropping entry")
        except Exception as e:
            log.debug("Failed to enqueue traffic log entry: %s", e)

    def recent(self, n: int | None = None) -> list[LogEntry]:
        entries = list(reversed(self._ring))
        if n is None:
            return entries
        return entries[:n]

    async def run(self) -> None:
        while True:
            entry = await self._queue.get()
            self._ring.append(entry)
            if self._log_path is not None:
                try:
                    with open(self._log_path, "a") as f:
                        f.write(json.dumps(entry.to_dict()) + "\n")
                except Exception as e:
                    if not self._write_failed_logged:
                        log.warning("Failed to write traffic log to %s: %s", self._log_path, e)
                        self._write_failed_logged = True
