from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from health_exporter.db import Database
from health_exporter.normalization import (
    OppoNormalizer,
    _activity_summary,
    _annotate_sleep_intervals,
    epoch_ms,
    local_date_for,
    repair_weight_units,
)


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
            "calories": 12300,
            "altitude_offset": 150,
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
            "total_calories": 234500,
            "current_day_calories_goal": 300000,
            "total_workout_minutes": 45,
            "sport_mode": -3,
            "display": 1,
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
    assert summary["active_energy_kcal"] == 234.5
    assert summary["energy_goal_kcal"] == 300
    assert chart["series"]["steps"][0]["value"] == 321
    assert chart["series"]["active_energy_kcal"][0]["value"] == 12.3
    assert chart["series"]["elevation_gain_m"][0]["value"] == 1.5

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
            "calories": 12300,
            "altitude_offset": 150,
        },
        ["_id"],
        "fixture.db",
    )
    assert second.finalize()["new_raw_versions"] == 0
    db.close()


def test_activity_summary_uses_visible_daily_aggregate_not_later_mode_row() -> None:
    summary = _activity_summary(
        [
            {
                "_id": 10,
                "sport_mode": -3,
                "display": 1,
                "total_steps": 8700,
                "total_calories": 540000,
                "current_day_calories_goal": 300000,
                "total_altitude_offset": 583,
            },
            {
                "_id": 11,
                "sport_mode": 8,
                "display": 1,
                "total_steps": 32,
                "total_calories": 5000,
                "total_altitude_offset": 0,
            },
        ]
    )

    assert summary["steps"] == 8700
    assert summary["active_energy_kcal"] == 540
    assert summary["energy_goal_kcal"] == 300
    assert summary["elevation_gain_m"] == 5.83
    assert summary["activity_stat_selection"]["strategy"] == (
        "sport_mode_-3_display_1"
    )


def test_sleep_stage_is_preserved_as_interval(tmp_path: Path) -> None:
    db, source_id, run_id = make_runtime(tmp_path)
    normalizer = OppoNormalizer(db, source_id, run_id)
    normalizer.ingest_row(
        "DBSleepTable",
        {
            "_id": 6,
            "date": 20240103,
            "start_time": 1704218400000,
            "end_time": 1704218460000,
            "sleep_state": 2,
        },
        ["_id"],
        "fixture.db",
    )
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
    assert db.connection.execute("SELECT COUNT(*) FROM metric_intervals").fetchone()[0] == 1
    assert interval["state"] == "deep"
    assert interval["value_num"] == 3600
    assert json.loads(interval["metadata_json"])["vendor_state"] == "2"
    db.close()


def test_legacy_sleep_daily_stat_extends_source_history(tmp_path: Path) -> None:
    db, source_id, run_id = make_runtime(tmp_path)
    normalizer = OppoNormalizer(db, source_id, run_id)
    normalizer.ingest_row(
        "DBSleepDataStatTable",
        {
            "_id": 1,
            "date": 20220101,
            "fall_asleep": 1627,
            "sleep_out": 628,
            "total_sleep_time": 436,
            "total_deep_sleep_time": 150,
            "total_lightly_sleep_time": 197,
            "total_rem_time": 89,
            "total_wake_up_time": 5,
            "sleep_score": 82,
            "checked_sleep_score": 1,
        },
        ["_id"],
        "fixture.db",
    )

    normalizer.finalize()

    record = db.connection.execute(
        "SELECT * FROM metric_records WHERE canonical_metric='sleep'"
    ).fetchone()
    assert record["local_date"] == "2022-01-01"
    daily = db.connection.execute(
        "SELECT * FROM daily_metrics_current WHERE canonical_metric='sleep'"
    ).fetchone()
    summary = json.loads(daily["summary_json"])
    assert summary["total_sleep_raw"] == 436
    assert summary["total_sleep_raw__unit"] == "min"
    assert summary["sleep_start_minute_offset"] == 1627
    assert summary["sleep_score"] == 82
    assert summary["vendor_summaries"]["DBSleepDataStatTable"][0]["sleep_out"] == 628
    db.close()


