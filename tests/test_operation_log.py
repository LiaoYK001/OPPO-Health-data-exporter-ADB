from __future__ import annotations

import builtins
import json
from pathlib import Path

from health_exporter.operation_log import OperationLogger, read_event_tail


def test_operation_logger_writes_structured_timestamped_event(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "operation-events.jsonl"
    logger = OperationLogger(path, "run-123", echo=False)

    logger.log(
        "warning",
        "calendar.fallback",
        "日历定位失败，改用逐日翻页",
        metric="heart_rate",
        local_date="2024-04-09",
        page="calendar",
        action="resume_previous_day_batches",
        status="fallback",
        details={"round": 3},
    )

    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["timestamp"].endswith(("+08:00", "Z"))
    assert event["epoch_ms"] > 0
    assert event["level"] == "WARNING"
    assert event["run_id"] == "run-123"
    assert event["metric"] == "heart_rate"
    assert event["details"] == {"round": 3}


def test_operation_logger_ignores_closed_console_pipe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "operation-events.jsonl"
    logger = OperationLogger(path, "run-detached", echo=True)

    def closed_pipe(*_args, **_kwargs) -> None:
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(builtins, "print", closed_pipe)
    event = logger.log("INFO", "day.begin", "继续采集")

    assert event["event"] == "day.begin"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["run_id"] == "run-detached"


def test_read_event_tail_returns_newest_complete_events(tmp_path: Path) -> None:
    path = tmp_path / "operation-events.jsonl"
    logger = OperationLogger(path, "run-1", echo=False)
    for index in range(35):
        logger.log("INFO", f"event.{index}", f"message {index}")
    with path.open("ab") as handle:
        handle.write(b'{"incomplete":')

    result = read_event_tail(path, 20)

    assert [event["event"] for event in result["events"]] == [
        f"event.{index}" for index in range(16, 35)
    ]
    assert result["bytes"] == path.stat().st_size
