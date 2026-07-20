from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from health_exporter.db import Database
from health_exporter.normalization import OppoNormalizer, epoch_ms, local_date_for


def make_runtime(tmp_path: Path) -> tuple[Database, int, str]:
    db = Database(tmp_path / "health.sqlite3", tmp_path / "private")
    installation = db.pseudonymize("fixture-device")
    source_id = db.ensure_source(
        vendor="oppo",
        app_package="com.heytap.health",
        app_version="fixture",
        installation_hash=installation,
        account_hash=f"unknown:{installation[:16]}",
        timezone="Asia/Shanghai",
    )
    run_id = db.start_run(source_id, "test", "fixture", "fixture", {})
    return db, source_id, run_id


def test_epoch_and_local_date_keep_oppo_timezone() -> None:
    value = epoch_ms("2024-02-29", "Asia/Shanghai")
    expected = int(
        dt.datetime(2024, 2, 29, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        * 1000
    )
    assert value == expected
    assert local_date_for(expected, "Asia/Shanghai") == "2024-02-29"


def test_activity_rows_are_versioned_and_rebuild_exact_series(tmp_path: Path) -> None:
    db, source_id, run_id = make_runtime(tmp_path)
    normalizer = OppoNormalizer(db, source_id, run_id)
    normalizer.ingest_row(
        "DBSportDataDetail",
        {
            "_id": 10,
            "date": 20240102,
            "start_time": 1704124800000,
            "end_time": 1704128400000,
            "steps": 321,
            "distance": 240.5,
            "calories": 12.3,
        },
        ["_id"],
        "fixture.db",
    )
    normalizer.ingest_row(
        "DBSportDataStat",
        {
            "_id": 11,
            "date": 20240102,
            "total_steps": 4321,
            "total_distance": 3260.25,
            "total_calories": 234.5,
            "total_workout_minutes": 45,
        },
        ["_id"],
        "fixture.db",
    )
    result = normalizer.finalize()
    assert result["new_raw_versions"] == 2
    row = db.connection.execute(
        "SELECT * FROM daily_metrics_current WHERE canonical_metric='activity'"
    ).fetchone()
    summary = json.loads(row["summary_json"])
    chart = json.loads(row["chart_json"])
    assert summary["steps"] == 4321
    assert chart["series"]["steps"][0]["value"] == 321

    second = OppoNormalizer(db, source_id, run_id)
    second.ingest_row(
        "DBSportDataDetail",
        {
            "_id": 10,
            "date": 20240102,
            "start_time": 1704124800000,
            "end_time": 1704128400000,
            "steps": 321,
            "distance": 240.5,
            "calories": 12.3,
        },
        ["_id"],
        "fixture.db",
    )
    assert second.finalize()["new_raw_versions"] == 0
    db.close()


def test_sleep_stage_is_preserved_as_interval(tmp_path: Path) -> None:
    db, source_id, run_id = make_runtime(tmp_path)
    normalizer = OppoNormalizer(db, source_id, run_id)
    normalizer.ingest_row(
        "DBSleepPiece",
        {
            "_id": 7,
            "date": 20240103,
            "start_time": 1704218400000,
            "end_time": 1704222000000,
            "sleep_state": 2,
        },
        ["_id"],
        "fixture.db",
    )
    normalizer.finalize()
    interval = db.connection.execute("SELECT * FROM metric_intervals").fetchone()
    assert interval["state"] == "2"
    assert interval["value_num"] == 3600
    db.close()
