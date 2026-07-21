from __future__ import annotations

import datetime as dt
from pathlib import Path

from health_exporter.constants import UI_PROFILES
from health_exporter.db import Database
from health_exporter.verification import (
    Verifier,
    _activity_ui_summary,
    _display_dates,
    _heart_ui_summary,
    _round_for_display,
    _sleep_ui_summary,
    _spo2_ui_summary,
    _stress_ui_summary,
    _visible_subpage_titles,
    _weight_ui_summary,
)


def test_display_dates_supports_weight_canvas_header_without_resource_id() -> None:
    payload = {
        "viewports": [
            {
                "nodes": [
                    {
                        "text": "2025年12月07日，周日",
                        "content_desc": "",
                        "resource_id": "",
                    }
                ]
            }
        ]
    }

    assert _display_dates(
        payload,
        UI_PROFILES["weight"],
        dt.date(2025, 12, 7),
    ) == [("2025年12月07日，周日", dt.date(2025, 12, 7))]


def test_display_dates_prefers_profile_date_resource_id() -> None:
    payload = {
        "viewports": [
            {
                "nodes": [
                    {"text": "7月01日", "content_desc": "", "resource_id": "axis"},
                    {"text": "7月02日", "content_desc": "", "resource_id": "tv_date"},
                ]
            }
        ]
    }

    assert _display_dates(
        payload,
        UI_PROFILES["heart_rate"],
        dt.date(2026, 7, 2),
    ) == [("7月02日", dt.date(2026, 7, 2))]


def test_visible_subpage_titles_only_returns_titles_actually_rendered() -> None:
    payload = {
        "viewports": [
            {
                "nodes": [
                    {"text": "静息心率", "content_desc": ""},
                    {"text": "步行平均心率", "content_desc": ""},
                    {"text": "其他说明", "content_desc": ""},
                ]
            }
        ]
    }

    assert _visible_subpage_titles(payload, UI_PROFILES["heart_rate"]) == {
        "静息心率",
        "步行平均心率",
    }


def test_heart_ui_summary_reads_exact_visible_integers() -> None:
    payload = {
        "viewports": [
            {
                "nodes": [
                    {"text": "全天心率"},
                    {"text": "51-145"},
                    {"text": "次/分"},
                    {"text": "静息心率"},
                    {"text": "67次/分"},
                    {"text": "步行平均心率"},
                    {"text": "108次/分"},
                ]
            }
        ]
    }

    assert _heart_ui_summary(payload) == {
        "range_bpm": (51, 145),
        "resting_bpm": 67,
        "walking_average_bpm": 108,
    }


def test_weight_ui_summary_uses_visible_precision() -> None:
    payload = {
        "viewports": [
            {
                "nodes": [
                    {"text": "55.6"},
                    {"text": "公斤"},
                ]
            }
        ]
    }

    assert _weight_ui_summary(payload) == {
        "weight_kg": 55.6,
        "weight_kg_decimals": 1,
        "weight_kg_text": "55.6",
    }
    assert _round_for_display(64.199, 1) == 64.2


def test_sleep_spo2_and_stress_ui_summaries_use_visible_text() -> None:
    sleep = {"viewports": [{"nodes": [{"text": "7\u00a0小时\u00a043\u00a0分钟"}]}]}
    spo2 = {"viewports": [{"nodes": [{"text": "95-95"}, {"text": "%"}]}]}
    stress = {
        "viewports": [
            {
                "nodes": [
                    {"text": "15-47"},
                    {"text": "平均压力"},
                    {"text": "31"},
                ]
            }
        ]
    }

    assert _sleep_ui_summary(sleep) == {"total_sleep_minutes": 463}
    assert _spo2_ui_summary(spo2) == {"range_percent": (95, 95)}
    assert _stress_ui_summary(stress) == {"range": (15, 47), "average": 31}


def test_spo2_ui_summary_reads_latest_exact_measurement() -> None:
    payload = {
        "viewports": [
            {
                "nodes": [
                    {"text": "95-95", "resource_id": "tv_range_value"},
                    {"text": "17:14 | 95%", "resource_id": "tv_warning_value"},
                ]
            }
        ]
    }

    assert _spo2_ui_summary(payload) == {
        "range_percent": (95, 95),
        "latest_time": (17, 14),
        "latest_percent": 95,
    }


