from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from .constants import PRIMARY_METRICS
from .db import Database


def _hash_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


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
            expected_days = (today - first).days + 1
            coverage = int(
                self.db.connection.execute(
                    """
                    SELECT COUNT(*) FROM coverage
                    WHERE source_id=? AND canonical_metric=?
                      AND local_date BETWEEN ? AND ?
                    """,
                    (source, metric, first.isoformat(), today.isoformat()),
                ).fetchone()[0]
            )
            date_ranges[metric] = {
                "first_source_date": first.isoformat(),
                "last_source_date": last.isoformat(),
                "source_days": int(row["count"]),
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
            "coverage_counts": coverage_counts,
            "date_ranges": date_ranges,
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

    def _latest_source_id(self) -> int:
        source = self.db.latest_source()
        if source is None:
            raise RuntimeError("数据库中还没有 OPPO 数据源。")
        return int(source["id"])