def test_sleep_intervals_are_marked_primary_against_visible_session() -> None:
    summary = {
        "sleep_start_utc_ms": 1000,
        "sleep_end_utc_ms": 181000,
        "total_sleep_raw": 2,
        "awake_raw": 1,
    }
    intervals = [
        {"start_utc_ms": 1000, "end_utc_ms": 121000, "state": "2"},
        {"start_utc_ms": 121000, "end_utc_ms": 181000, "state": "0"},
        {"start_utc_ms": 300000, "end_utc_ms": 360000, "state": "4"},
    ]

    annotated = _annotate_sleep_intervals(summary, intervals)

    assert [item["session_relation"] for item in annotated] == [
        "primary",
        "primary",
        "additional_session",
    ]
    assert summary["interval_validation"] == {
        "primary_expected_minutes": 3.0,
        "primary_interval_minutes": 3.0,
        "delta_minutes": 0.0,
        "complete_within_one_minute": True,
    }


def test_weight_grams_are_normalized_to_kg_and_legacy_values_are_audited(
    tmp_path: Path,
) -> None:
    db, source_id, run_id = make_runtime(tmp_path)
    normalizer = OppoNormalizer(db, source_id, run_id)
    normalizer.ingest_row(
        "DBWeightBodyFatTable",
        {
            "_id": 9,
            "measurement_timestamp": 1765087046017,
            "weight": "75000",
            "bmi": "25.95",
            "weight_status": -1,
        },
        ["_id"],
        "fixture.db",
    )
    normalizer.finalize()

    sample = db.connection.execute(
        "SELECT * FROM metric_samples WHERE sample_kind='weight_kg'"
    ).fetchone()
    assert sample["value_num"] == 75.0
    assert sample["unit"] == "kg"
    assert sample["original_value"] == "75000"
    assert sample["original_unit"] == "g"
    daily = db.connection.execute(
        "SELECT * FROM daily_metrics_current WHERE canonical_metric='weight'"
    ).fetchone()
    summary = json.loads(daily["summary_json"])
    assert summary["latest_weight_kg"] == 75.0
    assert summary["latest_bmi"] == 25.95

    db.connection.execute(
        "UPDATE metric_samples SET value_num=75000, original_unit='kg' WHERE id=?",
        (sample["id"],),
    )
    legacy_summary = {
        "computed_from_samples": {
            "weight_kg": {
                "count": 1,
                "min": 75000.0,
                "max": 75000.0,
                "average": 75000.0,
                "sum": 75000.0,
            }
        }
    }
    legacy_chart = {
        "series": {
            "weight_kg": [
                {
                    "timestamp_utc_ms": 1765087046017,
                    "value": 75000.0,
                    "unit": "kg",
                }
            ]
        },
        "intervals": [],
    }
    db.connection.execute(
        """
        UPDATE daily_metrics
        SET summary_json=?, chart_json=?, payload_hash='legacy-weight-fixture'
        WHERE id=?
        """,
        (
            json.dumps(legacy_summary, ensure_ascii=False),
            json.dumps(legacy_chart, ensure_ascii=False),
            daily["id"],
        ),
    )
    db.connection.commit()

    repair_run = db.start_run(source_id, "repair-normalized", "fixture", "offline", {})
    repaired = repair_weight_units(db, source_id, repair_run)
    assert repaired["corrected_samples"] == 1
    assert repaired["new_daily_versions"] == 1
    corrected_sample = db.connection.execute(
        "SELECT * FROM metric_samples WHERE id=?", (sample["id"],)
    ).fetchone()
    assert corrected_sample["value_num"] == 75.0
    assert corrected_sample["original_unit"] == "g"
    corrected_daily = db.connection.execute(
        "SELECT * FROM daily_metrics_current WHERE canonical_metric='weight'"
    ).fetchone()
    corrected_summary = json.loads(corrected_daily["summary_json"])
    assert corrected_summary["latest_weight_kg"] == 75.0
    assert corrected_summary["computed_from_samples"]["weight_kg"]["average"] == 75.0
    assert db.connection.execute(
        "SELECT COUNT(*) FROM normalization_corrections"
    ).fetchone()[0] == 1

    second_run = db.start_run(source_id, "repair-normalized", "fixture", "offline", {})
    assert repair_weight_units(db, source_id, second_run)["corrected_samples"] == 0
    db.close()
