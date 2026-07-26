from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ..adb import AdbClient
from ..artifacts import ArtifactStore
from ..constants import MAIN_ACTIVITY, PACKAGE
from ..db import Database, canonical_json
from ..errors import ExporterError, RootRequiredError
from ..normalization import OppoNormalizer, TABLE_METRICS, json_safe
from .base import SnapshotFile, SnapshotResult, VendorAdapter


APP_DATA_ROOT = f"/data/user/0/{PACKAGE}"


def _safe_relative(remote: str) -> Path:
    relative = remote.removeprefix(APP_DATA_ROOT).lstrip("/")
    parts = [re.sub(r"[^A-Za-z0-9._-]+", "_", part) for part in Path(relative).parts]
    return Path(*parts)


def _sqlite_header(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(16) == b"SQLite format 3\x00"


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


class OppoAdapter(VendorAdapter):
    vendor = "oppo"

    def __init__(
        self,
        adb: AdbClient,
        db: Database,
        artifacts: ArtifactStore,
        data_dir: Path,
        app_version: str,
        restart_app: bool = True,
    ):
        self.adb = adb
        self.db = db
        self.artifacts = artifacts
        self.data_dir = data_dir
        self.app_version = app_version
        self.restart_app = restart_app

    def discover_remote_files(self) -> list[str]:
        if not self.adb.root_available():
            raise RootRequiredError(
                "Root 数据源不可用；请开启 MuMu Root 并确认 `su -c id` 返回 uid=0。"
            )
        script = (
            f"find {self.adb.quote(APP_DATA_ROOT)} -type f "
            "\\( -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' "
            "-o -name '*.db-wal' -o -name '*.db-shm' "
            "-o -name '*.sqlite-wal' -o -name '*.sqlite-shm' \\) 2>/dev/null"
        )
        output = self.adb.su(script)
        return sorted({line.strip() for line in output.splitlines() if line.strip()})

    def discover(self) -> dict[str, Any]:
        files = self.discover_remote_files()
        return {
            "vendor": self.vendor,
            "app_package": PACKAGE,
            "app_version": self.app_version,
            "root": True,
            "database_files": files,
            "target_tables": sorted({name for name in TABLE_METRICS}),
        }

    def snapshot(self, run_id: str) -> SnapshotResult:
        remote_files = self.discover_remote_files()
        if not remote_files:
            raise ExporterError(f"在 {APP_DATA_ROOT} 下未发现数据库文件。")
        run_root = self.data_dir / "raw" / "oppo" / run_id
        manifest_dir = self.data_dir / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        pulled: list[SnapshotFile] = []
        staged_views: list[tuple[str, Path, str]] = []
        self.adb.force_stop(PACKAGE)
        try:
            with tempfile.TemporaryDirectory(prefix="ohealth-root-snapshot-") as temp:
                temp_root = Path(temp)
                for index, remote in enumerate(remote_files):
                    relative = _safe_relative(remote)
                    staging = temp_root / relative
                    token = f"{run_id[:8]}-{index}-{uuid.uuid4().hex[:8]}"
                    self.adb.pull_root_file(remote, staging, token)
                    media_type = "application/vnd.sqlite3" if ".db" in staging.name or ".sqlite" in staging.name else "application/octet-stream"
                    artifact = self.artifacts.put_file(staging, media_type=media_type)
                    view_path = run_root / relative
                    self.artifacts.create_view_link(artifact, view_path)
                    staged_views.append((remote, view_path, artifact.sha256))
                # Inspect only after every DB/WAL/SHM file is present. Opening a
                # database earlier may create and lock a temporary -shm file.
                for remote, view_path, artifact_sha256 in staged_views:
                    status, tables, schema_hash, row_count = self._inspect_file(view_path)
                    self.db.add_raw_snapshot(
                        run_id=run_id,
                        source_path=remote,
                        artifact_sha256=artifact_sha256,
                        sqlite_status=status,
                        schema_hash=schema_hash,
                        table_count=len(tables) if tables else None,
                        row_count=row_count,
                    )
                    pulled.append(
                        SnapshotFile(
                            remote_path=remote,
                            local_path=view_path,
                            artifact_sha256=artifact_sha256,
                            sqlite_status=status,
                            tables=tuple(tables),
                        )
                    )
        finally:
            if self.restart_app:
                self.adb.start_activity(MAIN_ACTIVITY)

        manifest = {
            "run_id": run_id,
            "vendor": self.vendor,
            "app_package": PACKAGE,
            "app_version": self.app_version,
            "captured_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "files": [
                {
                    "remote_path": item.remote_path,
                    "local_path": item.local_path.relative_to(self.data_dir).as_posix(),
                    "sha256": item.artifact_sha256,
                    "sqlite_status": item.sqlite_status,
                    "tables": list(item.tables),
                }
                for item in pulled
            ],
        }
        manifest_path = manifest_dir / f"oppo-snapshot-{run_id}.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self.artifacts.put_file(manifest_path, media_type="application/json")
        return SnapshotResult(run_id, tuple(pulled), manifest_path)

    def _inspect_file(self, path: Path) -> tuple[str, list[str], str | None, int | None]:
        if path.name.endswith(("-wal", "-shm")):
            return "sidecar", [], None, None
        if not _sqlite_header(path):
            return "encrypted_or_non_sqlite", [], None, None
        try:
            with _open_readonly(path) as connection:
                schema_rows = connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
                tables = [str(row["name"]) for row in schema_rows]
                schema = [
                    {"name": row["name"], "sql": row["sql"]} for row in schema_rows
                ]
                schema_hash = hashlib.sha256(canonical_json(schema).encode("utf-8")).hexdigest()
                row_count = 0
                for table in tables:
                    if table.lower() in TABLE_METRICS:
                        quoted = table.replace('"', '""')
                        row_count += int(
                            connection.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0]
                        )
                return "sqlite", tables, schema_hash, row_count
        except sqlite3.Error:
            return "sqlite_unreadable", [], None, None

    def extract(
        self,
        snapshot: SnapshotResult,
        source_id: int,
        run_id: str,
    ) -> dict[str, Any]:
        normalizer = OppoNormalizer(self.db, source_id, run_id)
        inspected: list[dict[str, Any]] = []
        for snapshot_file in snapshot.files:
            if snapshot_file.sqlite_status != "sqlite":
                continue
            database_path = snapshot_file.local_path
            with _open_readonly(database_path) as connection:
                tables = {
                    str(row[0]): str(row[0]).lower()
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                selected = [table for table, lower in tables.items() if lower in TABLE_METRICS]
                for table in sorted(selected):
                    primary_keys = self._primary_keys(connection, table)
                    quoted = table.replace('"', '""')
                    cursor = connection.execute(f'SELECT * FROM "{quoted}"')
                    count = 0
                    while True:
                        rows = cursor.fetchmany(500)
                        if not rows:
                            break
                        with self.db.transaction():
                            for row in rows:
                                payload = {
                                    key: json_safe(row[key]) for key in row.keys()
                                }
                                normalizer.ingest_row(
                                    table,
                                    payload,
                                    primary_keys,
                                    f"{database_path.name}:{table}",
                                )
                                count += 1
                    inspected.append(
                        {
                            "database": database_path.name,
                            "table": table,
                            "rows": count,
                            "primary_keys": primary_keys,
                        }
                    )
        result = self.normalize(normalizer)
        result["inspected"] = inspected
        result["encrypted_files"] = [
            item.remote_path
            for item in snapshot.files
            if item.sqlite_status in {"encrypted_or_non_sqlite", "sqlite_unreadable"}
            and not item.remote_path.endswith(("-wal", "-shm"))
        ]
        return result

    def normalize(self, normalizer: Any) -> dict[str, Any]:
        if not isinstance(normalizer, OppoNormalizer):
            raise TypeError("OPPO adapter requires OppoNormalizer")
        return normalizer.finalize()

    def capture(
        self,
        collector: Any,
        metric: str,
        *,
        earliest_date: dt.date,
        target_dates: tuple[dt.date, ...] | None,
        max_days: int | None,
        resume: bool,
        capture_subpages: bool,
        max_scrolls: int,
    ) -> dict[str, Any]:
        return collector.capture_history(
            metric,
            earliest_date=earliest_date,
            target_dates=target_dates,
            max_days=max_days,
            resume=resume,
            capture_subpages=capture_subpages,
            max_scrolls=max_scrolls,
        )

    def verify(
        self,
        verifier: Any,
        *,
        source_id: int | None,
        verify_all_artifacts: bool,
    ) -> dict[str, Any]:
        return verifier.verify(
            source_id=source_id,
            verify_all_artifacts=verify_all_artifacts,
        )

    @staticmethod
    def _primary_keys(connection: sqlite3.Connection, table: str) -> list[str]:
        quoted = table.replace('"', '""')
        rows = connection.execute(f'PRAGMA table_info("{quoted}")').fetchall()
        return [
            str(row[1])
            for row in sorted(rows, key=lambda value: int(value[5]) if value[5] else 999)
            if int(row[5]) > 0
        ]
