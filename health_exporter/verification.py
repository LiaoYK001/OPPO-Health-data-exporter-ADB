from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .constants import PRIMARY_METRICS, UI_PROFILES, MetricUiProfile
from .db import Database
from .ui import DATE_PATTERN, date_from_label


def _hash_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _display_dates(
    payload: dict[str, Any],
    profile: MetricUiProfile,
    reference: dt.date,
) -> list[tuple[str, dt.date]]:
    viewports = payload.get("viewports")
    if not isinstance(viewports, list) or not viewports:
        return []
    nodes = viewports[0].get("nodes")
    if not isinstance(nodes, list):
        return []
    labeled: list[tuple[str, str]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        label = str(node.get("text") or node.get("content_desc") or "").strip()
        if label and DATE_PATTERN.search(label):
            labeled.append((str(node.get("resource_id") or ""), label))
    preferred = [item for item in labeled if item[0] in profile.date_ids]
    selected = preferred or labeled
    result: list[tuple[str, dt.date]] = []
    for _, label in selected:
        parsed = date_from_label(label, reference)
        if parsed is not None and (label, parsed) not in result:
            result.append((label, parsed))
    return result


def _visible_subpage_titles(
    payload: dict[str, Any],
    profile: MetricUiProfile,
) -> set[str]:
    expected = set(profile.subpage_titles)
    if not expected:
        return set()
    result: set[str] = set()
    viewports = payload.get("viewports")
    if not isinstance(viewports, list):
        return result
    for viewport in viewports:
        if not isinstance(viewport, dict):
            continue
        nodes = viewport.get("nodes")
        if not isinstance(nodes, list):
            continue
        for node in nodes:
            if not isinstance(node, dict):
                continue
            label = str(node.get("text") or node.get("content_desc") or "").strip()
            if label in expected:
                result.add(label)
    return result


def _heart_ui_summary(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    viewports = payload.get("viewports")
    if not isinstance(viewports, list):
        return result
    title_fields = {
        "静息心率": "resting_bpm",
        "步行平均心率": "walking_average_bpm",
        "睡眠基准心率": "sleep_baseline_bpm",
    }
    for viewport in viewports:
        if not isinstance(viewport, dict) or not isinstance(viewport.get("nodes"), list):
            continue
        labels = [
            str(node.get("text") or node.get("content_desc") or "").strip()
            for node in viewport["nodes"]
            if isinstance(node, dict)
        ]
        for label in labels:
            match = re.fullmatch(r"(\d{1,3})\s*[-–—]\s*(\d{1,3})", label)
            if match:
                result["range_bpm"] = (int(match.group(1)), int(match.group(2)))
                break
        for title, field in title_fields.items():
            for index, label in enumerate(labels):
                if label != title:
                    continue
                result.setdefault(field, None)
                for candidate in labels[index + 1 : index + 6]:
                    match = re.fullmatch(r"(\d{1,3})\s*次/分", candidate)
                    if match:
                        result[field] = int(match.group(1))
                        break
                break
    return result


def _weight_ui_summary(payload: dict[str, Any]) -> dict[str, Any]:
    viewports = payload.get("viewports")
    if not isinstance(viewports, list):
        return {}
    for viewport in viewports:
        if not isinstance(viewport, dict) or not isinstance(viewport.get("nodes"), list):
            continue
        labels = [
            str(node.get("text") or node.get("content_desc") or "").strip()
            for node in viewport["nodes"]
            if isinstance(node, dict)
        ]
        for index, label in enumerate(labels):
            if label not in {"公斤", "kg"}:
                continue
            for candidate in reversed(labels[max(0, index - 5) : index]):
                if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3})?", candidate):
                    continue
                decimals = len(candidate.split(".", 1)[1]) if "." in candidate else 0
                return {
                    "weight_kg": float(candidate),
                    "weight_kg_decimals": decimals,
                    "weight_kg_text": candidate,
                }
    return {}


