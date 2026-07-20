from __future__ import annotations

from dataclasses import dataclass


PACKAGE = "com.heytap.health"
MAIN_ACTIVITY = "com.heytap.health/.main.MainActivity"
EXPECTED_APP_VERSION = "6.5.4_48053ce_260715"
EXPECTED_SIZE = "1440x2560"
EXPECTED_DENSITY = "640"
DEFAULT_SERIAL = "127.0.0.1:7555"
DEFAULT_TIMEZONE = "Asia/Shanghai"

FIDELITY_VALUES = {
    "exact_source",
    "exact_ui",
    "visual_only",
    "no_data",
    "failed",
}

PRIMARY_METRICS = (
    "activity",
    "heart_rate",
    "sleep",
    "spo2",
    "blood_pressure",
    "weight",
    "stress",
)

EXCLUDED_HOME_CARDS = {
    "健康趋势",
    "AI 健康助手",
    "AI健康助手",
    "健康旅程",
    "步数排行榜",
    "习惯打卡",
    "身心状态",
    "猜你想问",
}


@dataclass(frozen=True)
class MetricUiProfile:
    metric: str
    home_titles: tuple[str, ...]
    page_titles: tuple[str, ...]
    date_ids: tuple[str, ...]
    previous_ids: tuple[str, ...] = ("iv_pre", "iv_last")
    subpage_titles: tuple[str, ...] = ()


UI_PROFILES = {
    "activity": MetricUiProfile(
        metric="activity",
        home_titles=("每日活动", "步数", "消耗"),
        page_titles=("每日活动",),
        date_ids=("tv_daily_date",),
        subpage_titles=("步数", "活动消耗", "锻炼时长", "活动次数"),
    ),
    "heart_rate": MetricUiProfile(
        metric="heart_rate",
        home_titles=("心率",),
        page_titles=("心率",),
        date_ids=("tv_date",),
        subpage_titles=("静息心率", "步行平均心率", "睡眠基准心率"),
    ),
    "sleep": MetricUiProfile(
        metric="sleep",
        home_titles=("睡眠",),
        page_titles=("睡眠",),
        date_ids=("tv_date", "sleep_date", "tv_sleep_date"),
    ),
    "spo2": MetricUiProfile(
        metric="spo2",
        home_titles=("血氧",),
        page_titles=("血氧",),
        date_ids=("tv_date", "spo2_date", "blood_oxygen_date"),
    ),
    "blood_pressure": MetricUiProfile(
        metric="blood_pressure",
        home_titles=("血压",),
        page_titles=("血压",),
        date_ids=("tv_date", "bp_date", "blood_pressure_date"),
    ),
    "weight": MetricUiProfile(
        metric="weight",
        home_titles=("体重",),
        page_titles=("体重", "公斤"),
        date_ids=("tv_date", "weight_date"),
    ),
    "stress": MetricUiProfile(
        metric="stress",
        home_titles=("压力",),
        page_titles=("压力",),
        date_ids=("tv_date", "stress_date"),
    ),
}
