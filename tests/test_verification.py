from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from health_exporter.artifacts import ArtifactStore
from health_exporter.constants import UI_PROFILES
from health_exporter.db import Database
from health_exporter.verification import (
    Verifier,
    _activity_ui_summary,
    _display_dates,
    _heart_ui_summary,
    _heart_main_scroll_state,
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


def test_heart_ui_summary_preserves_visible_no_data_placeholder() -> None:
    payload = {
        "viewports": [
            {
                "nodes": [
                    {"text": "步行平均心率"},
                    {"text": "--次/分"},
                ]
            }
        ]
    }

    assert _heart_ui_summary(payload) == {
        "walking_average_bpm": None,
        "walking_average_bpm__placeholder": "--",
    }


def test_heart_main_scroll_requires_top_chart_and_bottom_card_viewports() -> None:
    complete = {
        "viewports": [
            {
                "nodes": [
                    {
                        "resource_id": "view_heart_rate_day",
                        "bounds": "[0,723][1440,1859]",
                    }
                ]
            },
            {"nodes": [{"text": "睡眠基准心率"}]},
        ]
    }
    bottom_only = {
        "viewports": [
            {
                "nodes": [
                    {
                        "resource_id": "view_heart_rate_day",
                        "bounds": "[0,723][1440,1381]",
                    },
                    {"text": "睡眠基准心率"},
                ]
            }
        ]
    }

    assert _heart_main_scroll_state(complete) == {
        "viewport_count": 2,
        "max_chart_height": 1136,
        "has_sleep_baseline_card": True,
        "complete": True,
    }
    assert _heart_main_scroll_state(bottom_only)["complete"] is False


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
    artifacts = ArtifactStore(tmp_path, db)
    screenshot = artifacts.put_bytes(
        b"fixture screenshot",
        suffix=".png",
        media_type="image/png",
    )
    xml = artifacts.put_bytes(
        b"<hierarchy/>",
        suffix=".xml",
        media_type="application/xml",
    )
    mosaic = artifacts.put_bytes(
        b"fixture mosaic",
        suffix=".png",
        media_type="image/png",
    )
    capture_id = db.add_page_capture(
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
                    "index": 0,
                    "screenshot_sha256": screenshot.sha256,
                    "xml_sha256": xml.sha256,
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
    db.link_evidence(capture_id, mosaic.sha256, "page_mosaic")
    db.link_evidence(capture_id, screenshot.sha256, "viewport_png", 0)
    db.link_evidence(capture_id, xml.sha256, "ui_xml", 0)
    db.connection.commit()

    running_report = Verifier(db, tmp_path).verify(
        source_id=source_id,
        verify_all_artifacts=False,
    )
    assert running_report["passed"] is True
    assert running_report["subpage_validation"]["pending_main_pages"] == 1
    assert running_report["page_evidence_validation"] == {
        "pages": 1,
        "viewports": 1,
        "links_checked": 3,
        "missing": 0,
        "ambiguous": 0,
        "invalid_payload": 0,
    }
    assert running_report["subpage_validation"]["source_required"] == 0
    assert running_report["artifact_store_validation"] == {
        "registered_files": 3,
        "content_files": 3,
        "orphan_files": 0,
        "invalid_locations": 0,
    }

    db.connection.execute(
        "DELETE FROM evidence_links WHERE page_capture_id=? AND role='page_mosaic'",
        (capture_id,),
    )
    db.connection.commit()
    missing_evidence_report = Verifier(db, tmp_path).verify(
        source_id=source_id,
        verify_all_artifacts=False,
    )
    assert missing_evidence_report["passed"] is False
    assert any(
        item["type"] == "page_evidence_missing"
        and item["role"] == "page_mosaic"
        for item in missing_evidence_report["failures"]
    )
    db.link_evidence(capture_id, mosaic.sha256, "page_mosaic")
    db.connection.commit()

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


def test_strict_verifier_rejects_unregistered_content_store_file(
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
    orphan = tmp_path / "artifacts" / "sha256" / "aa" / ("a" * 64 + ".bin")
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"not registered")

    report = Verifier(db, tmp_path).verify(
        source_id=source_id,
        verify_all_artifacts=True,
    )

    assert report["passed"] is False
    assert report["artifact_store_validation"]["orphan_files"] == 1
    assert any(item["type"] == "artifact_orphan_files" for item in report["failures"])
    db.close()


def test_strict_verifier_validates_completed_snapshot_manifest(
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
    artifacts = ArtifactStore(tmp_path, db)
    raw_view = tmp_path / "raw" / "oppo" / "fixture" / "database.db"
    raw_view.parent.mkdir(parents=True)
    raw_view.write_bytes(b"fixture snapshot")
    raw_artifact = artifacts.put_file(
        raw_view, media_type="application/vnd.sqlite3"
    )
    run_id = db.start_run(source_id, "snapshot-source", "fixture", "fixture", {})
    db.add_raw_snapshot(
        run_id=run_id,
        source_path="/data/user/0/fixture/databases/database.db",
        artifact_sha256=raw_artifact.sha256,
        sqlite_status="encrypted_or_non_sqlite",
        schema_hash=None,
        table_count=None,
        row_count=None,
    )
    manifest = {
        "run_id": run_id,
        "files": [
            {
                "remote_path": "/data/user/0/fixture/databases/database.db",
                "local_path": raw_view.relative_to(tmp_path).as_posix(),
                "sha256": raw_artifact.sha256,
                "sqlite_status": "encrypted_or_non_sqlite",
                "tables": [],
            }
        ],
    }
    manifest_path = tmp_path / "manifests" / f"oppo-snapshot-{run_id}.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    artifacts.put_file(manifest_path, media_type="application/json")
    db.finish_run(run_id, "complete")

    report = Verifier(db, tmp_path).verify(
        source_id=source_id,
        verify_all_artifacts=True,
    )

    assert report["passed"] is True
    assert report["raw_snapshot_validation"] == {
        "completed_runs": 1,
        "manifests_checked": 1,
        "manifest_artifacts_checked": 1,
        "files_checked": 1,
        "issues": 0,
    }
    db.close()


def test_verifier_rejects_unknown_inventory_cards(tmp_path: Path) -> None:
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
    artifacts = ArtifactStore(tmp_path, db)
    xml = artifacts.put_bytes(
        b"<hierarchy/>", suffix=".xml", media_type="application/xml"
    )
    run_id = db.start_run(source_id, "inventory", "fixture", "fixture", {})
    manifest = {
        "run_id": run_id,
        "ui": {
            "cards": ["new raw metric"],
            "included": [],
            "excluded": [],
            "unknown": ["new raw metric"],
            "views": [{"index": 0, "xml_sha256": xml.sha256}],
        },
    }
    manifest_path = tmp_path / "manifests" / f"inventory-{run_id}.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    artifacts.put_file(manifest_path, media_type="application/json")
    db.finish_run(run_id, "partial")

    report = Verifier(db, tmp_path).verify(
        source_id=source_id,
        verify_all_artifacts=False,
    )

    assert report["passed"] is False
    assert report["inventory_validation"]["unknown"] == 1
    assert report["inventory_validation"]["view_xml_artifacts_checked"] == 1
    assert any(item["type"] == "inventory_unknown_cards" for item in report["failures"])
    db.close()
