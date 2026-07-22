from __future__ import annotations

import csv
import datetime as dt
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font

from .constants import PRIMARY_METRICS
from .db import Database


DAILY_BASE_COLUMNS = ["source_id", "date", "revision", "fidelity", "partial"]
TABLE_BASE_COLUMNS: dict[str, list[str]] = {
    "daily_overview": ["source_id", "date", "partial"],
    "activity_daily": DAILY_BASE_COLUMNS
    + [
        "steps",
        "distance_m",
        "active_energy_kcal",
        "workout_minutes",
        "active_count",
        "elevation_gain_m",
        "steps_goal",
        "energy_goal_kcal",
        "workout_goal_minutes",
        "active_count_goal",
    ],
    "heart_rate_daily": DAILY_BASE_COLUMNS
    + [
        "min_bpm",
        "max_bpm",
        "average_bpm",
        "resting_bpm",
        "walking_average_bpm",
        "sleep_baseline_bpm",
    ],
    "sleep_daily": DAILY_BASE_COLUMNS
    + [
        "sleep_start_utc_ms",
        "sleep_end_utc_ms",
        "total_sleep_raw",
        "deep_sleep_raw",
        "light_sleep_raw",
        "rem_sleep_raw",
        "awake_raw",
        "wake_count",
        "sleep_score",
    ],
    "spo2_daily": DAILY_BASE_COLUMNS
    + [
        "min_percent",
        "max_percent",
        "average_percent",
        "drop_count",
        "low_total_time_raw",
    ],
    "blood_pressure_daily": DAILY_BASE_COLUMNS
    + [
        "min_systolic_mmhg",
        "max_systolic_mmhg",
        "average_systolic_mmhg",
        "min_diastolic_mmhg",
        "max_diastolic_mmhg",
        "average_diastolic_mmhg",
        "normal_count",
        "high_count",
        "low_count",
    ],
    "weight_daily": DAILY_BASE_COLUMNS
    + [
        "latest_measurement_utc_ms",
        "latest_weight_kg",
        "latest_bmi",
        "measurement_count",
    ],
    "stress_daily": DAILY_BASE_COLUMNS
    + [
        "min",
        "max",
        "average",
        "max_timestamp_utc_ms",
        "relaxed_duration_raw",
        "normal_duration_raw",
        "medium_duration_raw",
        "high_duration_raw",
    ],
    "coverage": [
        "source_id",
        "canonical_metric",
        "local_date",
        "status",
        "detail_json",
        "updated_at",
    ],
}


def _flatten(
    value: Any,
    prefix: str = "",
    *,
    skip_keys: set[str] | None = None,
) -> dict[str, Any]:
    skip_keys = skip_keys or set()
    result: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            if key in skip_keys:
                continue
            name = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten(item, name, skip_keys=skip_keys))
    elif isinstance(value, list):
        result[prefix] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        result[prefix] = value
    return result


