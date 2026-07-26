from __future__ import annotations

import datetime as dt

from health_exporter.cli import (
    _collection_issues,
    _sparse_calendar_targets,
    build_parser,
)


def test_snapshot_does_not_restart_app_by_default() -> None:
    parser = build_parser()

    assert parser.parse_args(["snapshot-source"]).restart_app is False
    assert parser.parse_args(["snapshot-source", "--restart-app"]).restart_app is True
    assert parser.parse_args(["snapshot-source", "--no-restart-app"]).restart_app is False


def test_repair_normalized_is_an_offline_command() -> None:
    args = build_parser().parse_args(["repair-normalized", "--source-id", "7"])

    assert args.command == "repair-normalized"
    assert args.source_id == 7


def test_collection_issues_rejects_metric_and_inventory_failures() -> None:
    assert _collection_issues(
        ["new card"],
        [
            {"metric": "heart_rate", "failures": 2},
            {"metric": "sleep", "failed": "navigation stopped"},
            {"metric": "weight", "captured": 1, "failures": 0},
        ],
    ) == [
        "unmapped_card:new card",
        "heart_rate:failed_days:2",
        "sleep:exception:navigation stopped",
    ]


def test_sparse_source_dates_choose_calendar_targets() -> None:
    targets, strategy = _sparse_calendar_targets(
        ["2026-01-01", "2025-10-01", "2025-01-01"],
        through_date=dt.date(2026, 1, 31),
    )

    assert targets == (
        dt.date(2026, 1, 1),
        dt.date(2025, 10, 1),
        dt.date(2025, 1, 1),
    )
    assert strategy["mode"] == "calendar_source_dates"
    assert strategy["source_days"] == 3


def test_dense_source_dates_keep_day_by_day_mode() -> None:
    dates = [
        (dt.date(2026, 1, 1) + dt.timedelta(days=offset)).isoformat()
        for offset in range(31)
    ]

    targets, strategy = _sparse_calendar_targets(
        dates,
        through_date=dt.date(2026, 1, 31),
    )

    assert targets is None
    assert strategy["mode"] == "day_by_day"
