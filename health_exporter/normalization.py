from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .constants import DEFAULT_TIMEZONE
from .db import Database, canonical_json


TABLE_METRICS = {
    "dbsportdatadetail": "activity",
    "dbsportdatastat": "activity",
    "dbheartrate": "heart_rate",
    "dbheartratedatastattable": "heart_rate",
    "dbsleep": "sleep",
    "dbsleeptable": "sleep",
    "dbsleepdaystat": "sleep",
    "dbsleepmainstat": "sleep",
    "dbsleepdayfrgstat": "sleep",
    "dbsleeppiece": "sleep",
    "dbbloodoxygensaturation": "spo2",
    "dbbloodoxygensaturationdatastat": "spo2",
    "dbbloodpressure": "blood_pressure",
    "dbbloodpressurestat": "blood_pressure",
    "dbweightbodyfattable": "weight",
    "dbstresstable": "stress",
    "dbstressdatastattable": "stress",
}

DAILY_TABLES = {
    "dbsportdatastat",
    "dbheartratedatastattable",
    "dbsleepdaystat",
    "dbsleepmainstat",
    "dbsleepdayfrgstat",
    "dbbloodoxygensaturationdatastat",
    "dbbloodpressurestat",
    "dbstressdatastattable",
}

STAT_SUMMARY_MAPS: dict[str, dict[str, tuple[str, str | None]]] = {
    "dbsportdatastat": {
        "steps": ("total_steps", "count"),
        "distance_m": ("total_distance", "m"),
        "active_energy_kcal": ("total_calories", "kcal"),
        "duration_raw": ("total_duration", None),
        "workout_minutes": ("total_workout_minutes", "min"),
        "active_count": ("total_move_about_times", "count"),
        "floors": ("total_altitude_offset", "floor"),
        "steps_goal": ("current_day_steps_goal", "count"),
        "energy_goal_kcal": ("current_day_calories_goal", "kcal"),
        "workout_goal_minutes": ("current_day_workout_goal", "min"),
        "active_count_goal": ("current_day_move_about_times_goal", "count"),
        "sedentary_duration_raw": ("sedentary_total_duration", None),
        "sedentary_count": ("sedentary_counts", "count"),
        "static_energy_kcal": ("total_static_cal", "kcal"),
    },
    "dbheartratedatastattable": {
        "max_bpm": ("max_hr", "bpm"),
        "min_bpm": ("min_hr", "bpm"),
        "average_bpm": ("average_hr", "bpm"),
        "resting_bpm": ("rest_hr", "bpm"),
        "walking_average_bpm": ("walk_avg_hr", "bpm"),
        "sleep_baseline_bpm": ("sleep_base_hr", "bpm"),
    },
    "dbsleepdaystat": {
        "sleep_start_utc_ms": ("sleep_in_timestamp", "ms"),
        "sleep_end_utc_ms": ("sleep_out_timestamp", "ms"),
        "total_sleep_raw": ("total_sleep_time", None),
        "deep_sleep_raw": ("total_deep_sleep_time", None),
        "light_sleep_raw": ("total_lightly_sleep_time", None),
        "rem_sleep_raw": ("total_rem_time", None),
        "awake_raw": ("total_wake_up_time", None),
        "wake_count": ("wake_count", "count"),
        "sleep_score": ("sleep_score", "score"),
    },
    "dbsleepmainstat": {
        "sleep_start_utc_ms": ("sleep_in_timestamp", "ms"),
        "sleep_end_utc_ms": ("sleep_out_timestamp", "ms"),
        "total_sleep_raw": ("total_sleep_time", None),
        "deep_sleep_raw": ("total_deep_sleep_time", None),
        "light_sleep_raw": ("total_lightly_sleep_time", None),
        "rem_sleep_raw": ("total_rem_time", None),
        "awake_raw": ("total_wake_up_time", None),
        "wake_count": ("wake_count", "count"),
    },
    "dbbloodoxygensaturationdatastat": {
        "max_percent": ("max_blood_oxygen_saturation", "%"),
        "min_percent": ("min_blood_oxygen_saturation", "%"),
        "average_percent": ("average_blood_oxygen_saturation", "%"),
        "drop_count": ("blood_oxygen_saturation_drop", "count"),
        "low_total_time_raw": ("low_blood_oxygen_saturation_total_time", None),
    },
    "dbbloodpressurestat": {
        "max_systolic_mmhg": ("max_systolic", "mmHg"),
        "min_systolic_mmhg": ("min_systolic", "mmHg"),
        "average_systolic_mmhg": ("avg_systolic", "mmHg"),
        "max_diastolic_mmhg": ("max_diastolic", "mmHg"),
        "min_diastolic_mmhg": ("min_diastolic", "mmHg"),
        "average_diastolic_mmhg": ("avg_diastolic", "mmHg"),
        "normal_count": ("normal_times", "count"),
        "high_count": ("high_times", "count"),
        "low_count": ("low_times", "count"),
    },
    "dbstressdatastattable": {
        "max": ("max_hr", "score"),
        "min": ("min_hr", "score"),
        "average": ("average_hr", "score"),
        "max_timestamp_utc_ms": ("max_stress_timestamp", "ms"),
        "relaxed_duration_raw": ("relax_stress_total_time", None),
        "normal_duration_raw": ("normal_stress_total_time", None),
        "medium_duration_raw": ("middle_stress_total_time", None),
        "high_duration_raw": ("high_stress_total_time", None),
    },
}


