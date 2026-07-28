from __future__ import annotations

from typing import Any


METRIC_CATALOG: dict[str, dict[str, Any]] = {
    "steps": {
        "key": "steps",
        "name": "步数",
        "short_name": "步数",
        "category": "activity",
        "color": "#21c97a",
        "accent": "#0ea863",
        "primary_field": "steps",
        "primary_unit": "步",
        "series_kinds": ["steps"],
        "storage_metric": "activity",
        "long_term_stats": True,
        "summary_fields": [
            {"key": "steps", "label": "步数", "unit": "步"},
            {"key": "steps_goal", "label": "步数目标", "unit": "步"},
            {"key": "distance_m", "label": "距离", "unit": "米"},
            {"key": "elevation_gain_m", "label": "爬升", "unit": "米"},
        ],
    },
    "active_energy": {
        "key": "active_energy",
        "name": "消耗",
        "short_name": "消耗",
        "category": "activity",
        "color": "#ff8a34",
        "accent": "#e66a13",
        "primary_field": "active_energy_kcal",
        "primary_unit": "千卡",
        "series_kinds": ["active_energy_kcal"],
        "storage_metric": "activity",
        "long_term_stats": True,
        "summary_fields": [
            {"key": "active_energy_kcal", "label": "活动消耗", "unit": "千卡"},
            {"key": "energy_goal_kcal", "label": "消耗目标", "unit": "千卡"},
            {"key": "static_energy_kcal", "label": "静息消耗", "unit": "千卡"},
        ],
    },
    "workout_duration": {
        "key": "workout_duration",
        "name": "锻炼时长",
        "short_name": "锻炼时长",
        "category": "activity",
        "color": "#f5b51b",
        "accent": "#d49300",
        "primary_field": "workout_minutes",
        "primary_unit": "分钟",
        "series_kinds": [],
        "storage_metric": "activity",
        "long_term_stats": True,
        "summary_fields": [
            {"key": "workout_minutes", "label": "锻炼时长", "unit": "分钟"},
            {"key": "workout_goal_minutes", "label": "锻炼目标", "unit": "分钟"},
        ],
    },
    "active_count": {
        "key": "active_count",
        "name": "活动次数",
        "short_name": "活动次数",
        "category": "activity",
        "color": "#1ca9f4",
        "accent": "#078bd2",
        "primary_field": "active_count",
        "primary_unit": "次",
        "series_kinds": [],
        "storage_metric": "activity",
        "long_term_stats": True,
        "summary_fields": [
            {"key": "active_count", "label": "活动次数", "unit": "次"},
            {"key": "active_count_goal", "label": "活动次数目标", "unit": "次"},
            {"key": "sedentary_count", "label": "久坐次数", "unit": "次"},
        ],
    },
    "activity": {
        "key": "activity",
        "name": "每日活动（兼容入口）",
        "short_name": "活动",
        "category": "activity",
        "color": "#36d98b",
        "accent": "#13b96f",
        "primary_field": "steps",
        "primary_unit": "步",
        "series_kinds": [
            "steps",
            "active_energy_kcal",
            "distance_m",
            "exercise_amount",
        ],
        "summary_fields": [
            {"key": "steps", "label": "步数", "unit": "步"},
            {"key": "active_energy_kcal", "label": "活动消耗", "unit": "千卡"},
            {"key": "workout_minutes", "label": "锻炼时长", "unit": "分钟"},
            {"key": "active_count", "label": "活动次数", "unit": "次"},
            {"key": "distance_m", "label": "距离", "unit": "米"},
            {"key": "elevation_gain_m", "label": "爬升", "unit": "米"},
            {"key": "steps_goal", "label": "步数目标", "unit": "步"},
            {"key": "energy_goal_kcal", "label": "消耗目标", "unit": "千卡"},
            {"key": "workout_goal_minutes", "label": "锻炼目标", "unit": "分钟"},
            {"key": "active_count_goal", "label": "活动次数目标", "unit": "次"},
        ],
        "hidden": True,
    },
    "heart_rate": {
        "key": "heart_rate",
        "name": "心率",
        "short_name": "心率",
        "category": "vitals",
        "color": "#ff5964",
        "accent": "#e83d4a",
        "primary_field": "average_bpm",
        "primary_unit": "次/分",
        "series_kinds": ["heart_rate_bpm"],
        "summary_fields": [
            {"key": "average_bpm", "label": "平均心率", "unit": "次/分"},
            {"key": "min_bpm", "label": "最低心率", "unit": "次/分"},
            {"key": "max_bpm", "label": "最高心率", "unit": "次/分"},
            {"key": "resting_bpm", "label": "静息心率", "unit": "次/分"},
            {"key": "walking_average_bpm", "label": "步行平均", "unit": "次/分"},
            {"key": "sleep_baseline_bpm", "label": "睡眠基准", "unit": "次/分"},
        ],
    },
    "sleep": {
        "key": "sleep",
        "name": "睡眠",
        "short_name": "睡眠",
        "category": "recovery",
        "color": "#8b7cff",
        "accent": "#6958e8",
        "primary_field": "total_sleep_raw",
        "primary_unit": "分钟",
        "series_kinds": [],
        "interval_kinds": ["sleep_stage"],
        "summary_fields": [
            {"key": "total_sleep_raw", "label": "睡眠时长", "unit": "分钟"},
            {"key": "deep_sleep_raw", "label": "深睡", "unit": "分钟"},
            {"key": "light_sleep_raw", "label": "浅睡", "unit": "分钟"},
            {"key": "rem_sleep_raw", "label": "快速眼动", "unit": "分钟"},
            {"key": "awake_raw", "label": "清醒", "unit": "分钟"},
            {"key": "wake_count", "label": "醒来次数", "unit": "次"},
            {"key": "sleep_score", "label": "睡眠评分", "unit": "分"},
        ],
    },
    "spo2": {
        "key": "spo2",
        "name": "血氧",
        "short_name": "血氧",
        "category": "vitals",
        "color": "#28a9ff",
        "accent": "#0788db",
        "primary_field": "average_percent",
        "primary_unit": "%",
        "series_kinds": ["spo2_percent"],
        "summary_fields": [
            {"key": "average_percent", "label": "平均血氧", "unit": "%"},
            {"key": "min_percent", "label": "最低血氧", "unit": "%"},
            {"key": "max_percent", "label": "最高血氧", "unit": "%"},
            {"key": "drop_count", "label": "下降次数", "unit": "次"},
        ],
    },
    "blood_pressure": {
        "key": "blood_pressure",
        "name": "血压",
        "short_name": "血压",
        "category": "vitals",
        "color": "#ff8a4c",
        "accent": "#e36a27",
        "primary_field": "average_systolic_mmhg",
        "secondary_field": "average_diastolic_mmhg",
        "primary_unit": "mmHg",
        "series_kinds": ["systolic_mmhg", "diastolic_mmhg", "pulse_bpm"],
        "summary_fields": [
            {"key": "average_systolic_mmhg", "label": "平均收缩压", "unit": "mmHg"},
            {"key": "average_diastolic_mmhg", "label": "平均舒张压", "unit": "mmHg"},
            {"key": "min_systolic_mmhg", "label": "最低收缩压", "unit": "mmHg"},
            {"key": "max_systolic_mmhg", "label": "最高收缩压", "unit": "mmHg"},
            {"key": "min_diastolic_mmhg", "label": "最低舒张压", "unit": "mmHg"},
            {"key": "max_diastolic_mmhg", "label": "最高舒张压", "unit": "mmHg"},
        ],
    },
    "weight": {
        "key": "weight",
        "name": "体重与身体成分",
        "short_name": "体重",
        "category": "body",
        "color": "#ee73bc",
        "accent": "#d650a1",
        "primary_field": "latest_weight_kg",
        "primary_unit": "kg",
        "series_kinds": ["weight_kg", "bmi"],
        "summary_fields": [
            {"key": "latest_weight_kg", "label": "体重", "unit": "kg"},
            {"key": "latest_bmi", "label": "BMI", "unit": ""},
            {"key": "measurement_count", "label": "测量次数", "unit": "次"},
        ],
    },
    "stress": {
        "key": "stress",
        "name": "压力",
        "short_name": "压力",
        "category": "recovery",
        "color": "#f6bd45",
        "accent": "#d99a1d",
        "primary_field": "average",
        "primary_unit": "分",
        "series_kinds": ["stress_score"],
        "summary_fields": [
            {"key": "average", "label": "平均压力", "unit": "分"},
            {"key": "min", "label": "最低压力", "unit": "分"},
            {"key": "max", "label": "最高压力", "unit": "分"},
            {"key": "relaxed_duration_raw", "label": "放松时长", "unit": "分钟"},
            {"key": "normal_duration_raw", "label": "正常时长", "unit": "分钟"},
            {"key": "medium_duration_raw", "label": "中等时长", "unit": "分钟"},
            {"key": "high_duration_raw", "label": "高压时长", "unit": "分钟"},
        ],
    },
}

FIDELITY_RANK = {
    "exact_source": 5,
    "exact_ui": 4,
    "visual_only": 3,
    "no_data": 2,
    "failed": 1,
}


def metric_definition(metric: str) -> dict[str, Any]:
    return METRIC_CATALOG[metric]


def storage_metric(metric: str) -> str:
    definition = metric_definition(metric)
    return str(definition.get("storage_metric", metric))


def visible_metrics() -> dict[str, dict[str, Any]]:
    return {
        key: definition
        for key, definition in METRIC_CATALOG.items()
        if not definition.get("hidden")
    }
