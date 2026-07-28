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
    "dbsleepdatastattable": "sleep",
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
    "dbsleepdatastattable",
    "dbsleepdaystat",
    "dbsleepmainstat",
    "dbsleepdayfrgstat",
    "dbbloodoxygensaturationdatastat",
    "dbbloodpressurestat",
    "dbstressdatastattable",
}

OPPO_SLEEP_STAGE_STATES = {
    "0": "awake",
    "2": "deep",
    "3": "rem",
    "4": "light",
}

STAT_SUMMARY_MAPS: dict[str, dict[str, tuple[str, str | None]]] = {
    "dbsportdatastat": {
        "steps": ("total_steps", "count"),
        "distance_m": ("total_distance", "m"),
        "active_energy_kcal": ("total_calories", "milli_kcal"),
        "duration_raw": ("total_duration", None),
        "workout_minutes": ("total_workout_minutes", "min"),
        "active_count": ("total_move_about_times", "count"),
        "elevation_gain_m": ("total_altitude_offset", "cm"),
        "steps_goal": ("current_day_steps_goal", "count"),
        "energy_goal_kcal": ("current_day_calories_goal", "milli_kcal"),
        "workout_goal_minutes": ("current_day_workout_goal", "min"),
        "active_count_goal": ("current_day_move_about_times_goal", "count"),
        "sedentary_duration_raw": ("sedentary_total_duration", None),
        "sedentary_count": ("sedentary_counts", "count"),
        "static_energy_kcal": ("total_static_cal", "milli_kcal"),
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
    "dbsleepdatastattable": {
        "sleep_start_minute_offset": ("fall_asleep", "min"),
        "sleep_end_minute_offset": ("sleep_out", "min"),
        "total_sleep_raw": ("total_sleep_time", "min"),
        "deep_sleep_raw": ("total_deep_sleep_time", "min"),
        "light_sleep_raw": ("total_lightly_sleep_time", "min"),
        "rem_sleep_raw": ("total_rem_time", "min"),
        "awake_raw": ("total_wake_up_time", "min"),
        "sleep_score": ("sleep_score", "score"),
        "checked_sleep_score": ("checked_sleep_score", "score"),
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


ACTIVITY_SUM_FIELDS = {
    "total_steps",
    "total_distance",
    "total_calories",
    "total_duration",
    "total_workout_minutes",
    "total_move_about_times",
    "total_altitude_offset",
    "sedentary_total_duration",
    "sedentary_counts",
}


def _select_activity_stat(
    payloads: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    if not payloads:
        return None, "missing"

    def newest(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not rows:
            return None
        return max(
            rows,
            key=lambda row: (
                as_number(row.get("modified_time")) or 0,
                as_number(row.get("_id")) or 0,
            ),
        )

    main_visible = [
        row
        for row in payloads
        if as_number(row.get("sport_mode")) == -3 and as_number(row.get("display")) == 1
    ]
    selected = newest(main_visible)
    if selected is not None:
        return selected, "sport_mode_-3_display_1"

    aggregate_rows = [
        row for row in payloads if as_number(row.get("sport_mode")) == -2
    ]
    selected = newest(aggregate_rows)
    if selected is not None:
        return selected, "sport_mode_-2_fallback"

    hidden_main = [
        row for row in payloads if as_number(row.get("sport_mode")) == -3
    ]
    selected = newest(hidden_main)
    if selected is not None:
        return selected, "sport_mode_-3_fallback"

    mode_rows = [
        row
        for row in payloads
        if (as_number(row.get("sport_mode")) or 0) >= 0
    ]
    if not mode_rows:
        return newest(payloads), "single_or_untyped_row"

    combined = dict(mode_rows[0])
    for field in ACTIVITY_SUM_FIELDS:
        values = [as_number(row.get(field)) for row in mode_rows]
        numeric = [value for value in values if value is not None]
        if numeric:
            combined[field] = sum(numeric)
    for field in (
        "current_day_steps_goal",
        "current_day_calories_goal",
        "current_day_workout_goal",
        "current_day_move_about_times_goal",
        "total_static_cal",
    ):
        values = [as_number(row.get(field)) for row in mode_rows]
        numeric = [value for value in values if value is not None]
        if numeric:
            combined[field] = max(numeric)
    combined["sport_mode"] = "aggregated_visible_modes"
    return combined, "sum_nonnegative_modes"


def _activity_summary(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    selected, strategy = _select_activity_stat(payloads)
    if selected is None:
        return {}
    result: dict[str, Any] = {
        "activity_stat_selection": {
            "strategy": strategy,
            "sport_mode": selected.get("sport_mode"),
            "display": selected.get("display"),
            "vendor_id": selected.get("_id"),
        }
    }
    for canonical_name, (vendor_name, vendor_unit) in STAT_SUMMARY_MAPS[
        "dbsportdatastat"
    ].items():
        if vendor_name not in selected:
            continue
        value: Any = selected[vendor_name]
        unit = vendor_unit
        number = as_number(value)
        if vendor_unit == "milli_kcal" and number is not None:
            value = number / 1000
            unit = "kcal"
        elif vendor_unit == "cm" and number is not None:
            value = number / 100
            unit = "m"
        result[canonical_name] = value
        if unit:
            result[f"{canonical_name}__unit"] = unit
    return result


def _annotate_sleep_intervals(
    summary: dict[str, Any], intervals: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    primary_start = as_number(summary.get("sleep_start_utc_ms"))
    primary_end = as_number(summary.get("sleep_end_utc_ms"))
    primary_duration_ms = 0
    annotated: list[dict[str, Any]] = []
    for source in intervals:
        item = dict(source)
        start = as_number(item.get("start_utc_ms"))
        end = as_number(item.get("end_utc_ms"))
        if (
            start is not None
            and end is not None
            and primary_start is not None
            and primary_end is not None
            and start >= primary_start
            and end <= primary_end
        ):
            item["session_relation"] = "primary"
            primary_duration_ms += max(int(end - start), 0)
        elif start is not None and end is not None:
            item["session_relation"] = "additional_session"
        annotated.append(item)
    expected = as_number(summary.get("total_sleep_raw"))
    awake = as_number(summary.get("awake_raw")) or 0
    if expected is not None:
        expected += awake
        summary["interval_validation"] = {
            "primary_expected_minutes": expected,
            "primary_interval_minutes": primary_duration_ms / 60000,
            "delta_minutes": primary_duration_ms / 60000 - expected,
            "complete_within_one_minute": abs(
                primary_duration_ms / 60000 - expected
            )
            <= 1,
        }
    return annotated


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
        elif lower == "dbsleeppiece":
            self._sleep_interval(record_id, bucket, payload, start, end, table)
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
        if table == "dbsportdatastat":
            return
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
            "steps": ("steps", "count", 1.0),
            "distance_m": ("distance", "m", 1.0),
            "active_energy_kcal": ("calories", "kcal", 1000.0),
            "elevation_gain_m": ("altitude_offset", "m", 100.0),
            "exercise_amount": ("amount_of_exercise", "raw", 1.0),
        }
        for kind, (column, unit, divisor) in fields.items():
            if column not in payload:
                continue
            raw_value = payload[column]
            number = as_number(raw_value)
            value = number / divisor if number is not None else raw_value
            original_unit = {
                "calories": "milli_kcal",
                "altitude_offset": "cm",
            }.get(column, unit)
            self.db.add_sample(
                metric_record_id=record_id,
                sample_kind=kind,
                timestamp_utc_ms=start,
                value_num=as_number(value),
                value_text=None,
                unit=unit,
                original_value=raw_value,
                original_unit=original_unit,
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
        source_table: str,
    ) -> None:
        if start is None or end is None or end < start:
            return
        state = payload.get("sleep_state", payload.get("type", payload.get("sleep_type")))
        vendor_state = None if state is None else str(state)
        state_text = (
            OPPO_SLEEP_STAGE_STATES.get(vendor_state, vendor_state)
            if vendor_state is not None
            else None
        )
        metadata = {
            key: payload[key]
            for key in ("sleep_type", "sleep_state", "alg_origin_state", "type", "stage_sleep_end", "data_version")
            if key in payload
        }
        metadata["source_table"] = source_table
        metadata["vendor_state"] = vendor_state
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
        measurement: dict[str, Any] = {"timestamp_utc_ms": stamp}
        raw_weight = payload.get("weight")
        weight_grams = as_number(raw_weight)
        if weight_grams is not None:
            weight_kg = weight_grams / 1000
            self.db.add_sample(
                metric_record_id=record_id,
                sample_kind="weight_kg",
                timestamp_utc_ms=stamp,
                value_num=weight_kg,
                value_text=None,
                unit="kg",
                original_value=raw_weight,
                original_unit="g",
                quality={},
            )
            bucket.series["weight_kg"].append(
                {"timestamp_utc_ms": stamp, "value": weight_kg, "unit": "kg"}
            )
            measurement["weight_kg"] = weight_kg
        if payload.get("bmi") not in (None, ""):
            bmi = as_number(payload["bmi"])
            self._sample(record_id, bucket, "bmi", stamp, payload["bmi"], "kg/m2", payload)
            measurement["bmi"] = bmi if bmi is not None else payload["bmi"]
        for metadata_column in ("metadata", "body_style_text", "body_advice_text", "weight_status"):
            if payload.get(metadata_column) not in (None, ""):
                measurement[metadata_column] = payload[metadata_column]
        bucket.canonical_summary.setdefault("measurements", []).append(measurement)
        latest_stamp = as_number(bucket.canonical_summary.get("latest_measurement_utc_ms"))
        if latest_stamp is None or stamp >= latest_stamp:
            bucket.canonical_summary["latest_measurement_utc_ms"] = stamp
            if "weight_kg" in measurement:
                bucket.canonical_summary["latest_weight_kg"] = measurement["weight_kg"]
                bucket.canonical_summary["latest_weight_kg__unit"] = "kg"
            if "bmi" in measurement:
                bucket.canonical_summary["latest_bmi"] = measurement["bmi"]
                bucket.canonical_summary["latest_bmi__unit"] = "kg/m2"
        bucket.canonical_summary["measurement_count"] = len(
            bucket.canonical_summary["measurements"]
        )

    def finalize(self) -> dict[str, Any]:
        today = dt.datetime.now(ZoneInfo(self.timezone)).date().isoformat()
        new_daily_versions = 0
        with self.db.transaction():
            for (metric, local_date), bucket in sorted(self.days.items()):
                summary = dict(bucket.canonical_summary)
                if metric == "activity":
                    activity_rows = [
                        payload
                        for table, payloads in bucket.vendor_summaries.items()
                        if table.lower() == "dbsportdatastat"
                        for payload in payloads
                    ]
                    summary.update(_activity_summary(activity_rows))
                summary["vendor_summaries"] = dict(bucket.vendor_summaries)
                computed = self._computed_summary(bucket.series)
                if computed:
                    summary["computed_from_samples"] = computed
                chart = {
                    "series": {key: sorted(value, key=lambda item: item.get("timestamp_utc_ms", item.get("start_utc_ms", 0))) for key, value in bucket.series.items()},
                    "intervals": sorted(bucket.intervals, key=lambda item: item["start_utc_ms"]),
                }
                if metric == "sleep":
                    chart["intervals"] = _annotate_sleep_intervals(
                        summary, chart["intervals"]
                    )
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


def repair_weight_units(db: Database, source_id: int, run_id: str) -> dict[str, Any]:
    """Correct legacy OPPO weight samples that treated vendor grams as kg."""

    rows = db.connection.execute(
        """
        SELECT s.id, s.value_num, s.original_value, s.original_unit,
               r.local_date
        FROM metric_samples s
        JOIN metric_records r ON r.id=s.metric_record_id
        WHERE r.source_id=? AND r.canonical_metric='weight'
          AND s.sample_kind='weight_kg' AND s.unit='kg'
          AND s.value_num>=1000
        ORDER BY s.id
        """,
        (source_id,),
    ).fetchall()
    affected_dates: set[str] = set()
    new_daily_versions = 0
    with db.transaction():
        for row in rows:
            raw = as_number(row["original_value"])
            corrected = (raw if raw is not None else float(row["value_num"])) / 1000
            before = {
                "value_num": row["value_num"],
                "original_unit": row["original_unit"],
            }
            after = {"value_num": corrected, "original_unit": "g"}
            db.record_normalization_correction(
                source_id=source_id,
                run_id=run_id,
                entity_table="metric_samples",
                entity_id=int(row["id"]),
                field="weight_kg",
                before=before,
                after=after,
                reason="OPPO DBWeightBodyFatTable.weight is stored in grams",
            )
            db.connection.execute(
                "UPDATE metric_samples SET value_num=?, original_unit='g' WHERE id=?",
                (corrected, row["id"]),
            )
            affected_dates.add(str(row["local_date"]))

        for local_date in sorted(affected_dates):
            current = db.connection.execute(
                """
                SELECT summary_json, chart_json, fidelity, is_partial
                FROM daily_metrics_current
                WHERE source_id=? AND canonical_metric='weight' AND local_date=?
                """,
                (source_id, local_date),
            ).fetchone()
            if current is None:
                continue
            sample_rows = db.connection.execute(
                """
                SELECT s.sample_kind, s.timestamp_utc_ms, s.value_num,
                       s.value_text, s.unit
                FROM metric_samples s
                JOIN metric_records r ON r.id=s.metric_record_id
                WHERE r.source_id=? AND r.canonical_metric='weight'
                  AND r.local_date=?
                ORDER BY s.timestamp_utc_ms, s.sample_kind
                """,
                (source_id, local_date),
            ).fetchall()
            series: dict[str, list[dict[str, Any]]] = defaultdict(list)
            measurements: dict[int, dict[str, Any]] = {}
            for sample in sample_rows:
                stamp = int(sample["timestamp_utc_ms"])
                value = (
                    sample["value_num"]
                    if sample["value_num"] is not None
                    else sample["value_text"]
                )
                kind = str(sample["sample_kind"])
                series[kind].append(
                    {"timestamp_utc_ms": stamp, "value": value, "unit": sample["unit"]}
                )
                measurements.setdefault(stamp, {"timestamp_utc_ms": stamp})[kind] = value

            summary = json.loads(current["summary_json"])
            for item in summary.get("measurements", []):
                if not isinstance(item, dict) or item.get("timestamp_utc_ms") is None:
                    continue
                stamp = int(item["timestamp_utc_ms"])
                measurements.setdefault(stamp, {"timestamp_utc_ms": stamp}).update(item)
            ordered_measurements = [measurements[key] for key in sorted(measurements)]
            summary["measurements"] = ordered_measurements
            summary["measurement_count"] = len(ordered_measurements)
            if ordered_measurements:
                latest = ordered_measurements[-1]
                summary["latest_measurement_utc_ms"] = latest["timestamp_utc_ms"]
                if latest.get("weight_kg") is not None:
                    summary["latest_weight_kg"] = latest["weight_kg"]
                    summary["latest_weight_kg__unit"] = "kg"
                if latest.get("bmi") is not None:
                    summary["latest_bmi"] = latest["bmi"]
                    summary["latest_bmi__unit"] = "kg/m2"
            summary["computed_from_samples"] = OppoNormalizer._computed_summary(series)
            previous_chart = json.loads(current["chart_json"])
            chart = {
                "series": dict(series),
                "intervals": previous_chart.get("intervals", []),
            }
            _, created = db.upsert_daily_metric(
                source_id=source_id,
                canonical_metric="weight",
                local_date=local_date,
                summary=summary,
                chart=chart,
                fidelity=str(current["fidelity"]),
                is_partial=bool(current["is_partial"]),
                run_id=run_id,
            )
            new_daily_versions += int(created)
    return {
        "corrected_samples": len(rows),
        "affected_dates": sorted(affected_dates),
        "new_daily_versions": new_daily_versions,
    }


def repair_activity_normalization(
    db: Database, source_id: int, run_id: str
) -> dict[str, Any]:
    """Correct OPPO activity aggregate selection and vendor subunits."""

    sample_rows = db.connection.execute(
        """
        SELECT s.id, s.sample_kind, s.value_num, s.original_value,
               s.original_unit, r.local_date
        FROM metric_samples s
        JOIN metric_records r ON r.id=s.metric_record_id
        WHERE r.source_id=? AND r.canonical_metric='activity'
          AND (
            (s.sample_kind='active_energy_kcal' AND s.original_unit<>'milli_kcal')
            OR s.sample_kind='floors'
          )
        ORDER BY s.id
        """,
        (source_id,),
    ).fetchall()
    corrected_samples = 0
    affected_dates: set[str] = set()
    new_daily_versions = 0
    with db.transaction():
        for row in sample_rows:
            raw = as_number(row["original_value"])
            if raw is None:
                continue
            if row["sample_kind"] == "active_energy_kcal":
                after = {
                    "sample_kind": "active_energy_kcal",
                    "value_num": raw / 1000,
                    "unit": "kcal",
                    "original_unit": "milli_kcal",
                }
                reason = "OPPO DBSportDataDetail.calories is stored in milli-kcal"
            else:
                after = {
                    "sample_kind": "elevation_gain_m",
                    "value_num": raw / 100,
                    "unit": "m",
                    "original_unit": "cm",
                }
                reason = "OPPO DBSportDataDetail.altitude_offset is stored in centimeters"
            before = {
                "sample_kind": row["sample_kind"],
                "value_num": row["value_num"],
                "original_unit": row["original_unit"],
            }
            db.record_normalization_correction(
                source_id=source_id,
                run_id=run_id,
                entity_table="metric_samples",
                entity_id=int(row["id"]),
                field=str(row["sample_kind"]),
                before=before,
                after=after,
                reason=reason,
            )
            db.connection.execute(
                """
                UPDATE metric_samples
                SET sample_kind=?, value_num=?, unit=?, original_unit=?
                WHERE id=?
                """,
                (
                    after["sample_kind"],
                    after["value_num"],
                    after["unit"],
                    after["original_unit"],
                    int(row["id"]),
                ),
            )
            corrected_samples += 1
            affected_dates.add(str(row["local_date"]))

        daily_rows = db.connection.execute(
            """
            SELECT id, local_date, summary_json, chart_json, fidelity, is_partial
            FROM daily_metrics_current
            WHERE source_id=? AND canonical_metric='activity'
            ORDER BY local_date
            """,
            (source_id,),
        ).fetchall()
        for current in daily_rows:
            local_date = str(current["local_date"])
            original_summary = json.loads(str(current["summary_json"]))
            summary = dict(original_summary)
            vendor_summaries = summary.get("vendor_summaries", {})
            activity_payloads = [
                payload
                for table, payloads in vendor_summaries.items()
                if str(table).lower() == "dbsportdatastat" and isinstance(payloads, list)
                for payload in payloads
                if isinstance(payload, dict)
            ]
            corrected_summary = _activity_summary(activity_payloads)
            if not corrected_summary:
                continue
            for field in (
                "steps",
                "distance_m",
                "active_energy_kcal",
                "duration_raw",
                "workout_minutes",
                "active_count",
                "floors",
                "elevation_gain_m",
                "steps_goal",
                "energy_goal_kcal",
                "workout_goal_minutes",
                "active_count_goal",
                "sedentary_duration_raw",
                "sedentary_count",
                "static_energy_kcal",
            ):
                summary.pop(field, None)
                summary.pop(f"{field}__unit", None)
            summary.update(corrected_summary)

            rows = db.connection.execute(
                """
                SELECT s.sample_kind, s.timestamp_utc_ms, s.value_num,
                       s.value_text, s.unit, s.quality_json
                FROM metric_samples s
                JOIN metric_records r ON r.id=s.metric_record_id
                WHERE r.source_id=? AND r.canonical_metric='activity'
                  AND r.local_date=?
                ORDER BY s.timestamp_utc_ms, s.sample_kind
                """,
                (source_id, local_date),
            ).fetchall()
            series: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for sample in rows:
                value = (
                    sample["value_num"]
                    if sample["value_num"] is not None
                    else sample["value_text"]
                )
                point: dict[str, Any] = {
                    "start_utc_ms": int(sample["timestamp_utc_ms"]),
                    "value": value,
                    "unit": sample["unit"],
                }
                try:
                    quality = json.loads(str(sample["quality_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    quality = {}
                if quality.get("end_utc_ms") is not None:
                    point["end_utc_ms"] = int(quality["end_utc_ms"])
                series[str(sample["sample_kind"])].append(point)
            if series:
                summary["computed_from_samples"] = OppoNormalizer._computed_summary(
                    series
                )
            else:
                summary.pop("computed_from_samples", None)
            previous_chart = json.loads(str(current["chart_json"]))
            chart = {
                "series": dict(series),
                "intervals": previous_chart.get("intervals", []),
            }
            before = {
                "daily_metric_id": int(current["id"]),
                "summary_sha256": hashlib.sha256(
                    canonical_json(original_summary).encode()
                ).hexdigest(),
                "chart_sha256": hashlib.sha256(
                    canonical_json(previous_chart).encode()
                ).hexdigest(),
            }
            if original_summary == summary and previous_chart == chart:
                continue
            new_id, created = db.upsert_daily_metric(
                source_id=source_id,
                canonical_metric="activity",
                local_date=local_date,
                summary=summary,
                chart=chart,
                fidelity=str(current["fidelity"]),
                is_partial=bool(current["is_partial"]),
                run_id=run_id,
            )
            after = {
                "daily_metric_id": new_id,
                "summary_sha256": hashlib.sha256(
                    canonical_json(summary).encode()
                ).hexdigest(),
                "chart_sha256": hashlib.sha256(
                    canonical_json(chart).encode()
                ).hexdigest(),
                "activity_summary": corrected_summary,
            }
            db.record_normalization_correction(
                source_id=source_id,
                run_id=run_id,
                entity_table="daily_metrics",
                entity_id=int(current["id"]),
                field="activity_daily_normalization",
                before=before,
                after=after,
                reason=(
                    "Select OPPO all-day activity aggregate and convert milli-kcal/centimeters"
                ),
            )
            new_daily_versions += int(created)
            affected_dates.add(local_date)

    return {
        "activity_corrected_samples": corrected_samples,
        "activity_affected_dates": sorted(affected_dates),
        "activity_new_daily_versions": new_daily_versions,
    }


def repair_sleep_intervals(
    db: Database, source_id: int, run_id: str
) -> dict[str, Any]:
    """Exclude duplicate minute-resolution DBSleepTable rows from canonical stages."""

    excluded = db.connection.execute(
        """
        SELECT i.id, r.local_date
        FROM metric_intervals i
        JOIN metric_records r ON r.id=i.metric_record_id
        WHERE r.source_id=? AND r.canonical_metric='sleep'
          AND lower(json_extract(r.summary_json, '$.vendor_table'))='dbsleeptable'
        ORDER BY r.local_date, i.id
        """,
        (source_id,),
    ).fetchall()
    excluded_by_date: dict[str, list[int]] = defaultdict(list)
    for row in excluded:
        excluded_by_date[str(row["local_date"])].append(int(row["id"]))

    new_daily_versions = 0
    affected_dates: set[str] = set(excluded_by_date)
    with db.transaction():
        db.connection.execute(
            """
            DELETE FROM metric_intervals
            WHERE id IN (
                SELECT i.id
                FROM metric_intervals i
                JOIN metric_records r ON r.id=i.metric_record_id
                WHERE r.source_id=? AND r.canonical_metric='sleep'
                  AND lower(json_extract(r.summary_json, '$.vendor_table'))='dbsleeptable'
            )
            """,
            (source_id,),
        )

        daily_rows = db.connection.execute(
            """
            SELECT id, local_date, summary_json, chart_json, fidelity, is_partial
            FROM daily_metrics_current
            WHERE source_id=? AND canonical_metric='sleep'
            ORDER BY local_date
            """,
            (source_id,),
        ).fetchall()
        for current in daily_rows:
            local_date = str(current["local_date"])
            rows = db.connection.execute(
                """
                SELECT i.start_utc_ms, i.end_utc_ms, i.state, i.value_num,
                       i.unit, i.metadata_json,
                       json_extract(r.summary_json, '$.vendor_table') AS source_table
                FROM metric_intervals i
                JOIN metric_records r ON r.id=i.metric_record_id
                WHERE r.source_id=? AND r.canonical_metric='sleep'
                  AND r.local_date=?
                ORDER BY i.start_utc_ms, i.end_utc_ms, i.id
                """,
                (source_id, local_date),
            ).fetchall()
            intervals: list[dict[str, Any]] = []
            for interval in rows:
                try:
                    metadata = json.loads(str(interval["metadata_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                metadata["source_table"] = interval["source_table"]
                intervals.append(
                    {
                        "start_utc_ms": int(interval["start_utc_ms"]),
                        "end_utc_ms": int(interval["end_utc_ms"]),
                        "state": interval["state"],
                        "value": interval["value_num"],
                        "unit": interval["unit"],
                        "metadata": metadata,
                    }
                )
            original_summary = json.loads(str(current["summary_json"]))
            summary = dict(original_summary)
            summary.pop("interval_validation", None)
            annotated = _annotate_sleep_intervals(summary, intervals)
            previous_chart = json.loads(str(current["chart_json"]))
            chart = {
                "series": previous_chart.get("series", {}),
                "intervals": annotated,
            }
            before = {
                "daily_metric_id": int(current["id"]),
                "summary_sha256": hashlib.sha256(
                    canonical_json(original_summary).encode()
                ).hexdigest(),
                "chart_sha256": hashlib.sha256(
                    canonical_json(previous_chart).encode()
                ).hexdigest(),
                "excluded_interval_count": len(excluded_by_date.get(local_date, [])),
                "excluded_interval_ids_sha256": hashlib.sha256(
                    canonical_json(excluded_by_date.get(local_date, [])).encode()
                ).hexdigest(),
            }
            if original_summary == summary and previous_chart == chart:
                continue
            new_id, created = db.upsert_daily_metric(
                source_id=source_id,
                canonical_metric="sleep",
                local_date=local_date,
                summary=summary,
                chart=chart,
                fidelity=str(current["fidelity"]),
                is_partial=bool(current["is_partial"]),
                run_id=run_id,
            )
            after = {
                "daily_metric_id": new_id,
                "summary_sha256": hashlib.sha256(
                    canonical_json(summary).encode()
                ).hexdigest(),
                "chart_sha256": hashlib.sha256(
                    canonical_json(chart).encode()
                ).hexdigest(),
                "interval_validation": summary.get("interval_validation"),
            }
            db.record_normalization_correction(
                source_id=source_id,
                run_id=run_id,
                entity_table="daily_metrics",
                entity_id=int(current["id"]),
                field="sleep_canonical_intervals",
                before=before,
                after=after,
                reason=(
                    "Use consolidated DBSleepPiece stages; DBSleepTable minute rows overlap them"
                ),
            )
            new_daily_versions += int(created)
            affected_dates.add(local_date)
    return {
        "sleep_excluded_duplicate_intervals": len(excluded),
        "sleep_affected_dates": sorted(affected_dates),
        "sleep_new_daily_versions": new_daily_versions,
    }