def json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, memoryview):
        return {"$base64": base64.b64encode(value.tobytes()).decode("ascii")}
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def as_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return float(number)


def epoch_ms(value: Any, timezone: str = DEFAULT_TIMEZONE) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            local = dt.datetime.combine(dt.date.fromisoformat(text), dt.time(), ZoneInfo(timezone))
            return int(local.timestamp() * 1000)
        if re.fullmatch(r"\d{8}", text):
            local_date = dt.datetime.strptime(text, "%Y%m%d").date()
            local = dt.datetime.combine(local_date, dt.time(), ZoneInfo(timezone))
            return int(local.timestamp() * 1000)
    number = as_number(value)
    if number is None:
        return None
    absolute = abs(number)
    if 19_000_000 <= absolute <= 29_991_231 and float(number).is_integer():
        try:
            local_date = dt.datetime.strptime(str(int(number)), "%Y%m%d").date()
            local = dt.datetime.combine(local_date, dt.time(), ZoneInfo(timezone))
            return int(local.timestamp() * 1000)
        except ValueError:
            pass
    if absolute >= 100_000_000_000:
        return int(number)
    if absolute >= 100_000_000:
        return int(number * 1000)
    return None


def local_date_for(value: Any, timezone: str = DEFAULT_TIMEZONE) -> str | None:
    millis = epoch_ms(value, timezone)
    if millis is None:
        return None
    return dt.datetime.fromtimestamp(millis / 1000, ZoneInfo(timezone)).date().isoformat()


def row_key(payload: dict[str, Any], primary_keys: Iterable[str]) -> str:
    keys = [key for key in primary_keys if key in payload]
    if keys:
        return "|".join(f"{key}={payload[key]}" for key in keys)
    if "_id" in payload:
        return f"_id={payload['_id']}"
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass
class DayBucket:
    metric: str
    local_date: str
    vendor_summaries: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    canonical_summary: dict[str, Any] = field(default_factory=dict)
    series: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    intervals: list[dict[str, Any]] = field(default_factory=list)


