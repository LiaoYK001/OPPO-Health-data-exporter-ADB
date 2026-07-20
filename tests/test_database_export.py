from __future__ import annotations

import csv
from pathlib import Path

from openpyxl import load_workbook

from health_exporter.db import Database
from health_exporter.exporting import DailyExporter


def test_pseudonym_is_stable_for_private_salt(tmp_path: Path) -> None:
    path = tmp_path / "health.sqlite3"
    private = tmp_path / "private"
    first = Database(path, private)
    digest = first.pseudonymize("account-1")
    first.close()
    second = Database(path, private)
    assert second.pseudonymize("account-1") == digest
    assert second.pseudonymize("account-2") != digest
    second.close()


def test_daily_csv_and_xlsx_are_rebuilt_from_sqlite(tmp_path: Path) -> None:
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
    run_id = db.start_run(source_id, "test", "fixture", "fixture", {})
    db.upsert_daily_metric(
        source_id=source_id,
        canonical_metric="heart_rate",
        local_date="2024-01-02",
        summary={"min_bpm": 51, "max_bpm": 143},
        chart={"series": [{"timestamp_utc_ms": 1, "value": 51}]},
        fidelity="exact_source",
        is_partial=False,
        run_id=run_id,
    )
    db.connection.commit()

    manifest = DailyExporter(db, tmp_path).export(
        {"csv", "xlsx"}, source_id=source_id, output_dir=tmp_path / "export"
    )
    assert manifest["tables"]["daily_overview"] == 1
    with (tmp_path / "export" / "heart_rate_daily.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        row = next(csv.DictReader(handle))
    assert row["min_bpm"] == "51"
    workbook = load_workbook(tmp_path / "export" / "health_daily.xlsx", read_only=True)
    assert "daily_overview" in workbook.sheetnames
    assert "heart_rate_daily" in workbook.sheetnames
    workbook.close()
    db.close()
