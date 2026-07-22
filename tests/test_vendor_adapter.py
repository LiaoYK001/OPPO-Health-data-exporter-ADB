from __future__ import annotations

import datetime as dt
from pathlib import Path

from health_exporter.db import Database
from health_exporter.normalization import OppoNormalizer
from health_exporter.vendor.base import VendorAdapter
from health_exporter.vendor.oppo import OppoAdapter


def test_vendor_contract_requires_all_six_pipeline_stages() -> None:
    assert VendorAdapter.__abstractmethods__ == {
        "discover",
        "snapshot",
        "extract",
        "normalize",
        "capture",
        "verify",
    }
    assert OppoAdapter.__abstractmethods__ == frozenset()


def test_oppo_capture_and_verify_delegate_shared_engines() -> None:
    adapter = object.__new__(OppoAdapter)

    class FakeCollector:
        def capture_history(self, metric: str, **options: object) -> dict[str, object]:
            return {"metric": metric, **options}

    captured = adapter.capture(
        FakeCollector(),
        "heart_rate",
        earliest_date=dt.date(2022, 1, 1),
        max_days=7,
        resume=True,
        capture_subpages=True,
        max_scrolls=20,
    )
    assert captured == {
        "metric": "heart_rate",
        "earliest_date": dt.date(2022, 1, 1),
        "max_days": 7,
        "resume": True,
        "capture_subpages": True,
        "max_scrolls": 20,
    }

    class FakeVerifier:
        def verify(self, **options: object) -> dict[str, object]:
            return options

    assert adapter.verify(
        FakeVerifier(),
        source_id=3,
        verify_all_artifacts=False,
    ) == {"source_id": 3, "verify_all_artifacts": False}


def test_oppo_normalize_finalizes_oppo_normalizer(tmp_path: Path) -> None:
    db = Database(tmp_path / "health.sqlite3", tmp_path / "private")
    source_id = db.ensure_source(
        vendor="oppo",
        app_package="fixture.oppo",
        app_version="fixture",
        installation_hash=db.pseudonymize("fixture"),
        account_hash="unknown:fixture",
        timezone="Asia/Shanghai",
    )
    run_id = db.start_run(source_id, "test", "fixture", "offline", {})
    normalizer = OppoNormalizer(db, source_id, run_id)
    adapter = object.__new__(OppoAdapter)

    result = adapter.normalize(normalizer)

    assert result["daily_buckets"] == 0
    assert result["new_raw_versions"] == 0
    db.close()