class OppoNormalizer:
    def __init__(
        self,
        db: Database,
        source_id: int,
        run_id: str,
        timezone: str = DEFAULT_TIMEZONE,
    ):
        self.db = db
        self.source_id = source_id
        self.run_id = run_id
        self.timezone = timezone
        self.days: dict[tuple[str, str], DayBucket] = {}
        self.row_counts: defaultdict[str, int] = defaultdict(int)
        self.created_versions = 0
        self.account_value: str | None = None

    def ingest_row(
        self,
        table: str,
        payload: dict[str, Any],
        primary_keys: list[str],
        source_locator: str,
    ) -> None:
        lower = table.lower()
        metric = TABLE_METRICS.get(lower)
        if metric is None:
            return
        payload = json_safe(payload)
        if self.account_value is None and payload.get("ssoid"):
            self.account_value = str(payload["ssoid"])
        vendor_key = row_key(payload, primary_keys)
        raw_version_id, created = self.db.upsert_raw_version(
            source_id=self.source_id,
            vendor_table=table,
            vendor_key=vendor_key,
            run_id=self.run_id,
            payload=payload,
            source_locator=source_locator,
        )
        self.row_counts[table] += 1
        self.created_versions += int(created)

        start = self._first_timestamp(payload, ("start_time", "start_timestamp", "data_created_timestamp", "measure_timestamp", "measurement_timestamp"))
        end = self._first_timestamp(payload, ("end_time", "end_timestamp", "sleep_out_timestamp"))
        local_date = self._row_date(payload, start)
        if local_date is None:
            local_date = "unknown"
        is_partial = local_date == dt.datetime.now(ZoneInfo(self.timezone)).date().isoformat()
        record_id = self.db.add_metric_record(
            source_id=self.source_id,
            raw_version_id=raw_version_id,
            canonical_metric=metric,
            local_date=local_date,
            start_utc_ms=start,
            end_utc_ms=end,
            summary={"vendor_table": table, "vendor_payload": payload},
            fidelity="exact_source",
            is_partial=is_partial,
        )
        if local_date == "unknown":
            return
        bucket = self.days.setdefault((metric, local_date), DayBucket(metric, local_date))
        if lower in DAILY_TABLES:
            bucket.vendor_summaries[table].append(payload)
            self._merge_stat_summary(bucket, lower, payload)

        if lower == "dbsportdatadetail":
            self._activity_detail(record_id, bucket, payload, start, end)
        elif lower == "dbheartrate":
            self._sample(record_id, bucket, "heart_rate_bpm", payload.get("data_created_timestamp"), payload.get("heart_rate_value"), "bpm", payload)
        elif lower in {"dbsleep", "dbsleeptable", "dbsleeppiece"}:
            self._sleep_interval(record_id, bucket, payload, start, end)
        elif lower == "dbbloodoxygensaturation":
            self._sample(record_id, bucket, "spo2_percent", payload.get("data_created_timestamp"), payload.get("blood_oxygen_saturation_value"), "%", payload)
        elif lower == "dbbloodpressure":
            self._blood_pressure(record_id, bucket, payload)
        elif lower == "dbweightbodyfattable":
            self._weight(record_id, bucket, payload)
        elif lower == "dbstresstable":
            self._sample(record_id, bucket, "stress_score", payload.get("data_created_timestamp"), payload.get("stress_value"), "score", payload)

    def _first_timestamp(self, payload: dict[str, Any], keys: Iterable[str]) -> int | None:
        for key in keys:
            value = epoch_ms(payload.get(key), self.timezone)
            if value is not None:
                return value
        return None

    def _row_date(self, payload: dict[str, Any], fallback_ms: int | None) -> str | None:
        if payload.get("date") is not None:
            value = local_date_for(payload["date"], self.timezone)
            if value:
                return value
        if fallback_ms is not None:
            return local_date_for(fallback_ms, self.timezone)
        return None

    def _merge_stat_summary(self, bucket: DayBucket, table: str, payload: dict[str, Any]) -> None:
        mapping = STAT_SUMMARY_MAPS.get(table, {})
        for canonical_name, (vendor_name, unit) in mapping.items():
            if vendor_name not in payload:
                continue
            value = payload[vendor_name]
            bucket.canonical_summary[canonical_name] = value
            if unit:
                bucket.canonical_summary[f"{canonical_name}__unit"] = unit

    def _sample(
        self,
        record_id: int,
        bucket: DayBucket,
        kind: str,
        timestamp: Any,
        value: Any,
        unit: str,
        payload: dict[str, Any],
    ) -> None:
        stamp = epoch_ms(timestamp, self.timezone)
        if stamp is None:
            return
        number = as_number(value)
        self.db.add_sample(
            metric_record_id=record_id,
            sample_kind=kind,
            timestamp_utc_ms=stamp,
            value_num=number,
            value_text=None if number is not None else str(value),
            unit=unit,
            original_value=value,
            original_unit=unit,
            quality={
                key: payload[key]
                for key in ("reliability", "display", "heart_rate_type", "stress_type", "blood_oxygen_saturation_type")
                if key in payload
            },
        )
        bucket.series[kind].append(
            {"timestamp_utc_ms": stamp, "value": number if number is not None else value, "unit": unit}
        )

    def _activity_detail(
        self,
        record_id: int,
        bucket: DayBucket,
        payload: dict[str, Any],
        start: int | None,
        end: int | None,
    ) -> None:
        if start is None:
            return
        fields = {
            "steps": ("steps", "count"),
            "distance_m": ("distance", "m"),
            "active_energy_kcal": ("calories", "kcal"),
            "floors": ("altitude_offset", "floor"),
            "exercise_amount": ("amount_of_exercise", "raw"),
        }
        for kind, (column, unit) in fields.items():
            if column not in payload:
                continue
            value = payload[column]
            self.db.add_sample(
                metric_record_id=record_id,
                sample_kind=kind,
                timestamp_utc_ms=start,
                value_num=as_number(value),
                value_text=None,
                unit=unit,
                original_value=value,
                original_unit=unit,
                quality={"end_utc_ms": end, "sport_mode": payload.get("sport_mode")},
            )
            bucket.series[kind].append(
                {"start_utc_ms": start, "end_utc_ms": end, "value": value, "unit": unit}
            )

    def _sleep_interval(
        self,
        record_id: int,
        bucket: DayBucket,
        payload: dict[str, Any],
        start: int | None,
        end: int | None,
    ) -> None:
        if start is None or end is None or end < start:
            return
        state = payload.get("sleep_state", payload.get("type", payload.get("sleep_type")))
        state_text = None if state is None else str(state)
        metadata = {
            key: payload[key]
            for key in ("sleep_type", "sleep_state", "alg_origin_state", "type", "stage_sleep_end", "data_version")
            if key in payload
        }
        self.db.add_interval(
            metric_record_id=record_id,
            interval_kind="sleep_stage",
            start_utc_ms=start,
            end_utc_ms=end,
            state=state_text,
            value_num=(end - start) / 1000,
            unit="s",
            metadata=metadata,
        )
        bucket.intervals.append(
            {"kind": "sleep_stage", "start_utc_ms": start, "end_utc_ms": end, "state": state_text, "metadata": metadata}
        )

    def _blood_pressure(self, record_id: int, bucket: DayBucket, payload: dict[str, Any]) -> None:
        stamp = epoch_ms(payload.get("measure_timestamp"), self.timezone)
        if stamp is None:
            return
        for kind, column, unit in (
            ("systolic_mmhg", "systolic", "mmHg"),
            ("diastolic_mmhg", "diastolic", "mmHg"),
            ("pulse_bpm", "pulse", "bpm"),
        ):
            if column in payload:
                self._sample(record_id, bucket, kind, stamp, payload[column], unit, payload)

    def _weight(self, record_id: int, bucket: DayBucket, payload: dict[str, Any]) -> None:
        stamp = epoch_ms(payload.get("measurement_timestamp"), self.timezone)
        if stamp is None:
            return
        for kind, column, unit in (
            ("weight_kg", "weight", "kg"),
            ("bmi", "bmi", "kg/m2"),
        ):
            if payload.get(column) not in (None, ""):
                self._sample(record_id, bucket, kind, stamp, payload[column], unit, payload)
        for metadata_column in ("metadata", "body_style_text", "body_advice_text", "weight_status"):
            if payload.get(metadata_column) not in (None, ""):
                bucket.canonical_summary.setdefault("measurements", []).append(
                    {"timestamp_utc_ms": stamp, metadata_column: payload[metadata_column]}
                )

    def finalize(self) -> dict[str, Any]:
        today = dt.datetime.now(ZoneInfo(self.timezone)).date().isoformat()
        new_daily_versions = 0
        with self.db.transaction():
            for (metric, local_date), bucket in sorted(self.days.items()):
                summary = dict(bucket.canonical_summary)
                summary["vendor_summaries"] = dict(bucket.vendor_summaries)
                computed = self._computed_summary(bucket.series)
                if computed:
                    summary["computed_from_samples"] = computed
                chart = {
                    "series": {key: sorted(value, key=lambda item: item.get("timestamp_utc_ms", item.get("start_utc_ms", 0))) for key, value in bucket.series.items()},
                    "intervals": sorted(bucket.intervals, key=lambda item: item["start_utc_ms"]),
                }
                _, created = self.db.upsert_daily_metric(
                    source_id=self.source_id,
                    canonical_metric=metric,
                    local_date=local_date,
                    summary=summary,
                    chart=chart,
                    fidelity="exact_source",
                    is_partial=local_date == today,
                    run_id=self.run_id,
                )
                new_daily_versions += int(created)
                self.db.set_coverage(
                    self.source_id,
                    metric,
                    local_date,
                    "exact_source",
                    {"database": True, "ui": False},
                    self.run_id,
                )
        if self.account_value:
            self.db.update_source_account(self.source_id, self.account_value)
        return {
            "tables": dict(self.row_counts),
            "new_raw_versions": self.created_versions,
            "daily_buckets": len(self.days),
            "new_daily_versions": new_daily_versions,
        }

    @staticmethod
    def _computed_summary(series: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for kind, points in series.items():
            values = [as_number(point.get("value")) for point in points]
            numeric = [value for value in values if value is not None]
            if not numeric:
                continue
            result[kind] = {
                "count": len(numeric),
                "min": min(numeric),
                "max": max(numeric),
                "average": sum(numeric) / len(numeric),
                "sum": sum(numeric),
            }
        return result
