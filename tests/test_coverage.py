from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from health_exporter.db import Database
from health_exporter.verification import Verifier


def _source_and_run(db: Database) -> tuple[int, str]:
    source_id = db.ensure_source(
        vendor="oppo",
        app_package="com.heytap.health",
        app_version="fixture",
        installation_hash=db.pseudonymize("fixture-installation"),
        account_hash="unknown:fixture",
        timezone="Asia/Shanghai",
    )
    return source_id, db.start_run(source_id, "test", "fixture", "offline", {})


def test_coverage_merges_ui_evidence_without_downgrading_exact_source(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "health.sqlite3", tmp_path / "private")
    source_id, run_id = _source_and_run(db)
    day = "2026-07-01"

    db.set_coverage(
        source_id,
        "heart_rate",
        day,
        "exact_source",
        {"database": True, "ui": False},
        run_id,
    )
    db.set_coverage(
        source_id,
        "heart_rate",
        day,
        "exact_ui",
        {"database": True, "ui": True},
        run_id,
    )
    row = db.connection.execute(
        "SELECT status, detail_json FROM coverage WHERE source_id=? AND local_date=?",
        (source_id, day),
    ).fetchone()

    assert row["status"] == "exact_source"
    assert json.loads(row["detail_json"]) == {"database": True, "ui": True}
    db.close()


def test_no_data_day_keeps_semantic_status_after_ui_evidence(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "health.sqlite3", tmp_path / "private")
    source_id, run_id = _source_and_run(db)
    day = "2026-07-01"

    db.set_coverage(
        source_id,
        "heart_rate",
        day,
        "no_data",
        {
            "database": False,
            "ui": False,
            "no_data": True,
            "basis": "exact_source_absence",
        },
        run_id,
    )
    db.set_coverage(
        source_id,
        "heart_rate",
        day,
        "no_data",
        {
            "database": False,
            "ui": True,
            "no_data": True,
            "basis": "exact_source_absence_with_ui_evidence",
        },
        run_id,
    )
    row = db.connection.execute(
        "SELECT status, detail_json FROM coverage WHERE source_id=? AND local_date=?",
        (source_id, day),
    ).fetchone()

    assert row["status"] == "no_data"
    assert json.loads(row["detail_json"]) == {
        "basis": "exact_source_absence_with_ui_evidence",
        "database": False,
        "no_data": True,
        "ui": True,
    }
    db.close()


def test_coverage_repair_restores_best_fidelity_and_is_idempotent(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "health.sqlite3", tmp_path / "private")
    source_id, run_id = _source_and_run(db)
    db.connection.execute(
        """
        INSERT INTO coverage(
            source_id, canonical_metric, local_date, status,
            detail_json, run_id, updated_at
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (
            source_id,
            "heart_rate",
            "2026-07-01",
            "exact_ui",
            json.dumps({"database": True, "ui": True}),
            run_id,
            "fixture",
        ),
    )
    db.connection.commit()

    assert db.repair_coverage_statuses(source_id) == {
        "coverage_statuses_repaired": 1
    }
    assert db.repair_coverage_statuses(source_id) == {
        "coverage_statuses_repaired": 0
    }
    db.close()


def test_verifier_treats_today_as_pending_not_a_historical_gap(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "health.sqlite3", tmp_path / "private")
    source_id, run_id = _source_and_run(db)
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    db.upsert_daily_metric(
        source_id=source_id,
        canonical_metric="heart_rate",
        local_date=yesterday,
        summary={"min_bpm": 50, "max_bpm": 100},
        chart={},
        fidelity="exact_source",
        is_partial=False,
        run_id=run_id,
    )
    db.set_coverage(
        source_id,
        "heart_rate",
        yesterday,
        "exact_source",
        {"database": True, "ui": False},
        run_id,
    )
    db.finish_run(run_id, "complete")

    report = Verifier(db, tmp_path).verify(
        source_id=source_id,
        verify_all_artifacts=False,
    )

    assert report["passed"] is True
    assert report["date_ranges"]["heart_rate"]["coverage_days"] == 1
    assert report["current_day_coverage"]["heart_rate"]["status"] == "pending"
    assert report["source_sequence_validation"]["heart_rate"] == {
        "source_days": 1,
        "sequence_days": 0,
        "missing_sequence_days": 1,
        "missing_sequence_dates": [yesterday],
        "disagreement_days": 0,
        "disagreements": [],
    }
    assert any(
        item["type"] == "current_day_pending" and item["metric"] == "heart_rate"
        for item in report["warnings"]
    )
    db.close()
