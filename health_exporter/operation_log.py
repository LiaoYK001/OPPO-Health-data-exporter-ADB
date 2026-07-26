from __future__ import annotations

import datetime as dt
import json
import threading
import time
from pathlib import Path
from typing import Any


EVENT_LOG_NAME = "operation-events.jsonl"


class OperationLogger:
    """Append-only structured UI operation log for humans and the dashboard."""

    def __init__(self, path: Path, run_id: str, *, echo: bool = True):
        self.path = path
        self.run_id = run_id
        self.echo = echo
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        level: str,
        event: str,
        message: str,
        *,
        metric: str | None = None,
        local_date: str | None = None,
        page: str | None = None,
        activity: str | None = None,
        action: str | None = None,
        status: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = dt.datetime.now().astimezone()
        payload = {
            "timestamp": now.isoformat(timespec="milliseconds"),
            "epoch_ms": time.time_ns() // 1_000_000,
            "level": level.upper(),
            "event": event,
            "message": message,
            "run_id": self.run_id,
            "metric": metric,
            "local_date": local_date,
            "page": page,
            "activity": activity,
            "action": action,
            "status": status,
            "details": details or {},
        }
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        try:
            with self._lock:
                with self.path.open("ab") as handle:
                    handle.write(line.encode("utf-8") + b"\n")
                    handle.flush()
        except OSError as exc:
            if self.echo:
                try:
                    print(
                        f"{payload['timestamp']} [WARNING] operation_log 写入失败：{exc}",
                        flush=True,
                    )
                except OSError:
                    # A detached/closed terminal must never abort collection.
                    pass
            return payload
        if self.echo:
            context = " ".join(
                item
                for item in (metric, local_date, page)
                if item
            )
            context_text = f" [{context}]" if context else ""
            try:
                print(
                    f"{payload['timestamp']} [{payload['level']}]{context_text} "
                    f"{event}: {message}",
                    flush=True,
                )
            except OSError:
                # Structured JSONL is durable; console echo is optional.
                pass
        return payload


def read_event_tail(path: Path, limit: int = 300) -> dict[str, Any]:
    """Read the newest complete JSONL events without loading an unbounded file."""

    limit = max(20, min(int(limit), 2000))
    if not path.is_file():
        return {"events": [], "bytes": 0, "modified_at": None}
    block_size = 64 * 1024
    chunks: list[bytes] = []
    newline_count = 0
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        size = position
        while position > 0 and newline_count <= limit:
            read_size = min(block_size, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
    raw_lines = b"".join(reversed(chunks)).splitlines()[-limit:]
    events: list[dict[str, Any]] = []
    for raw_line in raw_lines:
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            events.append(value)
    stat = path.stat()
    return {
        "events": events,
        "bytes": size,
        "modified_at": stat.st_mtime,
    }