def test_stress_ui_summary_reads_latest_and_band_breakdown() -> None:
    payload = {
        "viewports": [
            {
                "nodes": [
                    {"text": "15-47", "resource_id": "tv_health_stress_range"},
                    {"text": "平均压力", "resource_id": "item_evaluate_text"},
                    {"text": "31", "resource_id": "item_evaluate_value"},
                    {"text": "最近压力", "resource_id": "item_evaluate_text"},
                    {"text": "43 | 正常", "resource_id": "item_evaluate_value"},
                    {"text": "(16:50)", "resource_id": "item_evaluate_time"},
                    {
                        "text": "2小时40分钟",
                        "resource_id": "tv_health_stress_percent_relax_time",
                    },
                    {
                        "text": "58%",
                        "resource_id": "tv_health_stress_percent_relax_value",
                    },
                    {
                        "text": "1小时57分钟",
                        "resource_id": "tv_health_stress_percent_normal_time",
                    },
                    {
                        "text": "42%",
                        "resource_id": "tv_health_stress_percent_normal_value",
                    },
                    {
                        "text": "0分钟",
                        "resource_id": "tv_health_stress_percent_medium_time",
                    },
                    {
                        "text": "0%",
                        "resource_id": "tv_health_stress_percent_medium_value",
                    },
                    {
                        "text": "0分钟",
                        "resource_id": "tv_health_stress_percent_high_time",
                    },
                    {
                        "text": "0%",
                        "resource_id": "tv_health_stress_percent_high_value",
                    },
                ]
            }
        ]
    }

    assert _stress_ui_summary(payload) == {
        "range": (15, 47),
        "average": 31,
        "latest_score": 43,
        "latest_time": (16, 50),
        "relaxed_duration_minutes": 160,
        "relaxed_percent": 58,
        "normal_duration_minutes": 117,
        "normal_percent": 42,
        "medium_duration_minutes": 0,
        "medium_percent": 0,
        "high_duration_minutes": 0,
        "high_percent": 0,
    }


def test_sleep_ui_summary_reads_visible_start_and_end_wall_time() -> None:
    payload = {
        "viewports": [
            {
                "nodes": [
                    {"text": "7/19", "resource_id": "tv_sleep_start_date"},
                    {"text": "入睡 03:03", "resource_id": "tv_sleep_start_time"},
                    {"text": "7/19", "resource_id": "tv_sleep_end_date"},
                    {"text": "醒来 10:54", "resource_id": "tv_sleep_end_time"},
                    {"text": "7 小时 43 分钟", "resource_id": "tv_sleep_time"},
                ]
            }
        ]
    }

    assert _sleep_ui_summary(payload) == {
        "total_sleep_minutes": 463,
        "start_month_day": (7, 19),
        "start_time": (3, 3),
        "end_month_day": (7, 19),
        "end_time": (10, 54),
    }


def test_activity_ui_summary_reads_current_values_and_goals() -> None:
    payload = {
        "viewports": [
            {
                "nodes": [
                    {"text": "2223 / 6000步"},
                    {"text": "43 / 300千卡"},
                    {"text": "0 / 30分钟"},
                    {"text": "0 / 12次"},
                ]
            }
        ]
    }

    values = _activity_ui_summary(payload)
    assert {key: value for key, value in values.items() if not key.endswith("__decimals")} == {
        "steps": 2223,
        "steps_goal": 6000,
        "active_energy_kcal": 43,
        "energy_goal_kcal": 300,
        "workout_minutes": 0,
        "workout_goal_minutes": 30,
        "active_count": 0,
        "active_count_goal": 12,
    }


def test_inflight_subpage_is_pending_but_finished_run_is_incomplete(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "health.sqlite3", tmp_path / "private")
    installation = db.pseudonymize("fixture")
    source_id = db.ensure_source(
        vendor="oppo",
        app_package="com.heytap.health",
        app_version="fixture",
        installation_hash=installation,
        account_hash="unknown:fixture",
        timezone="Asia/Shanghai",
    )
    run_id = db.start_run(source_id, "collect", "fixture", "fixture", {})
    db.add_page_capture(
        source_id=source_id,
        run_id=run_id,
        metric="heart_rate",
        local_date="2026-07-02",
        page_key="heart_rate",
        app_activity="fixture",
        app_version="fixture",
        display_profile="fixture",
        display_payload={
            "viewports": [
                {
                    "nodes": [
                        {"text": "7月02日", "resource_id": "tv_date"},
                        {"text": "静息心率", "resource_id": ""},
                    ]
                }
            ]
        },
        capture_hash="fixture-main-page",
        fidelity="exact_ui",
    )
    db.connection.commit()

    running_report = Verifier(db, tmp_path).verify(
        source_id=source_id,
        verify_all_artifacts=False,
    )
    assert running_report["passed"] is True
    assert running_report["subpage_validation"]["pending_main_pages"] == 1

    db.finish_run(run_id, "complete")
    finished_report = Verifier(db, tmp_path).verify(
        source_id=source_id,
        verify_all_artifacts=False,
    )
    failure_types = {item["type"] for item in finished_report["failures"]}
    assert finished_report["passed"] is False
    assert "main_page_incomplete" in failure_types
    assert "subpage_capture_missing" in failure_types
    db.close()