def _safe_cell(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    text = str(value)
    if len(text) > 32760:
        return text[:32760] + "…"
    return text


def _sheet_name(value: str) -> str:
    return re.sub(r"[\\/*?:\[\]]", "_", value)[:31]


class DailyExporter:
    def __init__(self, db: Database, data_dir: Path):
        self.db = db
        self.data_dir = data_dir

    def build_tables(self, source_id: int | None = None) -> dict[str, list[dict[str, Any]]]:
        parameters: tuple[Any, ...] = ()
        where = ""
        if source_id is not None:
            where = "WHERE source_id=?"
            parameters = (source_id,)
        rows = self.db.connection.execute(
            f"""
            SELECT source_id, canonical_metric, local_date, revision,
                   summary_json, fidelity, is_partial, created_at
            FROM daily_metrics_current
            {where}
            ORDER BY source_id, local_date, canonical_metric
            """,
            parameters,
        ).fetchall()
        metric_tables: dict[str, list[dict[str, Any]]] = {
            f"{metric}_daily": [] for metric in PRIMARY_METRICS
        }
        overview_by_day: dict[tuple[int, str], dict[str, Any]] = {}
        for row in rows:
            metric = str(row["canonical_metric"])
            summary = json.loads(row["summary_json"])
            flattened = _flatten(summary, skip_keys={"vendor_summaries"})
            base = {
                "source_id": row["source_id"],
                "date": row["local_date"],
                "revision": row["revision"],
                "fidelity": row["fidelity"],
                "partial": bool(row["is_partial"]),
            }
            metric_row = {**base, **flattened}
            metric_tables.setdefault(f"{metric}_daily", []).append(metric_row)
            overview = overview_by_day.setdefault(
                (int(row["source_id"]), str(row["local_date"])),
                {
                    "source_id": row["source_id"],
                    "date": row["local_date"],
                    "partial": bool(row["is_partial"]),
                },
            )
            overview["partial"] = bool(overview["partial"] or row["is_partial"])
            overview[f"{metric}.fidelity"] = row["fidelity"]
            for key, value in flattened.items():
                if key.startswith("computed_from_samples.") or "." not in key:
                    overview[f"{metric}.{key}"] = value
        coverage_where = "WHERE source_id=?" if source_id is not None else ""
        coverage_parameters: tuple[Any, ...] = (
            (source_id,) if source_id is not None else ()
        )
        coverage = [
            dict(row)
            for row in self.db.connection.execute(
                f"""
                SELECT source_id, canonical_metric, local_date, status,
                       detail_json, updated_at
                FROM coverage
                {coverage_where}
                ORDER BY source_id, local_date, canonical_metric
                """,
                coverage_parameters,
            )
        ]
        return {
            "daily_overview": sorted(
                overview_by_day.values(), key=lambda item: (item["source_id"], item["date"])
            ),
            **metric_tables,
            "coverage": coverage,
        }

    def export(
        self,
        formats: set[str],
        *,
        source_id: int | None = None,
        output_dir: Path | None = None,
    ) -> dict[str, Any]:
        stamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        target = output_dir or self.data_dir / "exports" / stamp
        target.mkdir(parents=True, exist_ok=True)
        tables = self.build_tables(source_id)
        outputs: list[str] = []
        if "csv" in formats:
            for name, rows in tables.items():
                path = target / f"{name}.csv"
                self._write_csv(path, name, rows)
                outputs.append(str(path.resolve()))
        if "xlsx" in formats:
            path = target / "health_daily.xlsx"
            self._write_xlsx(path, tables)
            outputs.append(str(path.resolve()))
        manifest = {
            "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_id": source_id,
            "formats": sorted(formats),
            "tables": {name: len(rows) for name, rows in tables.items()},
            "outputs": outputs,
        }
        manifest_path = target / "export_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest

    @staticmethod
    def _columns(table_name: str, rows: list[dict[str, Any]]) -> list[str]:
        preferred = TABLE_BASE_COLUMNS.get(table_name, DAILY_BASE_COLUMNS)
        discovered = {key for row in rows for key in row}
        return list(preferred) + sorted(discovered - set(preferred))

    def _write_csv(
        self,
        path: Path,
        table_name: str,
        rows: list[dict[str, Any]],
    ) -> None:
        columns = self._columns(table_name, rows)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: _safe_cell(value) for key, value in row.items()})

    def _write_xlsx(self, path: Path, tables: dict[str, list[dict[str, Any]]]) -> None:
        workbook = Workbook(write_only=False)
        workbook.remove(workbook.active)
        for name, rows in tables.items():
            sheet = workbook.create_sheet(_sheet_name(name))
            columns = self._columns(name, rows)
            sheet.append(columns)
            for cell in sheet[1]:
                cell.font = Font(bold=True)
            for row in rows:
                sheet.append([_safe_cell(row.get(column)) for column in columns])
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
        workbook.save(path)
