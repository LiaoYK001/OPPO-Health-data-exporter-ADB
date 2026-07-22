from __future__ import annotations

from health_exporter.cli import _collection_issues, build_parser


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