def _round_for_display(value: Any, decimals: int) -> float:
    quantum = Decimal("1").scaleb(-decimals)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _sleep_ui_summary(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for label in _all_labels(payload):
        match = re.fullmatch(r"(\d+)\s*小时\s*(\d+)\s*分钟", label)
        if match:
            result["total_sleep_minutes"] = int(match.group(1)) * 60 + int(
                match.group(2)
            )
            break
    resource_fields = {
        "tv_sleep_start_date": ("start_month_day", r"(\d{1,2})/(\d{1,2})"),
        "tv_sleep_end_date": ("end_month_day", r"(\d{1,2})/(\d{1,2})"),
        "tv_sleep_start_time": ("start_time", r"入睡\s*(\d{1,2}):(\d{2})"),
        "tv_sleep_end_time": ("end_time", r"醒来\s*(\d{1,2}):(\d{2})"),
    }
    viewports = payload.get("viewports")
    if not isinstance(viewports, list):
        return result
    for viewport in viewports:
        if not isinstance(viewport, dict) or not isinstance(viewport.get("nodes"), list):
            continue
        for node in viewport["nodes"]:
            if not isinstance(node, dict):
                continue
            spec = resource_fields.get(str(node.get("resource_id") or ""))
            if spec is None:
                continue
            label = str(node.get("text") or node.get("content_desc") or "").strip()
            match = re.fullmatch(spec[1], label)
            if match:
                result[spec[0]] = (int(match.group(1)), int(match.group(2)))
    return result


def _spo2_ui_summary(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for node in _all_nodes(payload):
        label = str(node.get("text") or node.get("content_desc") or "").strip()
        resource_id = str(node.get("resource_id") or "")
        range_match = re.fullmatch(r"(\d{1,3})\s*[-–—]\s*(\d{1,3})", label)
        if range_match and resource_id in {"", "tv_range_value"}:
            result["range_percent"] = (
                int(range_match.group(1)),
                int(range_match.group(2)),
            )
        latest_match = re.fullmatch(
            r"(\d{1,2}):(\d{2})\s*\|\s*(\d{1,3})\s*%",
            label,
        )
        if latest_match:
            result["latest_time"] = (
                int(latest_match.group(1)),
                int(latest_match.group(2)),
            )
            result["latest_percent"] = int(latest_match.group(3))
    return result


def _stress_ui_summary(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    duration_fields = {
        "tv_health_stress_percent_relax_time": "relaxed_duration_minutes",
        "tv_health_stress_percent_normal_time": "normal_duration_minutes",
        "tv_health_stress_percent_medium_time": "medium_duration_minutes",
        "tv_health_stress_percent_high_time": "high_duration_minutes",
    }
    percent_fields = {
        "tv_health_stress_percent_relax_value": "relaxed_percent",
        "tv_health_stress_percent_normal_value": "normal_percent",
        "tv_health_stress_percent_medium_value": "medium_percent",
        "tv_health_stress_percent_high_value": "high_percent",
    }
    for viewport in _nodes_by_viewport(payload):
        labels = [
            str(node.get("text") or node.get("content_desc") or "").strip()
            for node in viewport
        ]
        for label in labels:
            match = re.fullmatch(r"(\d{1,3})\s*[-–—]\s*(\d{1,3})", label)
            if match:
                result["range"] = (int(match.group(1)), int(match.group(2)))
                break
        for index, label in enumerate(labels):
            if label != "平均压力":
                continue
            for candidate in labels[index + 1 : index + 6]:
                if re.fullmatch(r"\d{1,3}", candidate):
                    result["average"] = int(candidate)
                    break
            break
        for index, label in enumerate(labels):
            if label != "最近压力":
                continue
            for candidate in labels[index + 1 : index + 8]:
                value_match = re.fullmatch(r"(\d{1,3})\s*\|.+", candidate)
                if value_match:
                    result["latest_score"] = int(value_match.group(1))
                time_match = re.fullmatch(r"\((\d{1,2}):(\d{2})\)", candidate)
                if time_match:
                    result["latest_time"] = (
                        int(time_match.group(1)),
                        int(time_match.group(2)),
                    )
            break
        for node in viewport:
            label = str(node.get("text") or node.get("content_desc") or "").strip()
            resource_id = str(node.get("resource_id") or "")
            duration_field = duration_fields.get(resource_id)
            if duration_field:
                duration_match = re.fullmatch(
                    r"(?:(\d+)\s*小时)?\s*(\d+)\s*分钟",
                    label,
                )
                if duration_match:
                    result[duration_field] = (
                        int(duration_match.group(1) or 0) * 60
                        + int(duration_match.group(2))
                    )
            percent_field = percent_fields.get(resource_id)
            if percent_field:
                percent_match = re.fullmatch(r"(\d{1,3})\s*%", label)
                if percent_match:
                    result[percent_field] = int(percent_match.group(1))
    return result


def _nodes_by_viewport(payload: dict[str, Any]) -> list[list[dict[str, Any]]]:
    result: list[list[dict[str, Any]]] = []
    viewports = payload.get("viewports")
    if not isinstance(viewports, list):
        return result
    for viewport in viewports:
        if not isinstance(viewport, dict) or not isinstance(viewport.get("nodes"), list):
            continue
        result.append([node for node in viewport["nodes"] if isinstance(node, dict)])
    return result


def _all_nodes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [node for nodes in _nodes_by_viewport(payload) for node in nodes]


def _labels_by_viewport(payload: dict[str, Any]) -> list[list[str]]:
    return [
        [
            label
            for node in nodes
            if (label := str(node.get("text") or node.get("content_desc") or "").strip())
        ]
        for nodes in _nodes_by_viewport(payload)
    ]


def _all_labels(payload: dict[str, Any]) -> list[str]:
    return [label for labels in _labels_by_viewport(payload) for label in labels]


def _activity_ui_summary(payload: dict[str, Any]) -> dict[str, Any]:
    unit_fields = {
        "步": ("steps", "steps_goal"),
        "千卡": ("active_energy_kcal", "energy_goal_kcal"),
        "分钟": ("workout_minutes", "workout_goal_minutes"),
        "次": ("active_count", "active_count_goal"),
    }
    result: dict[str, Any] = {}
    for label in _all_labels(payload):
        match = re.fullmatch(
            r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*(步|千卡|分钟|次)",
            label,
        )
        if not match:
            continue
        value_field, goal_field = unit_fields[match.group(3)]
        value_text, goal_text = match.group(1), match.group(2)
        result[value_field] = float(value_text) if "." in value_text else int(value_text)
        result[goal_field] = float(goal_text) if "." in goal_text else int(goal_text)
        result[f"{value_field}__decimals"] = (
            len(value_text.split(".", 1)[1]) if "." in value_text else 0
        )
        result[f"{goal_field}__decimals"] = (
            len(goal_text.split(".", 1)[1]) if "." in goal_text else 0
        )
    return result


class Verifier:
    def __init__(self, db: Database, data_dir: Path):
        self.db = db
        self.data_dir = data_dir

    def verify(
        self,
        *,
        source_id: int | None = None,
        verify_all_artifacts: bool = True,
    ) -> dict[str, Any]:
        source = source_id or self._latest_source_id()
        failures: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        artifact_rows = self.db.connection.execute(
            "SELECT sha256, relative_path, byte_size FROM artifacts"
        ).fetchall()
        checked_artifacts = 0
        for row in artifact_rows:
            path = self.data_dir / row["relative_path"]
            if not path.is_file():
                failures.append(
                    {"type": "artifact_missing", "sha256": row["sha256"], "path": str(path)}
                )
                continue
            if path.stat().st_size != int(row["byte_size"]):
                failures.append(
                    {"type": "artifact_size", "sha256": row["sha256"], "path": str(path)}
                )
                continue
            if verify_all_artifacts and _hash_file(path) != row["sha256"]:
                failures.append(
                    {"type": "artifact_hash", "sha256": row["sha256"], "path": str(path)}
                )
            checked_artifacts += 1

        page_date_validation = {
            "main_pages": 0,
            "checked": 0,
            "missing": 0,
            "mismatched": 0,
        }
        subpage_validation = {
            "visible_required": 0,
            "matched": 0,
            "missing": 0,
            "pending_main_pages": 0,
            "incomplete_main_pages": 0,
        }
        metric_summary_validation = {
            "activity": {
                "pages_checked": 0,
                "comparisons": 0,
                "matched": 0,
                "missing_ui_value": 0,
                "mismatched": 0,
                "skipped_no_source": 0,
            },
            "heart_rate": {
                "pages_checked": 0,
                "comparisons": 0,
                "matched": 0,
                "missing_ui_value": 0,
                "mismatched": 0,
                "skipped_no_source": 0,
            },
            "weight": {
                "pages_checked": 0,
                "comparisons": 0,
                "matched": 0,
                "missing_ui_value": 0,
                "mismatched": 0,
                "skipped_no_source": 0,
            },
            "sleep": {
                "pages_checked": 0,
                "comparisons": 0,
                "matched": 0,
                "missing_ui_value": 0,
                "mismatched": 0,
                "skipped_no_source": 0,
            },
            "spo2": {
                "pages_checked": 0,
                "comparisons": 0,
                "matched": 0,
                "missing_ui_value": 0,
                "mismatched": 0,
                "skipped_no_source": 0,
            },
            "stress": {
                "pages_checked": 0,
                "comparisons": 0,
                "matched": 0,
                "missing_ui_value": 0,
                "mismatched": 0,
                "skipped_no_source": 0,
            },
        }
        source_row = self.db.connection.execute(
            "SELECT timezone FROM sources WHERE id=?",
            (source,),
        ).fetchone()
        source_timezone = ZoneInfo(
            str(source_row["timezone"]) if source_row is not None else "Asia/Shanghai"
        )
        running_run_ids = {
            str(row["id"])
            for row in self.db.connection.execute(
                "SELECT id FROM ingest_runs WHERE source_id=? AND status='running'",
                (source,),
            )
        }
        completed_ui_days = {
            (str(row["canonical_metric"]), str(row["local_date"]))
            for row in self.db.connection.execute(
                """
                SELECT canonical_metric, local_date FROM coverage
                WHERE source_id=?
                  AND COALESCE(json_extract(detail_json, '$.ui'), 0)=1
                """,
                (source,),
            )
        }
        captured_page_keys = {
            (
                str(row["canonical_metric"]),
                str(row["local_date"]),
                str(row["page_key"]),
            )
            for row in self.db.connection.execute(
                """
                SELECT canonical_metric, local_date, page_key
                FROM page_captures WHERE source_id=?
                """,
                (source,),
            )
        }
        page_rows = self.db.connection.execute(
            """
            SELECT id, run_id, canonical_metric, local_date, page_key,
                   display_payload_json
            FROM page_captures
            WHERE source_id=? AND page_key=canonical_metric
            ORDER BY id
            """,
            (source,),
        ).fetchall()
        for row in page_rows:
            metric = str(row["canonical_metric"])
            profile = UI_PROFILES.get(metric)
            if profile is None:
                continue
            page_date_validation["main_pages"] += 1
            try:
                expected = dt.date.fromisoformat(str(row["local_date"]))
                payload = json.loads(row["display_payload_json"])
                candidates = _display_dates(payload, profile, expected)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                failures.append(
                    {
                        "type": "page_date_payload",
                        "page_capture_id": int(row["id"]),
                        "metric": metric,
                        "error": str(exc),
                    }
                )
                continue
            if not candidates:
                page_date_validation["missing"] += 1
                failures.append(
                    {
                        "type": "page_date_missing",
                        "page_capture_id": int(row["id"]),
                        "metric": metric,
                        "local_date": expected.isoformat(),
                    }
                )
                continue
            page_date_validation["checked"] += 1
            actual_dates = sorted({value.isoformat() for _, value in candidates})
            if expected.isoformat() not in actual_dates:
                page_date_validation["mismatched"] += 1
                failures.append(
                    {
                        "type": "page_date_mismatch",
                        "page_capture_id": int(row["id"]),
                        "metric": metric,
                        "local_date": expected.isoformat(),
                        "display_dates": actual_dates,
                        "labels": [label for label, _ in candidates],
                    }
                )
            visible_subpages = _visible_subpage_titles(payload, profile)
            if visible_subpages and (metric, expected.isoformat()) not in completed_ui_days:
                if str(row["run_id"]) in running_run_ids:
                    subpage_validation["pending_main_pages"] += 1
                    continue
                subpage_validation["incomplete_main_pages"] += 1
                failures.append(
                    {
                        "type": "main_page_incomplete",
                        "page_capture_id": int(row["id"]),
                        "run_id": str(row["run_id"]),
                        "metric": metric,
                        "local_date": expected.isoformat(),
                    }
                )
            for title in sorted(visible_subpages):
                subpage_validation["visible_required"] += 1
                page_key = f"{metric}/{title}"
                if (metric, expected.isoformat(), page_key) in captured_page_keys:
                    subpage_validation["matched"] += 1
                    continue
                subpage_validation["missing"] += 1
                failures.append(
                    {
                        "type": "subpage_capture_missing",
                        "page_capture_id": int(row["id"]),
                        "metric": metric,
                        "local_date": expected.isoformat(),
                        "page_key": page_key,
                    }
                )
            if metric == "heart_rate" and (metric, expected.isoformat()) in completed_ui_days:
                heart_validation = metric_summary_validation["heart_rate"]
                summary_row = self.db.connection.execute(
                    """
                    SELECT summary_json FROM daily_metrics_current
                    WHERE source_id=? AND canonical_metric='heart_rate' AND local_date=?
                    """,
                    (source, expected.isoformat()),
                ).fetchone()
                if summary_row is None:
                    heart_validation["skipped_no_source"] += 1
                    continue
                heart_validation["pages_checked"] += 1
                summary = json.loads(summary_row["summary_json"])
                ui_values = _heart_ui_summary(payload)
                comparisons: list[tuple[str, Any, Any]] = []
                expected_range = (summary.get("min_bpm"), summary.get("max_bpm"))
                comparisons.append(("range_bpm", expected_range, ui_values.get("range_bpm")))
                for field in (
                    "resting_bpm",
                    "walking_average_bpm",
                    "sleep_baseline_bpm",
                ):
                    if field in ui_values:
                        comparisons.append((field, summary.get(field), ui_values[field]))
                for field, expected_value, ui_value in comparisons:
                    heart_validation["comparisons"] += 1
                    if ui_value is None:
                        heart_validation["missing_ui_value"] += 1
                        failures.append(
                            {
                                "type": "ui_summary_missing",
                                "page_capture_id": int(row["id"]),
                                "metric": metric,
                                "local_date": expected.isoformat(),
                                "field": field,
                                "expected": expected_value,
                            }
                        )
                    elif expected_value != ui_value:
                        heart_validation["mismatched"] += 1
                        failures.append(
                            {
                                "type": "ui_summary_mismatch",
                                "page_capture_id": int(row["id"]),
                                "metric": metric,
                                "local_date": expected.isoformat(),
                                "field": field,
                                "expected": expected_value,
                                "actual": ui_value,
                            }
                        )
                    else:
                        heart_validation["matched"] += 1
            if metric == "weight" and (metric, expected.isoformat()) in completed_ui_days:
                weight_validation = metric_summary_validation["weight"]
                summary_row = self.db.connection.execute(
                    """
                    SELECT summary_json FROM daily_metrics_current
                    WHERE source_id=? AND canonical_metric='weight' AND local_date=?
                    """,
                    (source, expected.isoformat()),
                ).fetchone()
                if summary_row is None:
                    weight_validation["skipped_no_source"] += 1
                    continue
                weight_validation["pages_checked"] += 1
                weight_validation["comparisons"] += 1
                summary = json.loads(summary_row["summary_json"])
                ui_values = _weight_ui_summary(payload)
                ui_weight = ui_values.get("weight_kg")
                expected_weight = summary.get("latest_weight_kg")
                if ui_weight is None:
                    weight_validation["missing_ui_value"] += 1
                    failures.append(
                        {
                            "type": "ui_summary_missing",
                            "page_capture_id": int(row["id"]),
                            "metric": metric,
                            "local_date": expected.isoformat(),
                            "field": "weight_kg",
                            "expected": expected_weight,
                        }
                    )
                    continue
                decimals = int(ui_values["weight_kg_decimals"])
                expected_display = (
                    _round_for_display(expected_weight, decimals)
                    if expected_weight is not None
                    else None
                )
                if expected_display != ui_weight:
                    weight_validation["mismatched"] += 1
                    failures.append(
                        {
                            "type": "ui_summary_mismatch",
                            "page_capture_id": int(row["id"]),
                            "metric": metric,
                            "local_date": expected.isoformat(),
                            "field": "weight_kg",
                            "expected": expected_weight,
                            "expected_display": expected_display,
                            "actual": ui_weight,
                            "display_text": ui_values["weight_kg_text"],
                        }
                    )
                else:
                    weight_validation["matched"] += 1
            if metric in {"sleep", "spo2", "stress"} and (
                metric,
                expected.isoformat(),
            ) in completed_ui_days:
                validation = metric_summary_validation[metric]
                summary_row = self.db.connection.execute(
                    """
                    SELECT summary_json FROM daily_metrics_current
                    WHERE source_id=? AND canonical_metric=? AND local_date=?
                    """,
                    (source, metric, expected.isoformat()),
                ).fetchone()
                if summary_row is None:
                    validation["skipped_no_source"] += 1
                    continue
                validation["pages_checked"] += 1
                summary = json.loads(summary_row["summary_json"])
                if metric == "sleep":
                    ui_values = _sleep_ui_summary(payload)
                    start_stamp = summary.get("sleep_start_utc_ms")
                    end_stamp = summary.get("sleep_end_utc_ms")
                    start_local = (
                        dt.datetime.fromtimestamp(
                            float(start_stamp) / 1000, source_timezone
                        )
                        if start_stamp is not None
                        else None
                    )
                    end_local = (
                        dt.datetime.fromtimestamp(
                            float(end_stamp) / 1000, source_timezone
                        )
                        if end_stamp is not None
                        else None
                    )
                    # OPPO stores sleep_out as an exclusive interval boundary,
                    # while the UI labels the final occupied minute.
                    end_display = (
                        end_local - dt.timedelta(minutes=1)
                        if end_local is not None
                        else None
                    )
                    comparisons = [
                        (
                            "total_sleep_minutes",
                            summary.get("total_sleep_raw"),
                            ui_values.get("total_sleep_minutes"),
                        ),
                        (
                            "start_month_day",
                            (start_local.month, start_local.day)
                            if start_local is not None
                            else None,
                            ui_values.get("start_month_day"),
                        ),
                        (
                            "start_time",
                            (start_local.hour, start_local.minute)
                            if start_local is not None
                            else None,
                            ui_values.get("start_time"),
                        ),
                        (
                            "end_month_day",
                            (end_display.month, end_display.day)
                            if end_display is not None
                            else None,
                            ui_values.get("end_month_day"),
                        ),
                        (
                            "end_time",
                            (end_display.hour, end_display.minute)
                            if end_display is not None
                            else None,
                            ui_values.get("end_time"),
                        ),
                    ]
                elif metric == "spo2":
                    ui_values = _spo2_ui_summary(payload)
                    latest_sample = self.db.connection.execute(
                        """
                        SELECT s.timestamp_utc_ms, s.value_num FROM metric_samples s
                        JOIN metric_records r ON r.id=s.metric_record_id
                        WHERE r.source_id=? AND r.canonical_metric='spo2'
                          AND r.local_date=? AND s.sample_kind='spo2_percent'
                        ORDER BY s.timestamp_utc_ms DESC, s.id DESC LIMIT 1
                        """,
                        (source, expected.isoformat()),
                    ).fetchone()
                    latest_local = (
                        dt.datetime.fromtimestamp(
                            float(latest_sample["timestamp_utc_ms"]) / 1000,
                            source_timezone,
                        )
                        if latest_sample is not None
                        else None
                    )
                    comparisons = [
                        (
                            "range_percent",
                            (summary.get("min_percent"), summary.get("max_percent")),
                            ui_values.get("range_percent"),
                        ),
                        (
                            "latest_percent",
                            int(latest_sample["value_num"])
                            if latest_sample is not None
                            and latest_sample["value_num"] is not None
                            else None,
                            ui_values.get("latest_percent"),
                        ),
                        (
                            "latest_time",
                            (latest_local.hour, latest_local.minute)
                            if latest_local is not None
                            else None,
                            ui_values.get("latest_time"),
                        ),
                    ]
                else:
                    ui_values = _stress_ui_summary(payload)
                    latest_sample = self.db.connection.execute(
                        """
                        SELECT s.timestamp_utc_ms, s.value_num FROM metric_samples s
                        JOIN metric_records r ON r.id=s.metric_record_id
                        WHERE r.source_id=? AND r.canonical_metric='stress'
                          AND r.local_date=? AND s.sample_kind='stress_score'
                        ORDER BY s.timestamp_utc_ms DESC, s.id DESC LIMIT 1
                        """,
                        (source, expected.isoformat()),
                    ).fetchone()
                    latest_local = (
                        dt.datetime.fromtimestamp(
                            float(latest_sample["timestamp_utc_ms"]) / 1000,
                            source_timezone,
                        )
                        if latest_sample is not None
                        else None
                    )
                    durations = {
                        "relaxed": summary.get("relaxed_duration_raw"),
                        "normal": summary.get("normal_duration_raw"),
                        "medium": summary.get("medium_duration_raw"),
                        "high": summary.get("high_duration_raw"),
                    }
                    total_duration = sum(
                        int(value) for value in durations.values() if value is not None
                    )
                    comparisons = [
                        (
                            "range",
                            (summary.get("min"), summary.get("max")),
                            ui_values.get("range"),
                        ),
                        ("average", summary.get("average"), ui_values.get("average")),
                        (
                            "latest_score",
                            int(latest_sample["value_num"])
                            if latest_sample is not None
                            and latest_sample["value_num"] is not None
                            else None,
                            ui_values.get("latest_score"),
                        ),
                        (
                            "latest_time",
                            (latest_local.hour, latest_local.minute)
                            if latest_local is not None
                            else None,
                            ui_values.get("latest_time"),
                        ),
                    ]
                    for band, duration in durations.items():
                        comparisons.append(
                            (
                                f"{band}_duration_minutes",
                                int(duration) if duration is not None else None,
                                ui_values.get(f"{band}_duration_minutes"),
                            )
                        )
                        comparisons.append(
                            (
                                f"{band}_percent",
                                int(
                                    Decimal(int(duration) * 100 / total_duration).quantize(
                                        Decimal("1"), rounding=ROUND_HALF_UP
                                    )
                                )
                                if duration is not None and total_duration
                                else 0,
                                ui_values.get(f"{band}_percent"),
                            )
                        )
                for field, expected_value, ui_value in comparisons:
                    validation["comparisons"] += 1
                    if ui_value is None:
                        validation["missing_ui_value"] += 1
                        failures.append(
                            {
                                "type": "ui_summary_missing",
                                "page_capture_id": int(row["id"]),
                                "metric": metric,
                                "local_date": expected.isoformat(),
                                "field": field,
                                "expected": expected_value,
                            }
                        )
                    elif expected_value != ui_value:
                        validation["mismatched"] += 1
                        failures.append(
                            {
                                "type": "ui_summary_mismatch",
                                "page_capture_id": int(row["id"]),
                                "metric": metric,
                                "local_date": expected.isoformat(),
                                "field": field,
                                "expected": expected_value,
                                "actual": ui_value,
                            }
                        )
                    else:
                        validation["matched"] += 1
            if metric == "activity" and (metric, expected.isoformat()) in completed_ui_days:
                validation = metric_summary_validation["activity"]
                summary_row = self.db.connection.execute(
                    """
                    SELECT summary_json FROM daily_metrics_current
                    WHERE source_id=? AND canonical_metric='activity' AND local_date=?
                    """,
                    (source, expected.isoformat()),
                ).fetchone()
                if summary_row is None:
                    validation["skipped_no_source"] += 1
                    continue
                validation["pages_checked"] += 1
                summary = json.loads(summary_row["summary_json"])
                ui_values = _activity_ui_summary(payload)
                for field in (
                    "steps",
                    "steps_goal",
                    "active_energy_kcal",
                    "energy_goal_kcal",
                    "workout_minutes",
                    "workout_goal_minutes",
                    "active_count",
                    "active_count_goal",
                ):
                    validation["comparisons"] += 1
                    ui_value = ui_values.get(field)
                    expected_value = summary.get(field)
                    if ui_value is None:
                        validation["missing_ui_value"] += 1
                        failures.append(
                            {
                                "type": "ui_summary_missing",
                                "page_capture_id": int(row["id"]),
                                "metric": metric,
                                "local_date": expected.isoformat(),
                                "field": field,
                                "expected": expected_value,
                            }
                        )
                        continue
                    decimals = int(ui_values[f"{field}__decimals"])
                    expected_display = (
                        _round_for_display(expected_value, decimals)
                        if expected_value is not None
                        else None
                    )
                    if expected_display != ui_value:
                        validation["mismatched"] += 1
                        failures.append(
                            {
                                "type": "ui_summary_mismatch",
                                "page_capture_id": int(row["id"]),
                                "metric": metric,
                                "local_date": expected.isoformat(),
                                "field": field,
                                "expected": expected_value,
                                "expected_display": expected_display,
                                "actual": ui_value,
                            }
                        )
                    else:
                        validation["matched"] += 1

        source_sequence_validation = self._source_sequence_validation(source)
        sequence_issues = {
            metric: {
                "missing_sequence_days": details["missing_sequence_days"],
                "disagreement_days": details["disagreement_days"],
            }
            for metric, details in source_sequence_validation.items()
            if details["missing_sequence_days"] or details["disagreement_days"]
        }
        if sequence_issues:
            issue = {
                "type": "source_sequence_incomplete",
                "strict": bool(verify_all_artifacts),
                "metrics": sequence_issues,
            }
            if verify_all_artifacts:
                failures.append(issue)
            else:
                warnings.append(issue)

        coverage_counts = {
            str(row["status"]): int(row["count"])
            for row in self.db.connection.execute(
                """
                SELECT status, COUNT(*) AS count FROM coverage
                WHERE source_id=? GROUP BY status
                """,
                (source,),
            )
        }
        missing_metrics = []
        date_ranges: dict[str, dict[str, Any]] = {}
        today = dt.date.today()
        historical_through = today - dt.timedelta(days=1)
        current_day_coverage: dict[str, dict[str, Any]] = {}
        for metric in PRIMARY_METRICS:
            row = self.db.connection.execute(
                """
                SELECT MIN(local_date) AS first_date, MAX(local_date) AS last_date,
                       COUNT(*) AS count
                FROM daily_metrics_current
                WHERE source_id=? AND canonical_metric=?
                """,
                (source, metric),
            ).fetchone()
            if not row or row["first_date"] is None:
                missing_metrics.append(metric)
                warnings.append({"type": "metric_no_source_data", "metric": metric})
                continue
            first = dt.date.fromisoformat(row["first_date"])
            last = dt.date.fromisoformat(row["last_date"])
            expected_days = max((historical_through - first).days + 1, 0)
            coverage = int(
                self.db.connection.execute(
                    """
                    SELECT COUNT(*) FROM coverage
                    WHERE source_id=? AND canonical_metric=?
                      AND local_date BETWEEN ? AND ?
                    """,
                    (
                        source,
                        metric,
                        first.isoformat(),
                        historical_through.isoformat(),
                    ),
                ).fetchone()[0]
            )
            current_row = self.db.connection.execute(
                """
                SELECT status, detail_json, updated_at FROM coverage
                WHERE source_id=? AND canonical_metric=? AND local_date=?
                """,
                (source, metric, today.isoformat()),
            ).fetchone()
            if current_row is None:
                current_day_coverage[metric] = {
                    "local_date": today.isoformat(),
                    "status": "pending",
                }
                warnings.append(
                    {
                        "type": "current_day_pending",
                        "metric": metric,
                        "local_date": today.isoformat(),
                    }
                )
            else:
                current_day_coverage[metric] = {
                    "local_date": today.isoformat(),
                    "status": str(current_row["status"]),
                    "detail": json.loads(str(current_row["detail_json"])),
                    "updated_at": str(current_row["updated_at"]),
                }
            date_ranges[metric] = {
                "first_source_date": first.isoformat(),
                "last_source_date": last.isoformat(),
                "source_days": int(row["count"]),
                "historical_through": historical_through.isoformat(),
                "expected_coverage_days": expected_days,
                "coverage_days": coverage,
            }
            if coverage < expected_days:
                failures.append(
                    {
                        "type": "coverage_gap",
                        "metric": metric,
                        "missing_days": expected_days - coverage,
                    }
                )

        unresolved = [
            dict(row)
            for row in self.db.connection.execute(
                """
                SELECT canonical_metric, local_date, status, detail_json
                FROM coverage
                WHERE source_id=? AND status IN ('failed','visual_only')
                ORDER BY local_date, canonical_metric
                """,
                (source,),
            )
        ]
        if unresolved:
            failures.append({"type": "unresolved_coverage", "count": len(unresolved)})
        recorded_failures = int(
            self.db.connection.execute(
                """
                SELECT COUNT(*) FROM failures f
                JOIN ingest_runs r ON r.id=f.run_id
                WHERE r.source_id=? AND f.resolved_at IS NULL
                """,
                (source,),
            ).fetchone()[0]
        )
        if recorded_failures:
            warnings.append({"type": "recorded_failures", "count": recorded_failures})

        report = {
            "verified_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_id": source,
            "passed": not failures,
            "artifact_count": len(artifact_rows),
            "artifacts_checked": checked_artifacts,
            "page_date_validation": page_date_validation,
            "subpage_validation": subpage_validation,
            "metric_summary_validation": metric_summary_validation,
            "source_sequence_validation": source_sequence_validation,
            "coverage_counts": coverage_counts,
            "date_ranges": date_ranges,
            "current_day_coverage": current_day_coverage,
            "metrics_without_source_data": missing_metrics,
            "unresolved": unresolved,
            "failures": failures,
            "warnings": warnings,
        }
        target = self.data_dir / "reports"
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"verification-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(path.resolve())
        return report

    def _source_sequence_validation(self, source_id: int) -> dict[str, Any]:
        specs = {
            "activity": {
                "sample_kind": "steps",
                "comparisons": (("steps", "sum"),),
            },
            "heart_rate": {
                "sample_kind": "heart_rate_bpm",
                "comparisons": (("min_bpm", "min"), ("max_bpm", "max")),
            },
            "spo2": {
                "sample_kind": "spo2_percent",
                "comparisons": (
                    ("min_percent", "min"),
                    ("max_percent", "max"),
                ),
            },
            "stress": {
                "sample_kind": "stress_score",
                "comparisons": (("min", "min"), ("max", "max")),
            },
            "weight": {
                "sample_kind": "weight_kg",
                "comparisons": (),
            },
        }
        result: dict[str, Any] = {}
        for metric, spec in specs.items():
            daily_rows = self.db.connection.execute(
                """
                SELECT local_date, summary_json
                FROM daily_metrics_current
                WHERE source_id=? AND canonical_metric=?
                ORDER BY local_date
                """,
                (source_id, metric),
            ).fetchall()
            aggregates = {
                str(row["local_date"]): {
                    "count": int(row["sample_count"]),
                    "min": row["min_value"],
                    "max": row["max_value"],
                    "sum": row["sum_value"],
                }
                for row in self.db.connection.execute(
                    """
                    SELECT r.local_date, COUNT(*) AS sample_count,
                           MIN(s.value_num) AS min_value,
                           MAX(s.value_num) AS max_value,
                           SUM(s.value_num) AS sum_value
                    FROM metric_samples s
                    JOIN metric_records r ON r.id=s.metric_record_id
                    WHERE r.source_id=? AND r.canonical_metric=?
                      AND s.sample_kind=?
                    GROUP BY r.local_date
                    """,
                    (source_id, metric, spec["sample_kind"]),
                )
            }
            missing_dates: list[str] = []
            disagreements: list[dict[str, Any]] = []
            for row in daily_rows:
                local_date = str(row["local_date"])
                aggregate = aggregates.get(local_date)
                if aggregate is None:
                    missing_dates.append(local_date)
                    continue
                summary = json.loads(str(row["summary_json"]))
                fields: list[dict[str, Any]] = []
                for summary_field, aggregate_field in spec["comparisons"]:
                    expected = summary.get(summary_field)
                    actual = aggregate[aggregate_field]
                    if expected is None or actual is None:
                        fields.append(
                            {
                                "field": summary_field,
                                "summary": expected,
                                "sequence": actual,
                            }
                        )
                        continue
                    try:
                        matches = Decimal(str(expected)) == Decimal(str(actual))
                    except InvalidOperation:
                        matches = expected == actual
                    if not matches:
                        fields.append(
                            {
                                "field": summary_field,
                                "summary": expected,
                                "sequence": actual,
                            }
                        )
                if fields:
                    disagreements.append({"local_date": local_date, "fields": fields})
            result[metric] = {
                "source_days": len(daily_rows),
                "sequence_days": len(aggregates),
                "missing_sequence_days": len(missing_dates),
                "missing_sequence_dates": missing_dates,
                "disagreement_days": len(disagreements),
                "disagreements": disagreements,
            }

        sleep_rows = self.db.connection.execute(
            """
            SELECT local_date, summary_json
            FROM daily_metrics_current
            WHERE source_id=? AND canonical_metric='sleep'
            ORDER BY local_date
            """,
            (source_id,),
        ).fetchall()
        piece_intervals: dict[str, list[tuple[int, int]]] = {}
        for row in self.db.connection.execute(
            """
            SELECT r.local_date, i.start_utc_ms, i.end_utc_ms
            FROM metric_intervals i
            JOIN metric_records r ON r.id=i.metric_record_id
            WHERE r.source_id=? AND r.canonical_metric='sleep'
              AND lower(json_extract(r.summary_json, '$.vendor_table'))='dbsleeppiece'
            ORDER BY r.local_date, i.start_utc_ms, i.end_utc_ms
            """,
            (source_id,),
        ):
            piece_intervals.setdefault(str(row["local_date"]), []).append(
                (int(row["start_utc_ms"]), int(row["end_utc_ms"]))
            )
        missing_sleep: list[str] = []
        sleep_disagreements: list[dict[str, Any]] = []
        complete_primary = 0
        for row in sleep_rows:
            local_date = str(row["local_date"])
            intervals = piece_intervals.get(local_date)
            if not intervals:
                missing_sleep.append(local_date)
                continue
            summary = json.loads(str(row["summary_json"]))
            start = summary.get("sleep_start_utc_ms")
            end = summary.get("sleep_end_utc_ms")
            total = summary.get("total_sleep_raw")
            awake = summary.get("awake_raw", 0)
            if None in (start, end, total):
                sleep_disagreements.append(
                    {
                        "local_date": local_date,
                        "reason": "primary_summary_boundaries_missing",
                    }
                )
                continue
            primary_ms = sum(
                interval_end - interval_start
                for interval_start, interval_end in intervals
                if interval_start >= int(start) and interval_end <= int(end)
            )
            expected_minutes = float(total) + float(awake or 0)
            actual_minutes = primary_ms / 60000
            delta = actual_minutes - expected_minutes
            if abs(delta) <= 1:
                complete_primary += 1
            else:
                sleep_disagreements.append(
                    {
                        "local_date": local_date,
                        "expected_minutes": expected_minutes,
                        "sequence_minutes": actual_minutes,
                        "delta_minutes": delta,
                    }
                )
        result["sleep"] = {
            "source_days": len(sleep_rows),
            "sequence_days": len(piece_intervals),
            "complete_primary_days": complete_primary,
            "missing_sequence_days": len(missing_sleep),
            "missing_sequence_dates": missing_sleep,
            "disagreement_days": len(sleep_disagreements),
            "disagreements": sleep_disagreements,
        }
        return result

    def _latest_source_id(self) -> int:
        source = self.db.latest_source()
        if source is None:
            raise RuntimeError("数据库中还没有 OPPO 数据源。")
        return int(source["id"])
