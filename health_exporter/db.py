from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterator

from .constants import FIDELITY_VALUES
from .errors import ExporterError


SCHEMA_VERSION = 3

COVERAGE_STATUS_PRIORITY = {
    "failed": 0,
    "visual_only": 1,
    "no_data": 2,
    "exact_ui": 3,
    "exact_source": 4,
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


SCHEMA_SQL = r"""
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor TEXT NOT NULL,
    app_package TEXT NOT NULL,
    app_version TEXT NOT NULL,
    installation_hash TEXT NOT NULL,
    account_hash TEXT NOT NULL,
    timezone TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(vendor, app_package, installation_hash)
);

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    device_hash TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_id, device_hash)
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id TEXT PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    command TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    app_version TEXT NOT NULL,
    adb_serial TEXT NOT NULL,
    options_json TEXT NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    sha256 TEXT PRIMARY KEY,
    relative_path TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES ingest_runs(id) ON DELETE CASCADE,
    source_path TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
    sqlite_status TEXT NOT NULL,
    schema_hash TEXT,
    table_count INTEGER,
    row_count INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, source_path)
);

CREATE TABLE IF NOT EXISTS raw_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    vendor_table TEXT NOT NULL,
    vendor_key TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    UNIQUE(source_id, vendor_table, vendor_key)
);

CREATE TABLE IF NOT EXISTS raw_record_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_record_id INTEGER NOT NULL REFERENCES raw_records(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES ingest_runs(id),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    source_locator TEXT NOT NULL,
    UNIQUE(raw_record_id, payload_hash)
);

CREATE TABLE IF NOT EXISTS metric_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    raw_version_id INTEGER REFERENCES raw_record_versions(id),
    canonical_metric TEXT NOT NULL,
    local_date TEXT NOT NULL,
    start_utc_ms INTEGER,
    end_utc_ms INTEGER,
    summary_json TEXT NOT NULL,
    fidelity TEXT NOT NULL,
    is_partial INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    CHECK(fidelity IN ('exact_source','exact_ui','visual_only','no_data','failed')),
    UNIQUE(source_id, raw_version_id, canonical_metric, start_utc_ms, end_utc_ms)
);

CREATE TABLE IF NOT EXISTS metric_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_record_id INTEGER NOT NULL REFERENCES metric_records(id) ON DELETE CASCADE,
    sample_kind TEXT NOT NULL,
    timestamp_utc_ms INTEGER NOT NULL,
    value_num REAL,
    value_text TEXT,
    unit TEXT,
    original_value TEXT,
    original_unit TEXT,
    quality_json TEXT NOT NULL,
    UNIQUE(metric_record_id, sample_kind, timestamp_utc_ms)
);

CREATE TABLE IF NOT EXISTS metric_intervals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_record_id INTEGER NOT NULL REFERENCES metric_records(id) ON DELETE CASCADE,
    interval_kind TEXT NOT NULL,
    start_utc_ms INTEGER NOT NULL,
    end_utc_ms INTEGER NOT NULL,
    state TEXT,
    value_num REAL,
    unit TEXT,
    metadata_json TEXT NOT NULL,
    UNIQUE(metric_record_id, interval_kind, start_utc_ms, end_utc_ms, state)
);

CREATE TABLE IF NOT EXISTS daily_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    canonical_metric TEXT NOT NULL,
    local_date TEXT NOT NULL,
    revision INTEGER NOT NULL,
    summary_json TEXT NOT NULL,
    chart_json TEXT NOT NULL,
    fidelity TEXT NOT NULL,
    is_partial INTEGER NOT NULL DEFAULT 0,
    payload_hash TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES ingest_runs(id),
    created_at TEXT NOT NULL,
    CHECK(fidelity IN ('exact_source','exact_ui','visual_only','no_data','failed')),
    UNIQUE(source_id, canonical_metric, local_date, revision),
    UNIQUE(source_id, canonical_metric, local_date, payload_hash)
);

CREATE VIEW IF NOT EXISTS daily_metrics_current AS
SELECT d.*
FROM daily_metrics d
JOIN (
    SELECT source_id, canonical_metric, local_date, MAX(revision) AS revision
    FROM daily_metrics
    GROUP BY source_id, canonical_metric, local_date
) latest
ON latest.source_id=d.source_id
AND latest.canonical_metric=d.canonical_metric
AND latest.local_date=d.local_date
AND latest.revision=d.revision;

CREATE TABLE IF NOT EXISTS page_captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES ingest_runs(id),
    canonical_metric TEXT NOT NULL,
    local_date TEXT NOT NULL,
    page_key TEXT NOT NULL,
    app_activity TEXT NOT NULL,
    app_version TEXT NOT NULL,
    display_profile TEXT NOT NULL,
    display_payload_json TEXT NOT NULL,
    capture_hash TEXT NOT NULL,
    fidelity TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(fidelity IN ('exact_source','exact_ui','visual_only','no_data','failed')),
    UNIQUE(source_id, canonical_metric, local_date, page_key, capture_hash)
);

CREATE TABLE IF NOT EXISTS evidence_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_capture_id INTEGER NOT NULL REFERENCES page_captures(id) ON DELETE CASCADE,
    artifact_sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
    role TEXT NOT NULL,
    viewport_index INTEGER,
    bounds_json TEXT,
    UNIQUE(page_capture_id, artifact_sha256, role, viewport_index)
);

CREATE TABLE IF NOT EXISTS coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    canonical_metric TEXT NOT NULL,
    local_date TEXT NOT NULL,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES ingest_runs(id),
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, canonical_metric, local_date)
);

CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES ingest_runs(id) ON DELETE CASCADE,
    canonical_metric TEXT,
    local_date TEXT,
    stage TEXT NOT NULL,
    error_type TEXT NOT NULL,
    message TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT
);

CREATE TABLE IF NOT EXISTS checkpoints (
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    canonical_metric TEXT NOT NULL,
    last_date TEXT,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(source_id, canonical_metric)
);

CREATE TABLE IF NOT EXISTS normalization_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES ingest_runs(id),
    entity_table TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_id, entity_table, entity_id, field, before_json, after_json)
);

CREATE INDEX IF NOT EXISTS idx_metric_records_day
ON metric_records(source_id, canonical_metric, local_date);
CREATE INDEX IF NOT EXISTS idx_samples_time
ON metric_samples(sample_kind, timestamp_utc_ms);
CREATE INDEX IF NOT EXISTS idx_intervals_time
ON metric_intervals(interval_kind, start_utc_ms, end_utc_ms);
CREATE INDEX IF NOT EXISTS idx_coverage_status
ON coverage(source_id, status, local_date);
"""


class Database:
    def __init__(self, path: Path, private_dir: Path):
        self.path = path
        self.private_dir = private_dir
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.private_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self._salt = self._load_or_create_salt()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _migrate(self) -> None:
        current = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise ExporterError(
                f"数据库版本 {current} 高于程序支持的 {SCHEMA_VERSION}。"
            )
        if current < 1:
            self.connection.executescript(SCHEMA_SQL)
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (1, utc_now()),
            )
            self.connection.execute("PRAGMA user_version=1")
            self.connection.commit()
            current = 1
        if current < 2:
            columns = {
                str(row[1])
                for row in self.connection.execute("PRAGMA table_info(failures)")
            }
            if "resolved_at" not in columns:
                self.connection.execute("ALTER TABLE failures ADD COLUMN resolved_at TEXT")
            if "resolution" not in columns:
                self.connection.execute("ALTER TABLE failures ADD COLUMN resolution TEXT")
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (2, utc_now()),
            )
            self.connection.execute("PRAGMA user_version=2")
            self.connection.commit()
            current = 2
        if current < 3:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS normalization_corrections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES ingest_runs(id),
                    entity_table TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    field TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(
                        source_id, entity_table, entity_id, field,
                        before_json, after_json
                    )
                )
                """
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (3, utc_now()),
            )
            self.connection.execute("PRAGMA user_version=3")
            self.connection.commit()

    def _load_or_create_salt(self) -> bytes:
        salt_path = self.private_dir / "id_salt.bin"
        if salt_path.exists():
            value = salt_path.read_bytes()
            if len(value) < 32:
                raise ExporterError(f"脱敏盐文件损坏：{salt_path}")
            return value
        value = secrets.token_bytes(32)
        salt_path.write_bytes(value)
        return value

    def pseudonymize(self, value: str | None) -> str:
        normalized = (value or "unknown").strip().encode("utf-8")
        return hmac.new(self._salt, normalized, hashlib.sha256).hexdigest()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        savepoint: str | None = None
        try:
            if self.connection.in_transaction:
                savepoint = f"nested_{uuid.uuid4().hex}"
                self.connection.execute(f"SAVEPOINT {savepoint}")
            else:
                self.connection.execute("BEGIN")
            yield self.connection
            if savepoint is not None:
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self.connection.commit()
        except Exception:
            if savepoint is not None:
                self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self.connection.rollback()
            raise

    def ensure_source(
        self,
        *,
        vendor: str,
        app_package: str,
        app_version: str,
        installation_hash: str,
        account_hash: str,
        timezone: str,
    ) -> int:
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO sources(
                vendor, app_package, app_version, installation_hash,
                account_hash, timezone, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(vendor, app_package, installation_hash) DO UPDATE SET
                app_version=excluded.app_version,
                timezone=excluded.timezone,
                account_hash=CASE
                    WHEN excluded.account_hash NOT LIKE 'unknown:%'
                    THEN excluded.account_hash ELSE sources.account_hash END,
                updated_at=excluded.updated_at
            """,
            (
                vendor,
                app_package,
                app_version,
                installation_hash,
                account_hash,
                timezone,
                now,
                now,
            ),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT id FROM sources WHERE vendor=? AND app_package=? AND installation_hash=?",
            (vendor, app_package, installation_hash),
        ).fetchone()
        return int(row["id"])

    def update_source_account(self, source_id: int, account_value: str) -> str:
        account_hash = self.pseudonymize(account_value)
        self.connection.execute(
            "UPDATE sources SET account_hash=?, updated_at=? WHERE id=?",
            (account_hash, utc_now(), source_id),
        )
        self.connection.commit()
        return account_hash

    def upsert_device(
        self,
        *,
        source_id: int,
        device_hash: str,
        metadata: dict[str, Any],
    ) -> int:
        """Register a normalized device without storing its raw stable ID."""
        self.connection.execute(
            """
            INSERT INTO devices(
                source_id, device_hash, metadata_json, created_at
            ) VALUES(?,?,?,?)
            ON CONFLICT(source_id, device_hash) DO UPDATE SET
                metadata_json=excluded.metadata_json
            """,
            (
                source_id,
                device_hash,
                canonical_json(metadata),
                utc_now(),
            ),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT id FROM devices WHERE source_id=? AND device_hash=?",
            (source_id, device_hash),
        ).fetchone()
        return int(row["id"])

    def start_run(
        self,
        source_id: int,
        command: str,
        app_version: str,
        adb_serial: str,
        options: dict[str, Any],
    ) -> str:
        run_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO ingest_runs(
                id, source_id, command, status, started_at, app_version,
                adb_serial, options_json
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                source_id,
                command,
                "running",
                utc_now(),
                app_version,
                adb_serial,
                canonical_json(options),
            ),
        )
        self.connection.commit()
        return run_id

    def finish_run(self, run_id: str, status: str, error: str | None = None) -> None:
        self.connection.execute(
            "UPDATE ingest_runs SET status=?, finished_at=?, error=? WHERE id=?",
            (status, utc_now(), error, run_id),
        )
        self.connection.commit()

    def record_normalization_correction(
        self,
        *,
        source_id: int,
        run_id: str,
        entity_table: str,
        entity_id: int,
        field: str,
        before: Any,
        after: Any,
        reason: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO normalization_corrections(
                source_id, run_id, entity_table, entity_id, field,
                before_json, after_json, reason, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                source_id,
                run_id,
                entity_table,
                entity_id,
                field,
                canonical_json(before),
                canonical_json(after),
                reason,
                utc_now(),
            ),
        )

    def add_artifact(
        self,
        sha256: str,
        relative_path: str,
        media_type: str,
        byte_size: int,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO artifacts(
                sha256, relative_path, media_type, byte_size, width, height, created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (sha256, relative_path, media_type, byte_size, width, height, utc_now()),
        )
        self.connection.commit()

    def add_raw_snapshot(
        self,
        *,
        run_id: str,
        source_path: str,
        artifact_sha256: str,
        sqlite_status: str,
        schema_hash: str | None,
        table_count: int | None,
        row_count: int | None,
    ) -> int:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO raw_snapshots(
                run_id, source_path, artifact_sha256, sqlite_status,
                schema_hash, table_count, row_count, created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                source_path,
                artifact_sha256,
                sqlite_status,
                schema_hash,
                table_count,
                row_count,
                utc_now(),
            ),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT id FROM raw_snapshots WHERE run_id=? AND source_path=?",
            (run_id, source_path),
        ).fetchone()
        return int(row["id"])

    def upsert_raw_version(
        self,
        *,
        source_id: int,
        vendor_table: str,
        vendor_key: str,
        run_id: str,
        payload: dict[str, Any],
        source_locator: str,
    ) -> tuple[int, bool]:
        now = utc_now()
        self.connection.execute(
            """
            INSERT OR IGNORE INTO raw_records(
                source_id, vendor_table, vendor_key, first_seen_at
            ) VALUES(?,?,?,?)
            """,
            (source_id, vendor_table, vendor_key, now),
        )
        raw_record_id = int(
            self.connection.execute(
                "SELECT id FROM raw_records WHERE source_id=? AND vendor_table=? AND vendor_key=?",
                (source_id, vendor_table, vendor_key),
            ).fetchone()["id"]
        )
        encoded = canonical_json(payload)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        before = self.connection.total_changes
        self.connection.execute(
            """
            INSERT OR IGNORE INTO raw_record_versions(
                raw_record_id, run_id, payload_json, payload_hash,
                observed_at, source_locator
            ) VALUES(?,?,?,?,?,?)
            """,
            (raw_record_id, run_id, encoded, digest, now, source_locator),
        )
        created = self.connection.total_changes > before
        row = self.connection.execute(
            "SELECT id FROM raw_record_versions WHERE raw_record_id=? AND payload_hash=?",
            (raw_record_id, digest),
        ).fetchone()
        return int(row["id"]), created

    def add_metric_record(
        self,
        *,
        source_id: int,
        raw_version_id: int | None,
        canonical_metric: str,
        local_date: str,
        start_utc_ms: int | None,
        end_utc_ms: int | None,
        summary: dict[str, Any],
        fidelity: str = "exact_source",
        is_partial: bool = False,
    ) -> int:
        if fidelity not in FIDELITY_VALUES:
            raise ValueError(f"unknown fidelity: {fidelity}")
        self.connection.execute(
            """
            INSERT OR IGNORE INTO metric_records(
                source_id, raw_version_id, canonical_metric, local_date,
                start_utc_ms, end_utc_ms, summary_json, fidelity,
                is_partial, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source_id,
                raw_version_id,
                canonical_metric,
                local_date,
                start_utc_ms,
                end_utc_ms,
                canonical_json(summary),
                fidelity,
                int(is_partial),
                utc_now(),
            ),
        )
        row = self.connection.execute(
            """
            SELECT id FROM metric_records
            WHERE source_id=? AND raw_version_id IS ? AND canonical_metric=?
              AND start_utc_ms IS ? AND end_utc_ms IS ?
            """,
            (
                source_id,
                raw_version_id,
                canonical_metric,
                start_utc_ms,
                end_utc_ms,
            ),
        ).fetchone()
        return int(row["id"])

    def add_sample(
        self,
        *,
        metric_record_id: int,
        sample_kind: str,
        timestamp_utc_ms: int,
        value_num: float | None,
        value_text: str | None,
        unit: str | None,
        original_value: Any,
        original_unit: str | None,
        quality: dict[str, Any] | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO metric_samples(
                metric_record_id, sample_kind, timestamp_utc_ms, value_num,
                value_text, unit, original_value, original_unit, quality_json
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                metric_record_id,
                sample_kind,
                timestamp_utc_ms,
                value_num,
                value_text,
                unit,
                None if original_value is None else str(original_value),
                original_unit,
                canonical_json(quality or {}),
            ),
        )

    def add_interval(
        self,
        *,
        metric_record_id: int,
        interval_kind: str,
        start_utc_ms: int,
        end_utc_ms: int,
        state: str | None,
        value_num: float | None,
        unit: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO metric_intervals(
                metric_record_id, interval_kind, start_utc_ms, end_utc_ms,
                state, value_num, unit, metadata_json
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                metric_record_id,
                interval_kind,
                start_utc_ms,
                end_utc_ms,
                state,
                value_num,
                unit,
                canonical_json(metadata or {}),
            ),
        )

    def upsert_daily_metric(
        self,
        *,
        source_id: int,
        canonical_metric: str,
        local_date: str,
        summary: dict[str, Any],
        chart: dict[str, Any],
        fidelity: str,
        is_partial: bool,
        run_id: str,
    ) -> tuple[int, bool]:
        document = {
            "summary": summary,
            "chart": chart,
            "fidelity": fidelity,
            "is_partial": bool(is_partial),
        }
        digest = payload_hash(document)
        row = self.connection.execute(
            """
            SELECT id FROM daily_metrics
            WHERE source_id=? AND canonical_metric=? AND local_date=? AND payload_hash=?
            """,
            (source_id, canonical_metric, local_date, digest),
        ).fetchone()
        if row:
            return int(row["id"]), False
        revision = int(
            self.connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) + 1
                FROM daily_metrics
                WHERE source_id=? AND canonical_metric=? AND local_date=?
                """,
                (source_id, canonical_metric, local_date),
            ).fetchone()[0]
        )
        cursor = self.connection.execute(
            """
            INSERT INTO daily_metrics(
                source_id, canonical_metric, local_date, revision,
                summary_json, chart_json, fidelity, is_partial,
                payload_hash, run_id, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source_id,
                canonical_metric,
                local_date,
                revision,
                canonical_json(summary),
                canonical_json(chart),
                fidelity,
                int(is_partial),
                digest,
                run_id,
                utc_now(),
            ),
        )
        return int(cursor.lastrowid), True

    def add_page_capture(
        self,
        *,
        source_id: int,
        run_id: str,
        metric: str,
        local_date: str,
        page_key: str,
        app_activity: str,
        app_version: str,
        display_profile: str,
        display_payload: dict[str, Any],
        capture_hash: str,
        fidelity: str,
    ) -> int:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO page_captures(
                source_id, run_id, canonical_metric, local_date, page_key,
                app_activity, app_version, display_profile,
                display_payload_json, capture_hash, fidelity, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source_id,
                run_id,
                metric,
                local_date,
                page_key,
                app_activity,
                app_version,
                display_profile,
                canonical_json(display_payload),
                capture_hash,
                fidelity,
                utc_now(),
            ),
        )
        row = self.connection.execute(
            """
            SELECT id FROM page_captures
            WHERE source_id=? AND canonical_metric=? AND local_date=?
              AND page_key=? AND capture_hash=?
            """,
            (source_id, metric, local_date, page_key, capture_hash),
        ).fetchone()
        return int(row["id"])

    def link_evidence(
        self,
        page_capture_id: int,
        artifact_sha256: str,
        role: str,
        viewport_index: int | None = None,
        bounds: dict[str, Any] | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO evidence_links(
                page_capture_id, artifact_sha256, role, viewport_index, bounds_json
            ) VALUES(?,?,?,?,?)
            """,
            (
                page_capture_id,
                artifact_sha256,
                role,
                viewport_index,
                canonical_json(bounds) if bounds is not None else None,
            ),
        )

    def set_coverage(
        self,
        source_id: int,
        metric: str,
        local_date: str,
        status: str,
        detail: dict[str, Any],
        run_id: str,
    ) -> None:
        existing = self.connection.execute(
            """
            SELECT status, detail_json FROM coverage
            WHERE source_id=? AND canonical_metric=? AND local_date=?
            """,
            (source_id, metric, local_date),
        ).fetchone()
        merged_detail = dict(detail)
        final_status = status
        if existing is not None:
            try:
                previous_detail = json.loads(str(existing["detail_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                previous_detail = {}
            if not isinstance(previous_detail, dict):
                previous_detail = {}
            merged_detail = {**previous_detail, **detail}
            for evidence_kind in ("database", "ui"):
                if evidence_kind in previous_detail or evidence_kind in detail:
                    merged_detail[evidence_kind] = bool(
                        previous_detail.get(evidence_kind)
                        or detail.get(evidence_kind)
                    )
            final_status = max(
                (str(existing["status"]), status),
                key=lambda value: COVERAGE_STATUS_PRIORITY.get(value, -1),
            )
        if merged_detail.get("database"):
            final_status = "exact_source"
        elif merged_detail.get("no_data"):
            final_status = "no_data"
        elif merged_detail.get("ui"):
            final_status = "exact_ui"
        self.connection.execute(
            """
            INSERT INTO coverage(
                source_id, canonical_metric, local_date, status,
                detail_json, run_id, updated_at
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(source_id, canonical_metric, local_date) DO UPDATE SET
                status=excluded.status,
                detail_json=excluded.detail_json,
                run_id=excluded.run_id,
                updated_at=excluded.updated_at
            """,
            (
                source_id,
                metric,
                local_date,
                final_status,
                canonical_json(merged_detail),
                run_id,
                utc_now(),
            ),
        )

    def repair_coverage_statuses(self, source_id: int) -> dict[str, int]:
        """Restore best-fidelity status from independently merged evidence flags."""

        rows = self.connection.execute(
            """
            SELECT id, status, detail_json FROM coverage
            WHERE source_id=? ORDER BY id
            """,
            (source_id,),
        ).fetchall()
        repaired = 0
        with self.transaction():
            for row in rows:
                try:
                    detail = json.loads(str(row["detail_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(detail, dict):
                    continue
                desired = str(row["status"])
                if detail.get("database"):
                    desired = "exact_source"
                elif detail.get("no_data"):
                    desired = "no_data"
                elif detail.get("ui"):
                    desired = "exact_ui"
                if desired == row["status"]:
                    continue
                self.connection.execute(
                    "UPDATE coverage SET status=?, updated_at=? WHERE id=?",
                    (desired, utc_now(), int(row["id"])),
                )
                repaired += 1
        return {"coverage_statuses_repaired": repaired}

    def reconcile_no_data_coverage(
        self,
        source_id: int,
        run_id: str,
        metrics: Iterator[str] | tuple[str, ...] | list[str],
        through_date: dt.date | None = None,
    ) -> dict[str, int]:
        through = through_date or dt.date.today()
        global_first_value = self.connection.execute(
            "SELECT MIN(local_date) FROM daily_metrics_current WHERE source_id=?",
            (source_id,),
        ).fetchone()[0]
        if global_first_value is None:
            return {}
        global_first = dt.date.fromisoformat(str(global_first_value))
        counts: dict[str, int] = {}
        with self.transaction():
            for metric in metrics:
                source_dates = {
                    str(row[0])
                    for row in self.connection.execute(
                        """
                        SELECT local_date FROM daily_metrics_current
                        WHERE source_id=? AND canonical_metric=?
                        """,
                        (source_id, metric),
                    )
                }
                first = (
                    dt.date.fromisoformat(min(source_dates))
                    if source_dates
                    else global_first
                )
                inserted = 0
                day = first
                while day <= through:
                    local_date = day.isoformat()
                    if local_date not in source_dates:
                        existing = self.connection.execute(
                            """
                            SELECT status FROM coverage
                            WHERE source_id=? AND canonical_metric=? AND local_date=?
                            """,
                            (source_id, metric, local_date),
                        ).fetchone()
                        if existing is None or existing["status"] in {"failed", "visual_only"}:
                            self.set_coverage(
                                source_id,
                                metric,
                                local_date,
                                "no_data",
                                {
                                    "database": False,
                                    "ui": False,
                                    "no_data": True,
                                    "basis": "exact_source_absence",
                                },
                                run_id,
                            )
                            inserted += 1
                    day += dt.timedelta(days=1)
                counts[metric] = inserted
        return counts

    def set_checkpoint(
        self,
        source_id: int,
        metric: str,
        last_date: str | None,
        state: dict[str, Any],
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO checkpoints(
                source_id, canonical_metric, last_date, state_json, updated_at
            ) VALUES(?,?,?,?,?)
            ON CONFLICT(source_id, canonical_metric) DO UPDATE SET
                last_date=excluded.last_date,
                state_json=excluded.state_json,
                updated_at=excluded.updated_at
            """,
            (source_id, metric, last_date, canonical_json(state), utc_now()),
        )

    def get_checkpoint(self, source_id: int, metric: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM checkpoints WHERE source_id=? AND canonical_metric=?",
            (source_id, metric),
        ).fetchone()

    def record_failure(
        self,
        run_id: str,
        stage: str,
        error: Exception | str,
        metric: str | None = None,
        local_date: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO failures(
                run_id, canonical_metric, local_date, stage, error_type,
                message, evidence_json, created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                metric,
                local_date,
                stage,
                type(error).__name__ if isinstance(error, Exception) else "Error",
                str(error),
                canonical_json(evidence or {}),
                utc_now(),
            ),
        )

    def resolve_failures(
        self,
        source_id: int,
        *,
        resolution: str,
        metric: str | None = None,
        stage: str | None = None,
    ) -> int:
        clauses = ["run_id IN (SELECT id FROM ingest_runs WHERE source_id=?)", "resolved_at IS NULL"]
        parameters: list[Any] = [source_id]
        if metric is not None:
            clauses.append("canonical_metric=?")
            parameters.append(metric)
        if stage is not None:
            clauses.append("stage=?")
            parameters.append(stage)
        parameters.extend([utc_now(), resolution])
        cursor = self.connection.execute(
            f"""
            UPDATE failures SET resolved_at=?, resolution=?
            WHERE {' AND '.join(clauses)}
            """,
            (*parameters[-2:], *parameters[:-2]),
        )
        self.connection.commit()
        return int(cursor.rowcount)
        self.connection.commit()

    def metric_date_range(self, source_id: int, metric: str) -> tuple[str, str] | None:
        row = self.connection.execute(
            """
            SELECT MIN(local_date) AS first_date, MAX(local_date) AS last_date
            FROM daily_metrics_current
            WHERE source_id=? AND canonical_metric=?
            """,
            (source_id, metric),
        ).fetchone()
        if not row or row["first_date"] is None:
            return None
        return str(row["first_date"]), str(row["last_date"])

    def latest_source(self, vendor: str = "oppo") -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM sources WHERE vendor=? ORDER BY updated_at DESC LIMIT 1",
            (vendor,),
        ).fetchone()

    def latest_run(self, command: str | None = None) -> sqlite3.Row | None:
        if command:
            return self.connection.execute(
                "SELECT * FROM ingest_runs WHERE command=? ORDER BY started_at DESC LIMIT 1",
                (command,),
            ).fetchone()
        return self.connection.execute(
            "SELECT * FROM ingest_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
