from __future__ import annotations

from pathlib import Path

import pytest

from health_exporter.dashboard import DashboardState
from health_exporter.db import Database
from health_exporter.operation_log import EVENT_LOG_NAME, OperationLogger


def test_dashboard_snapshot_reads_checkpoint_and_failures(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    database = Database(data_dir / "health.sqlite3", data_dir / "private")
    source_id = database.ensure_source(
        vendor="oppo",
        app_package="com.heytap.health",
        app_version="test",
        installation_hash="installation",
        account_hash="account",
        timezone="Asia/Shanghai",
    )
    run_id = database.start_run(source_id, "collect", "test", "serial", {})
    database.set_checkpoint(
        source_id,
        "heart_rate",
        "2024-04-09",
        {"page_date": "2024-04-09", "status": "captured"},
    )
    database.record_failure(
        run_id,
        "ui_capture",
        "fixture failure",
        metric="heart_rate",
        local_date="2024-04-08",
    )
    database.finish_run(run_id, "failed", "fixture failure")
    database.close()

    state = DashboardState(tmp_path, data_dir)
    state.system_probe = lambda: {  # type: ignore[method-assign]
        "adb": False,
        "processes": [],
        "errors": [],
    }

    snapshot = state.snapshot()

    heart = next(item for item in snapshot["metrics"] if item["key"] == "heart_rate")
    assert heart["checkpoint"] == "2024-04-09"
    assert snapshot["phase"] == "idle"
    assert snapshot["failures"][0]["message"] == "fixture failure"


def test_dashboard_log_reader_rejects_path_traversal(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "collector.log").write_text("one\ntwo\nthree\n", encoding="utf-8")
    state = DashboardState(tmp_path, data_dir)

    assert state.read_log("collector.log", 20)["text"].endswith("three\n")
    with pytest.raises(FileNotFoundError):
        state.read_log("../collector.log", 20)


def test_dashboard_log_reader_decodes_windows_chinese_log(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True)
    message = "错误：页面没有可识别日期。\r\n"
    (log_dir / "collector.err.log").write_bytes(message.encode("gb18030"))
    state = DashboardState(tmp_path, data_dir)

    result = state.read_log("collector.err.log", 20)

    assert result["text"] == message.replace("\r\n", "\n")
    assert result["encoding"] == "gb18030"


def test_dashboard_reads_realtime_operation_events(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    logger = OperationLogger(
        data_dir / "logs" / EVENT_LOG_NAME,
        "run-dashboard",
        echo=False,
    )
    logger.log(
        "INFO",
        "viewport.captured",
        "已保存第 2 个视口",
        metric="heart_rate",
        local_date="2024-04-08",
        page="heart_rate",
        action="capture_viewport",
        status="complete",
    )
    state = DashboardState(tmp_path, data_dir)

    result = state.read_events(100)

    assert result["events"][0]["event"] == "viewport.captured"
    assert result["events"][0]["level"] == "INFO"
