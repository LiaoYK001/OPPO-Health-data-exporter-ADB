from __future__ import annotations

import datetime as dt
import getpass
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import sqlite3
import statistics
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .constants import DEFAULT_SERIAL, DEFAULT_TIMEZONE
from .dashboard import DashboardState
from .db import Database
from .errors import ExporterError
from .metric_catalog import (
    FIDELITY_RANK,
    METRIC_CATALOG,
    storage_metric,
    visible_metrics,
)


DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SESSION_COOKIE = "health_archive_session"
SESSION_SECONDS = 12 * 60 * 60
LOGIN_FAILURE_WINDOW_SECONDS = 5 * 60
LOGIN_BLOCK_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
LOGIN_BODY_MAX_BYTES = 4096
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024
OPPO_SLEEP_STAGE_STATES = {
    "0": "awake",
    "2": "deep",
    "3": "rem",
    "4": "light",
}


def _json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _date(value: str, name: str) -> str:
    if not DATE_PATTERN.fullmatch(value):
        raise HTTPException(400, f"{name} 必须是 YYYY-MM-DD")
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, f"{name} 不是有效日期") from exc
    return value


def _date_range(date_from: str, date_to: str) -> tuple[str, str]:
    start = _date(date_from, "from")
    end = _date(date_to, "to")
    if start > end:
        raise HTTPException(400, "from 不能晚于 to")
    return start, end


def _host_allowed(raw_host: str, *, lan_access: bool) -> bool:
    if (
        not raw_host
        or len(raw_host) > 255
        or any(character in raw_host for character in "/\\\r\n\t @")
    ):
        return False
    try:
        parsed = urlsplit(f"//{raw_host}")
        host = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    if not host or parsed.username or parsed.password or parsed.path:
        return False
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    return bool(lan_access and (address.is_private or address.is_link_local))


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _field_statistics(
    days: list[dict[str, Any]], field: str, unit: str
) -> dict[str, Any]:
    values = [
        (str(day["local_date"]), value)
        for day in days
        if (value := _number(day.get("summary", {}).get(field))) is not None
    ]
    if not values:
        return {
            "field": field,
            "unit": unit,
            "recorded_days": 0,
            "total": None,
            "average": None,
            "minimum": None,
            "median": None,
            "maximum": None,
            "first_date": None,
            "last_date": None,
        }
    numbers = [value for _, value in values]
    total = sum(numbers)
    return {
        "field": field,
        "unit": unit,
        "recorded_days": len(numbers),
        "total": total,
        "average": total / len(numbers),
        "minimum": min(numbers),
        "median": statistics.median(numbers),
        "maximum": max(numbers),
        "first_date": values[0][0],
        "last_date": values[-1][0],
    }


def _daily_statistics(
    days: list[dict[str, Any]], definition: dict[str, Any]
) -> dict[str, Any]:
    result = _field_statistics(
        days,
        str(definition["primary_field"]),
        str(definition.get("primary_unit", "")),
    )
    secondary_field = definition.get("secondary_field")
    if secondary_field:
        result["secondary"] = _field_statistics(
            days,
            str(secondary_field),
            str(definition.get("primary_unit", "")),
        )
    return result


def _sample_daily_points(
    days: list[dict[str, Any]],
    definition: dict[str, Any],
    max_points: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    primary_field = str(definition["primary_field"])
    points: list[tuple[int, float, float]] = []
    for index, day in enumerate(days):
        value = _number(day.get("summary", {}).get(primary_field))
        if value is None:
            continue
        x_value = float(dt.date.fromisoformat(str(day["local_date"])).toordinal())
        points.append((index, x_value, value))
    if len(points) <= max_points or max_points < 3:
        return days, {
            "input_days": len(days),
            "valid_points": len(points),
            "returned_points": len(days),
            "downsampled": False,
            "method": "none",
            "max_points": max_points,
        }

    threshold = max_points
    every = (len(points) - 2) / (threshold - 2)
    sampled_positions = [0]
    anchor = 0
    for bucket in range(threshold - 2):
        average_start = int(math.floor((bucket + 1) * every)) + 1
        average_end = int(math.floor((bucket + 2) * every)) + 1
        average_end = min(average_end, len(points))
        if average_start >= average_end:
            average_start = min(average_start, len(points) - 1)
            average_end = min(average_start + 1, len(points))
        average_bucket = points[average_start:average_end]
        average_x = sum(point[1] for point in average_bucket) / len(average_bucket)
        average_y = sum(point[2] for point in average_bucket) / len(average_bucket)

        range_start = int(math.floor(bucket * every)) + 1
        range_end = int(math.floor((bucket + 1) * every)) + 1
        range_end = min(range_end, len(points) - 1)
        if range_start >= range_end:
            range_end = min(range_start + 1, len(points) - 1)

        anchor_x, anchor_y = points[anchor][1], points[anchor][2]
        max_area = -1.0
        next_anchor = range_start
        for candidate in range(range_start, range_end):
            point_x, point_y = points[candidate][1], points[candidate][2]
            area = abs(
                (anchor_x - average_x) * (point_y - anchor_y)
                - (anchor_x - point_x) * (average_y - anchor_y)
            )
            if area > max_area:
                max_area = area
                next_anchor = candidate
        sampled_positions.append(next_anchor)
        anchor = next_anchor
    sampled_positions.append(len(points) - 1)
    sampled = [days[points[position][0]] for position in sampled_positions]
    return sampled, {
        "input_days": len(days),
        "valid_points": len(points),
        "returned_points": len(sampled),
        "downsampled": True,
        "method": "lttb",
        "max_points": max_points,
    }


def _trend_day(
    day: dict[str, Any], definition: dict[str, Any]
) -> dict[str, Any]:
    result = dict(day)
    fields = [str(definition["primary_field"])]
    if definition.get("secondary_field"):
        fields.append(str(definition["secondary_field"]))
    summary = day.get("summary", {})
    result["summary"] = {
        field: summary[field]
        for field in fields
        if field in summary
    }
    return result


class WebAuth:
    def __init__(self, config_path: Path, enabled: bool):
        self.config_path = config_path
        self.enabled = enabled
        self._sessions: dict[str, float] = {}
        self._login_failures: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()
        if self.config_path.parent.exists():
            try:
                os.chmod(self.config_path.parent, 0o700)
                if self.config_path.exists():
                    os.chmod(self.config_path, 0o600)
            except OSError:
                pass

    def configured(self) -> bool:
        return self.config_path.is_file()

    def set_password(self, password: str) -> None:
        if len(password) < 8:
            raise ExporterError("局域网访问密码至少需要 8 个字符。")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=SCRYPT_DKLEN,
            maxmem=SCRYPT_MAXMEM,
        )
        payload = {
            "version": 2,
            "algorithm": "scrypt",
            "n": SCRYPT_N,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
            "dklen": SCRYPT_DKLEN,
            "salt": salt.hex(),
            "digest": digest.hex(),
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            os.chmod(self.config_path.parent, 0o700)
            os.chmod(self.config_path, 0o600)
        except OSError:
            pass

    def configure_interactively(self, *, force: bool = False) -> None:
        if not self.enabled or (self.configured() and not force):
            return
        if not sys.stdin.isatty():
            raise ExporterError(
                "首次开启 --lan 需要在交互式终端设置访问密码。"
            )
        print("首次开启局域网访问，请设置 Web 登录密码（至少 8 个字符）。")
        first = getpass.getpass("新密码：")
        second = getpass.getpass("再次输入：")
        if first != second:
            raise ExporterError("两次输入的密码不一致。")
        self.set_password(first)

    def retry_after(self, client_key: str) -> int:
        now = time.time()
        with self._lock:
            self._purge_login_attempts(now)
            return max(0, math.ceil(self._blocked_until.get(client_key, 0) - now))

    def verify_password(self, password: str, client_key: str = "local") -> bool:
        if not self.enabled:
            return True
        if self.retry_after(client_key):
            return False
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
            salt = bytes.fromhex(payload["salt"])
            expected = bytes.fromhex(payload["digest"])
            n = int(payload.get("n", 2**14))
            r = int(payload.get("r", 8))
            p = int(payload.get("p", 1))
            dklen = int(payload.get("dklen", len(expected)))
            if (
                n not in {2**14, 2**15}
                or r not in {8}
                or p not in {1, 2}
                or dklen != len(expected)
                or not 16 <= dklen <= 64
            ):
                return False
            actual = hashlib.scrypt(
                password.encode("utf-8"),
                salt=salt,
                n=n,
                r=r,
                p=p,
                dklen=dklen,
                maxmem=SCRYPT_MAXMEM,
            )
            valid = secrets.compare_digest(actual, expected)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            valid = False
        now = time.time()
        with self._lock:
            self._purge_login_attempts(now)
            if valid:
                self._login_failures.pop(client_key, None)
                self._blocked_until.pop(client_key, None)
            else:
                failures = self._login_failures.setdefault(client_key, [])
                failures.append(now)
                if len(failures) >= LOGIN_MAX_FAILURES:
                    self._blocked_until[client_key] = now + LOGIN_BLOCK_SECONDS
                    self._login_failures.pop(client_key, None)
        return valid

    def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge()
            self._sessions[token] = time.time() + SESSION_SECONDS
        return token

    def authenticated(self, request: Request) -> bool:
        if not self.enabled:
            return True
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return False
        with self._lock:
            self._purge()
            expiry = self._sessions.get(token)
            return bool(expiry and expiry > time.time())

    def revoke(self, request: Request) -> None:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            with self._lock:
                self._sessions.pop(token, None)

    def _purge(self) -> None:
        now = time.time()
        for token, expiry in list(self._sessions.items()):
            if expiry <= now:
                self._sessions.pop(token, None)

    def _purge_login_attempts(self, now: float) -> None:
        cutoff = now - LOGIN_FAILURE_WINDOW_SECONDS
        for client_key, values in list(self._login_failures.items()):
            remaining = [value for value in values if value >= cutoff]
            if remaining:
                self._login_failures[client_key] = remaining
            else:
                self._login_failures.pop(client_key, None)
        for client_key, expiry in list(self._blocked_until.items()):
            if expiry <= now:
                self._blocked_until.pop(client_key, None)


class WebDataService:
    def __init__(self, workspace: Path, data_dir: Path):
        self.workspace = workspace.resolve()
        self.data_dir = data_dir.resolve()
        self.database_path = self.data_dir / "health.sqlite3"

    def connection(self) -> sqlite3.Connection:
        if not self.database_path.is_file():
            raise HTTPException(503, f"数据库不存在：{self.database_path}")
        connection = sqlite3.connect(
            f"file:{self.database_path.as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _profile_id(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT id FROM profiles WHERE profile_key='default'"
        ).fetchone()
        if row is None:
            raise HTTPException(503, "默认健康档案尚未初始化")
        return int(row["id"])

    def meta(self) -> dict[str, Any]:
        with self.connection() as connection:
            earliest_daily = connection.execute(
                "SELECT local_date FROM daily_metrics ORDER BY local_date LIMIT 1"
            ).fetchone()
            latest_daily = connection.execute(
                """
                SELECT local_date FROM daily_metrics
                 ORDER BY local_date DESC, created_at DESC LIMIT 1
                """
            ).fetchone()
            latest_daily_write = connection.execute(
                "SELECT created_at FROM daily_metrics ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            latest_ui = connection.execute(
                """
                SELECT local_date FROM page_captures
                 ORDER BY local_date DESC, created_at DESC LIMIT 1
                """
            ).fetchone()
            latest_ui_write = connection.execute(
                "SELECT created_at FROM page_captures ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            profile = connection.execute(
                "SELECT timezone FROM profiles WHERE profile_key='default'"
            ).fetchone()
            source_count = int(
                connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            )
        stat = self.database_path.stat()
        return {
            "api_version": "v1",
            "app_version": __version__,
            "timezone": str(profile["timezone"]) if profile else DEFAULT_TIMEZONE,
            "earliest_data_date": earliest_daily["local_date"] if earliest_daily else None,
            "latest_data_date": latest_daily["local_date"] if latest_daily else None,
            "latest_ui_date": latest_ui["local_date"] if latest_ui else None,
            "latest_normalized_write": (
                latest_daily_write["created_at"] if latest_daily_write else None
            ),
            "latest_ui_write": (
                latest_ui_write["created_at"] if latest_ui_write else None
            ),
            "database_bytes": stat.st_size,
            "database_modified_at": stat.st_mtime,
            "source_count": source_count,
            "read_only": True,
        }

    def current_profile(self) -> dict[str, Any]:
        with self.connection() as connection:
            profile = connection.execute(
                """
                SELECT id, profile_key, display_name, timezone, created_at, updated_at
                  FROM profiles
                 WHERE profile_key='default'
                """
            ).fetchone()
            if profile is None:
                raise HTTPException(404, "默认健康档案不存在")
            sources = _rows(
                connection.execute(
                    """
                    SELECT ps.source_id, ps.display_name, ps.priority, ps.enabled,
                           s.vendor, s.app_package, s.app_version
                      FROM profile_sources ps
                      JOIN sources s ON s.id=ps.source_id
                     WHERE ps.profile_id=?
                     ORDER BY ps.priority DESC, ps.source_id
                    """,
                    (profile["id"],),
                )
            )
        result = dict(profile)
        result["sources"] = sources
        return result

    def sources(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            profile_id = self._profile_id(connection)
            sources = _rows(
                connection.execute(
                    """
                    SELECT s.id, s.vendor, s.app_package, s.app_version, s.timezone,
                           s.created_at, s.updated_at, ps.display_name, ps.priority,
                           ps.enabled
                      FROM sources s
                      LEFT JOIN profile_sources ps
                        ON ps.source_id=s.id AND ps.profile_id=?
                     ORDER BY COALESCE(ps.priority,0) DESC, s.id
                    """,
                    (profile_id,),
                )
            )
            for source in sources:
                source_id = int(source["id"])
                stored_metrics = _rows(
                    connection.execute(
                        """
                        SELECT canonical_metric, COUNT(*) AS day_count,
                               MIN(local_date) AS earliest_date,
                               MAX(local_date) AS latest_date,
                               SUM(CASE WHEN is_partial=1 THEN 1 ELSE 0 END) AS partial_days
                          FROM daily_metrics_current
                         WHERE source_id=?
                         GROUP BY canonical_metric
                         ORDER BY canonical_metric
                        """,
                        (source_id,),
                    )
                )
                metrics_by_storage = {
                    str(item["canonical_metric"]): item for item in stored_metrics
                }
                source["metrics"] = []
                for metric_key, definition in visible_metrics().items():
                    stored = metrics_by_storage.get(storage_metric(metric_key))
                    if stored is None:
                        continue
                    item = dict(stored)
                    item["canonical_metric"] = metric_key
                    item["display_name"] = definition["name"]
                    source["metrics"].append(item)
                source["coverage"] = _rows(
                    connection.execute(
                        """
                        SELECT status, COUNT(*) AS count
                          FROM coverage
                         WHERE source_id=?
                         GROUP BY status
                         ORDER BY status
                        """,
                        (source_id,),
                    )
                )
                device_rows = _rows(
                    connection.execute(
                        """
                        SELECT id, device_hash, metadata_json, created_at
                          FROM devices WHERE source_id=? ORDER BY created_at
                        """,
                        (source_id,),
                    )
                )
                for device in device_rows:
                    device["device_hash"] = str(device["device_hash"])[:12]
                    device["metadata"] = _json(device.pop("metadata_json"), {})
                source["devices"] = device_rows
        return sources

    def metrics(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            availability = {
                row["canonical_metric"]: dict(row)
                for row in connection.execute(
                    """
                    SELECT canonical_metric, COUNT(*) AS day_count,
                           MIN(local_date) AS earliest_date,
                           MAX(local_date) AS latest_date,
                           SUM(CASE WHEN is_partial=1 THEN 1 ELSE 0 END) AS partial_days
                      FROM daily_metrics_current
                     GROUP BY canonical_metric
                    """
                )
            }
            coverage = {
                row["canonical_metric"]: int(row["covered_days"])
                for row in connection.execute(
                    """
                    SELECT canonical_metric,
                           SUM(CASE WHEN status IN ('exact_source','exact_ui') THEN 1 ELSE 0 END)
                               AS covered_days
                      FROM coverage GROUP BY canonical_metric
                    """
                )
            }
        result = []
        for key, definition in visible_metrics().items():
            stored_key = storage_metric(key)
            item = dict(definition)
            item["availability"] = availability.get(
                stored_key,
                {
                    "canonical_metric": key,
                    "day_count": 0,
                    "earliest_date": None,
                    "latest_date": None,
                    "partial_days": 0,
                },
            )
            item["availability"] = dict(item["availability"])
            item["availability"]["canonical_metric"] = key
            item["availability"]["covered_days"] = coverage.get(stored_key, 0)
            result.append(item)
        return result

    @staticmethod
    def _source_clause(source: str) -> tuple[str, list[Any]]:
        if source == "unified":
            return "", []
        try:
            source_id = int(source)
        except ValueError as exc:
            raise HTTPException(400, "source 必须是 unified 或数字 source_id") from exc
        return " AND d.source_id=? ", [source_id]

    def _daily_candidates(
        self,
        connection: sqlite3.Connection,
        *,
        metrics: Iterable[str],
        date_from: str,
        date_to: str,
        source: str,
        include_chart: bool = False,
    ) -> list[dict[str, Any]]:
        metric_list = list(metrics)
        unknown = [metric for metric in metric_list if metric not in METRIC_CATALOG]
        if unknown:
            raise HTTPException(404, f"未知指标：{', '.join(unknown)}")
        public_by_storage: dict[str, list[str]] = defaultdict(list)
        for metric in metric_list:
            public_by_storage[storage_metric(metric)].append(metric)
        storage_metrics = list(public_by_storage)
        profile_id = self._profile_id(connection)
        source_clause, source_parameters = self._source_clause(source)
        placeholders = ",".join("?" for _ in storage_metrics)
        chart_expression = "d.chart_json" if include_chart else "NULL"
        query_template = """
            SELECT d.id, d.source_id, d.canonical_metric, d.local_date,
                   d.revision,
                   json_remove(d.summary_json, '$.vendor_summaries')
                       AS summary_json,
                   __CHART_EXPRESSION__ AS chart_json, d.fidelity,
                   d.is_partial, d.created_at, s.vendor, s.app_version,
                   COALESCE(ps.priority,0) AS source_priority,
                   COALESCE(ps.display_name, UPPER(s.vendor) || ' 健康') AS source_name
              FROM daily_metrics_current d
              JOIN sources s ON s.id=d.source_id
              LEFT JOIN profile_sources ps
                ON ps.source_id=d.source_id AND ps.profile_id=?
             WHERE d.canonical_metric IN (__METRIC_PLACEHOLDERS__)
               AND d.local_date BETWEEN ? AND ?
               AND (ps.enabled=1 OR ps.enabled IS NULL)
               __SOURCE_CLAUSE__
             ORDER BY d.local_date, d.canonical_metric,
                      COALESCE(ps.priority,0) DESC,
                      CASE d.fidelity
                        WHEN 'exact_source' THEN 5
                        WHEN 'exact_ui' THEN 4
                        WHEN 'visual_only' THEN 3
                        WHEN 'no_data' THEN 2
                        ELSE 1
                      END DESC,
                      d.revision DESC, d.source_id
        """
        # Replacements are fixed SQL tokens or generated '?' placeholders only.
        query = (  # nosec B608
            query_template.replace("__CHART_EXPRESSION__", chart_expression)
            .replace("__METRIC_PLACEHOLDERS__", placeholders)
            .replace("__SOURCE_CLAUSE__", source_clause)
        )
        stored_rows = _rows(
            connection.execute(
                query,
                [
                    profile_id,
                    *storage_metrics,
                    date_from,
                    date_to,
                    *source_parameters,
                ],
            )
        )
        rows: list[dict[str, Any]] = []
        for stored_row in stored_rows:
            stored_key = str(stored_row["canonical_metric"])
            summary = self._public_summary(
                _json(stored_row.get("summary_json"), {})
            )
            chart = (
                _json(
                    stored_row.get("chart_json"),
                    {"series": {}, "intervals": []},
                )
                if include_chart
                else {"series": {}, "intervals": []}
            )
            for public_key in public_by_storage.get(stored_key, []):
                row = dict(stored_row)
                row["storage_metric"] = stored_key
                row["canonical_metric"] = public_key
                row.pop("summary_json", None)
                row.pop("chart_json", None)
                row["summary"] = summary
                row["chart"] = chart
                row["partial"] = bool(row.pop("is_partial"))
                row["fidelity_rank"] = FIDELITY_RANK.get(
                    str(row["fidelity"]), 0
                )
                rows.append(row)
        return rows

    @staticmethod
    def _public_summary(summary: dict[str, Any]) -> dict[str, Any]:
        result = dict(summary)
        result.pop("vendor_summaries", None)
        return result

    @staticmethod
    def _selected(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        selected: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (str(row["canonical_metric"]), str(row["local_date"]))
            selected.setdefault(key, row)
        return sorted(
            selected.values(),
            key=lambda row: (str(row["local_date"]), str(row["canonical_metric"])),
        )

    @staticmethod
    def _compact_day(row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: row[key]
            for key in (
                "source_id",
                "source_name",
                "vendor",
                "canonical_metric",
                "local_date",
                "revision",
                "summary",
                "fidelity",
                "partial",
                "created_at",
            )
        }

    def overview(
        self,
        date_from: str,
        date_to: str,
        source: str,
        max_points: int = 720,
    ) -> dict[str, Any]:
        start, end = _date_range(date_from, date_to)
        with self.connection() as connection:
            rows = self._daily_candidates(
                connection,
                metrics=visible_metrics(),
                date_from=start,
                date_to=end,
                source=source,
            )
        selected = self._selected(rows)
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in selected:
            grouped[str(row["canonical_metric"])].append(self._compact_day(row))
        metrics: dict[str, Any] = {}
        for metric, definition in visible_metrics().items():
            days = grouped.get(metric, [])
            sampled_days, sampling = _sample_daily_points(
                days, definition, max_points
            )
            metrics[metric] = {
                "definition": definition,
                "days": [
                    _trend_day(day, definition) for day in sampled_days
                ],
                "latest": _trend_day(days[-1], definition) if days else None,
                "day_count": len(days),
                "statistics": _daily_statistics(days, definition),
                "sampling": sampling,
            }
        return {
            "from": start,
            "to": end,
            "source": source,
            "metrics": metrics,
        }

    def metric_daily(
        self,
        metric: str,
        date_from: str,
        date_to: str,
        source: str,
        max_points: int = 900,
    ) -> dict[str, Any]:
        start, end = _date_range(date_from, date_to)
        with self.connection() as connection:
            candidates = self._daily_candidates(
                connection,
                metrics=[metric],
                date_from=start,
                date_to=end,
                source=source,
            )
        selected = [self._compact_day(row) for row in self._selected(candidates)]
        definition = METRIC_CATALOG[metric]
        sampled, sampling = _sample_daily_points(
            selected, definition, max_points
        )
        return {
            "metric": definition,
            "from": start,
            "to": end,
            "source": source,
            "latest_date": selected[-1]["local_date"] if selected else None,
            "day_count": len(selected),
            "statistics": _daily_statistics(selected, definition),
            "sampling": sampling,
            "days": [_trend_day(day, definition) for day in sampled],
        }

    def calendar(self, month: str, metric: str, source: str) -> dict[str, Any]:
        if metric not in METRIC_CATALOG:
            raise HTTPException(404, f"未知指标：{metric}")
        stored_metric = storage_metric(metric)
        if not MONTH_PATTERN.fullmatch(month):
            raise HTTPException(400, "month 必须是 YYYY-MM")
        try:
            first = dt.date.fromisoformat(f"{month}-01")
        except ValueError as exc:
            raise HTTPException(400, "month 不是有效月份") from exc
        if first.month == 12:
            next_month = dt.date(first.year + 1, 1, 1)
        else:
            next_month = dt.date(first.year, first.month + 1, 1)
        last = next_month - dt.timedelta(days=1)
        start, end = first.isoformat(), last.isoformat()
        with self.connection() as connection:
            candidates = self._daily_candidates(
                connection,
                metrics=[metric],
                date_from=start,
                date_to=end,
                source=source,
            )
            selected = self._selected(candidates)
            if source == "unified":
                coverage_query = """
                    SELECT c.source_id, c.local_date, c.status, c.updated_at,
                           s.vendor
                      FROM coverage c
                      JOIN sources s ON s.id=c.source_id
                     WHERE c.canonical_metric=?
                       AND c.local_date BETWEEN ? AND ?
                     ORDER BY c.local_date,
                              CASE c.status
                                WHEN 'exact_source' THEN 5
                                WHEN 'exact_ui' THEN 4
                                WHEN 'visual_only' THEN 3
                                WHEN 'no_data' THEN 2
                                ELSE 1
                              END DESC
                """
                parameters: list[Any] = [stored_metric, start, end]
            else:
                try:
                    source_id = int(source)
                except ValueError as exc:
                    raise HTTPException(400, "source 参数无效") from exc
                coverage_query = """
                    SELECT c.source_id, c.local_date, c.status, c.updated_at,
                           s.vendor
                      FROM coverage c
                      JOIN sources s ON s.id=c.source_id
                     WHERE c.canonical_metric=?
                       AND c.local_date BETWEEN ? AND ?
                       AND c.source_id=?
                     ORDER BY c.local_date,
                              CASE c.status
                                WHEN 'exact_source' THEN 5
                                WHEN 'exact_ui' THEN 4
                                WHEN 'visual_only' THEN 3
                                WHEN 'no_data' THEN 2
                                ELSE 1
                              END DESC
                """
                parameters = [stored_metric, start, end, source_id]
            coverage = _rows(
                connection.execute(
                    coverage_query,
                    parameters,
                )
            )
        by_date: dict[str, dict[str, Any]] = {}
        for row in coverage:
            by_date.setdefault(
                str(row["local_date"]),
                {
                    "date": row["local_date"],
                    "status": row["status"],
                    "source_id": row["source_id"],
                    "vendor": row["vendor"],
                    "has_daily": False,
                    "partial": False,
                },
            )
        for row in selected:
            by_date[str(row["local_date"])] = {
                "date": row["local_date"],
                "status": row["fidelity"],
                "source_id": row["source_id"],
                "vendor": row["vendor"],
                "has_daily": True,
                "partial": row["partial"],
            }
        return {
            "month": month,
            "metric": metric,
            "source": source,
            "days": sorted(by_date.values(), key=lambda item: item["date"]),
        }

    def metric_day(self, metric: str, local_date: str, source: str) -> dict[str, Any]:
        day = _date(local_date, "date")
        if metric not in METRIC_CATALOG:
            raise HTTPException(404, f"未知指标：{metric}")
        stored_metric = storage_metric(metric)
        definition = METRIC_CATALOG[metric]
        with self.connection() as connection:
            candidates = self._daily_candidates(
                connection,
                metrics=[metric],
                date_from=day,
                date_to=day,
                source=source,
                include_chart=True,
            )
            selected_rows = self._selected(candidates)
            selected = selected_rows[0] if selected_rows else None
            if selected is None:
                if source == "unified":
                    coverage_query = """
                        SELECT c.source_id, c.status, c.detail_json, c.updated_at,
                               s.vendor
                          FROM coverage c JOIN sources s ON s.id=c.source_id
                         WHERE c.canonical_metric=? AND c.local_date=?
                         ORDER BY CASE c.status
                           WHEN 'exact_source' THEN 5 WHEN 'exact_ui' THEN 4
                           WHEN 'visual_only' THEN 3 WHEN 'no_data' THEN 2 ELSE 1 END DESC
                    """
                    coverage_parameters: list[Any] = [stored_metric, day]
                else:
                    try:
                        source_id = int(source)
                    except ValueError as exc:
                        raise HTTPException(400, "source 参数无效") from exc
                    coverage_query = """
                        SELECT c.source_id, c.status, c.detail_json, c.updated_at,
                               s.vendor
                          FROM coverage c JOIN sources s ON s.id=c.source_id
                         WHERE c.canonical_metric=? AND c.local_date=?
                           AND c.source_id=?
                         ORDER BY CASE c.status
                           WHEN 'exact_source' THEN 5 WHEN 'exact_ui' THEN 4
                           WHEN 'visual_only' THEN 3 WHEN 'no_data' THEN 2 ELSE 1 END DESC
                    """
                    coverage_parameters = [stored_metric, day, source_id]
                coverage = _rows(
                    connection.execute(
                        coverage_query,
                        coverage_parameters,
                    )
                )
                for item in coverage:
                    item["detail"] = _json(item.pop("detail_json"), {})
                return {
                    "metric": METRIC_CATALOG[metric],
                    "date": day,
                    "source": source,
                    "data": None,
                    "candidates": [],
                    "coverage": coverage,
                    "completeness": {
                        "summary": False,
                        "series": False,
                        "intervals": False,
                        "evidence": False,
                        "issues": ["该日期没有规范化指标记录"],
                    },
                }
            source_id = int(selected["source_id"])
            sample_rows = _rows(
                connection.execute(
                    """
                    SELECT s.sample_kind, s.timestamp_utc_ms, s.value_num,
                           s.value_text, s.unit, s.original_value,
                           s.original_unit, s.quality_json
                      FROM metric_samples s
                      JOIN metric_records r ON r.id=s.metric_record_id
                     WHERE r.source_id=? AND r.canonical_metric=? AND r.local_date=?
                     ORDER BY s.sample_kind, s.timestamp_utc_ms
                    """,
                    (source_id, stored_metric, day),
                )
            )
            allowed_series = set(definition.get("series_kinds", []))
            series: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for sample in sample_rows:
                if str(sample["sample_kind"]) not in allowed_series:
                    continue
                series[str(sample["sample_kind"])].append(
                    {
                        "timestamp_utc_ms": sample["timestamp_utc_ms"],
                        "value": sample["value_num"]
                        if sample["value_num"] is not None
                        else sample["value_text"],
                        "unit": sample["unit"],
                        "original_value": sample["original_value"],
                        "original_unit": sample["original_unit"],
                        "quality": _json(sample["quality_json"], {}),
                    }
                )
            interval_rows = _rows(
                connection.execute(
                    """
                    SELECT i.interval_kind, i.start_utc_ms, i.end_utc_ms,
                           i.state, i.value_num, i.unit, i.metadata_json
                      FROM metric_intervals i
                      JOIN metric_records r ON r.id=i.metric_record_id
                     WHERE r.source_id=? AND r.canonical_metric=? AND r.local_date=?
                     ORDER BY i.start_utc_ms, i.end_utc_ms
                    """,
                    (source_id, stored_metric, day),
                )
            )
            intervals = [
                {
                    **{
                        key: value
                        for key, value in interval.items()
                        if key not in {"metadata_json", "state"}
                    },
                    "state": (
                        OPPO_SLEEP_STAGE_STATES.get(
                            str(interval["state"]), str(interval["state"])
                        )
                        if selected["vendor"] == "oppo"
                        and interval["state"] is not None
                        else interval["state"]
                    ),
                    "original_state": interval["state"],
                    "metadata": _json(interval["metadata_json"], {}),
                }
                for interval in interval_rows
            ]
            if not series:
                chart_series = selected.get("chart", {}).get("series", {})
                if isinstance(chart_series, dict):
                    for kind, points in chart_series.items():
                        if str(kind) not in allowed_series:
                            continue
                        normalized_points = []
                        for point in points if isinstance(points, list) else []:
                            item = dict(point)
                            if (
                                "timestamp_utc_ms" not in item
                                and "start_utc_ms" in item
                            ):
                                item["timestamp_utc_ms"] = item["start_utc_ms"]
                            normalized_points.append(item)
                        series[str(kind)] = normalized_points
            if not intervals:
                chart_intervals = selected.get("chart", {}).get("intervals", [])
                if isinstance(chart_intervals, list):
                    intervals = chart_intervals
            evidence = self._evidence(
                connection, source_id, stored_metric, day
            )
        expected_series = bool(definition.get("series_kinds"))
        expected_intervals = bool(definition.get("interval_kinds"))
        issues: list[str] = []
        if expected_series and not any(series.values()):
            issues.append("该日只有汇总，原始日内采样序列缺失")
        if expected_intervals and not intervals:
            issues.append("该日缺少可复现的阶段区间")
        if not evidence:
            issues.append("该日没有关联页面证据")
        if selected["fidelity"] in {"visual_only", "failed"}:
            issues.append(f"数据保真状态为 {selected['fidelity']}")
        data = self._compact_day(selected)
        data["series"] = dict(series)
        data["intervals"] = intervals
        data["evidence"] = evidence
        candidates_payload = [
            {
                "source_id": row["source_id"],
                "source_name": row["source_name"],
                "vendor": row["vendor"],
                "fidelity": row["fidelity"],
                "revision": row["revision"],
                "priority": row["source_priority"],
                "selected": row["source_id"] == selected["source_id"],
                "selection_reason": (
                    "来源优先级、保真等级与最新版本综合排序"
                    if row["source_id"] == selected["source_id"]
                    else None
                ),
            }
            for row in candidates
        ]
        return {
            "metric": METRIC_CATALOG[metric],
            "date": day,
            "source": source,
            "data": data,
            "candidates": candidates_payload,
            "coverage": [],
            "completeness": {
                "summary": bool(data["summary"]),
                "series": any(series.values()),
                "intervals": bool(intervals),
                "evidence": bool(evidence),
                "issues": issues,
            },
        }

    @staticmethod
    def _evidence(
        connection: sqlite3.Connection, source_id: int, metric: str, day: str
    ) -> list[dict[str, Any]]:
        return _rows(
            connection.execute(
                """
                SELECT pc.id AS page_capture_id, pc.page_key, pc.fidelity,
                       pc.created_at, el.role, el.viewport_index,
                       el.artifact_sha256 AS sha256, a.media_type,
                       a.byte_size, a.width, a.height
                  FROM page_captures pc
                  JOIN evidence_links el ON el.page_capture_id=pc.id
                  JOIN artifacts a ON a.sha256=el.artifact_sha256
                 WHERE pc.source_id=? AND pc.canonical_metric=? AND pc.local_date=?
                 ORDER BY pc.id, el.role, el.viewport_index
                 LIMIT 300
                """,
                (source_id, metric, day),
            )
        )

    def artifact(self, sha256: str) -> tuple[Path, str]:
        if not SHA256_PATTERN.fullmatch(sha256):
            raise HTTPException(404, "素材不存在")
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT DISTINCT a.relative_path, a.media_type
                  FROM artifacts a
                  JOIN evidence_links el ON el.artifact_sha256=a.sha256
                 WHERE a.sha256=?
                """,
                (sha256,),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "素材不存在或不是页面证据")
        path = (self.data_dir / str(row["relative_path"])).resolve()
        try:
            path.relative_to(self.data_dir)
        except ValueError as exc:
            raise HTTPException(404, "素材路径无效") from exc
        if not path.is_file():
            raise HTTPException(404, "素材文件缺失")
        return path, str(row["media_type"])

    def quality(self) -> dict[str, Any]:
        with self.connection() as connection:
            coverage = _rows(
                connection.execute(
                    """
                    SELECT canonical_metric, status, COUNT(*) AS count
                      FROM coverage
                     GROUP BY canonical_metric, status
                     ORDER BY canonical_metric, status
                    """
                )
            )
            failures = _rows(
                connection.execute(
                    """
                    SELECT canonical_metric, stage, error_type, COUNT(*) AS count,
                           MIN(local_date) AS earliest_date,
                           MAX(local_date) AS latest_date
                      FROM failures
                     WHERE resolved_at IS NULL
                     GROUP BY canonical_metric, stage, error_type
                     ORDER BY count DESC
                    """
                )
            )
            sequences = _rows(
                connection.execute(
                    """
                    SELECT r.canonical_metric, s.sample_kind,
                           COUNT(*) AS sample_count,
                           COUNT(DISTINCT r.local_date) AS day_count
                      FROM metric_samples s
                      JOIN metric_records r ON r.id=s.metric_record_id
                     GROUP BY r.canonical_metric, s.sample_kind
                     ORDER BY r.canonical_metric, s.sample_kind
                    """
                )
            )
            intervals = _rows(
                connection.execute(
                    """
                    SELECT r.canonical_metric, i.interval_kind,
                           COUNT(*) AS interval_count,
                           COUNT(DISTINCT r.local_date) AS day_count
                      FROM metric_intervals i
                      JOIN metric_records r ON r.id=i.metric_record_id
                     GROUP BY r.canonical_metric, i.interval_kind
                    """
                )
            )
            totals = dict(
                connection.execute(
                    """
                    SELECT
                      (SELECT COUNT(*) FROM failures WHERE resolved_at IS NULL)
                        AS unresolved_failures,
                      (SELECT COUNT(*) FROM page_captures) AS page_captures,
                      (SELECT COUNT(*) FROM artifacts) AS artifacts,
                      (SELECT COUNT(*) FROM metric_samples) AS samples,
                      (SELECT COUNT(*) FROM metric_intervals) AS intervals
                    """
                ).fetchone()
            )
        expanded_coverage: list[dict[str, Any]] = []
        for row in coverage:
            for metric_key in visible_metrics():
                if storage_metric(metric_key) != row["canonical_metric"]:
                    continue
                item = dict(row)
                item["canonical_metric"] = metric_key
                expanded_coverage.append(item)
        expanded_sequences: list[dict[str, Any]] = []
        for row in sequences:
            for metric_key, definition in visible_metrics().items():
                if (
                    storage_metric(metric_key) == row["canonical_metric"]
                    and row["sample_kind"] in definition.get("series_kinds", [])
                ):
                    item = dict(row)
                    item["canonical_metric"] = metric_key
                    expanded_sequences.append(item)
        expanded_intervals: list[dict[str, Any]] = []
        for row in intervals:
            for metric_key, definition in visible_metrics().items():
                if (
                    storage_metric(metric_key) == row["canonical_metric"]
                    and row["interval_kind"]
                    in definition.get("interval_kinds", [])
                ):
                    item = dict(row)
                    item["canonical_metric"] = metric_key
                    expanded_intervals.append(item)
        report_summary = None
        report_dir = self.data_dir / "reports"
        if report_dir.is_dir():
            reports = sorted(report_dir.glob("verification-*.json"), reverse=True)
            if reports:
                try:
                    report = json.loads(reports[0].read_text(encoding="utf-8"))
                    report_summary = {
                        "name": reports[0].name,
                        "passed": report.get("passed"),
                        "generated_at": report.get("generated_at"),
                        "failure_count": len(report.get("failures", [])),
                        "warning_count": len(report.get("warnings", [])),
                    }
                except (OSError, json.JSONDecodeError):
                    report_summary = {"name": reports[0].name, "passed": None}
        return {
            "coverage": expanded_coverage,
            "failures": failures,
            "sequences": expanded_sequences,
            "intervals_by_kind": expanded_intervals,
            "totals": totals,
            "latest_verification": report_summary,
        }


def create_web_app(
    workspace: Path,
    data_dir: Path,
    *,
    serial: str = DEFAULT_SERIAL,
    auth: WebAuth | None = None,
    lan_access: bool = False,
) -> FastAPI:
    workspace = workspace.resolve()
    data_dir = data_dir.resolve()
    service = WebDataService(workspace, data_dir)
    dashboard = DashboardState(workspace, data_dir, serial)
    auth = auth or WebAuth(data_dir / "private" / "web-auth.json", enabled=False)
    app = FastAPI(
        title="健康数据中枢 API",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    def add_security_headers(response: Response, path: str) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
            "form-action 'self'",
        )
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.middleware("http")
    async def protect_private_api(request: Request, call_next: Any) -> Response:
        path = request.url.path
        if not _host_allowed(request.headers.get("host", ""), lan_access=lan_access):
            return add_security_headers(
                JSONResponse({"detail": "Host 不允许"}, status_code=400), path
            )
        public = {
            "/api/v1/auth/status",
            "/api/v1/auth/login",
            "/api/openapi.json",
        }
        if path.startswith("/api/") and path not in public and not auth.authenticated(request):
            return add_security_headers(
                JSONResponse({"detail": "需要登录"}, status_code=401), path
            )
        response = await call_next(request)
        return add_security_headers(response, path)

    @app.get("/api/v1/auth/status")
    def auth_status(request: Request) -> dict[str, Any]:
        return {
            "required": auth.enabled,
            "authenticated": auth.authenticated(request),
        }

    @app.post("/api/v1/auth/login")
    async def login(request: Request) -> Response:
        client_key = request.client.host if request.client else "unknown"
        retry_after = auth.retry_after(client_key)
        if retry_after:
            return JSONResponse(
                {"detail": "登录尝试过多，请稍后重试"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > LOGIN_BODY_MAX_BYTES:
                raise HTTPException(413, "登录请求过大")
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "登录请求不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "登录请求必须是 JSON 对象")
        if not auth.verify_password(str(payload.get("password", "")), client_key):
            raise HTTPException(401, "密码错误")
        token = auth.create_session()
        response = JSONResponse({"authenticated": True})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=SESSION_SECONDS,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return response

    @app.post("/api/v1/auth/logout")
    def logout(request: Request) -> Response:
        auth.revoke(request)
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/v1/meta")
    def meta() -> dict[str, Any]:
        return service.meta()

    @app.get("/api/v1/profiles/current")
    def current_profile() -> dict[str, Any]:
        return service.current_profile()

    @app.get("/api/v1/sources")
    def sources() -> list[dict[str, Any]]:
        return service.sources()

    @app.get("/api/v1/metrics")
    def metrics() -> list[dict[str, Any]]:
        return service.metrics()

    @app.get("/api/v1/overview")
    def overview(
        date_from: str = Query(alias="from"),
        date_to: str = Query(alias="to"),
        source: str = "unified",
        max_points: int = Query(default=720, ge=3, le=2000),
    ) -> dict[str, Any]:
        return service.overview(date_from, date_to, source, max_points)

    @app.get("/api/v1/calendar")
    def calendar(
        month: str,
        metric: str,
        source: str = "unified",
    ) -> dict[str, Any]:
        return service.calendar(month, metric, source)

    @app.get("/api/v1/metrics/{metric}/daily")
    def metric_daily(
        metric: str,
        date_from: str = Query(alias="from"),
        date_to: str = Query(alias="to"),
        source: str = "unified",
        max_points: int = Query(default=900, ge=3, le=2000),
    ) -> dict[str, Any]:
        return service.metric_daily(
            metric, date_from, date_to, source, max_points
        )

    @app.get("/api/v1/metrics/{metric}/days/{local_date}")
    def metric_day(
        metric: str,
        local_date: str,
        source: str = "unified",
    ) -> dict[str, Any]:
        return service.metric_day(metric, local_date, source)

    @app.get("/api/v1/artifacts/{sha256}")
    def artifact(sha256: str) -> FileResponse:
        path, media_type = service.artifact(sha256)
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @app.get("/api/v1/admin/status")
    def admin_status() -> dict[str, Any]:
        return dashboard.snapshot()

    @app.get("/api/v1/admin/events")
    def admin_events(limit: int = 300) -> dict[str, Any]:
        return dashboard.read_events(max(20, min(limit, 2000)))

    @app.get("/api/v1/admin/log")
    def admin_log(name: str, lines: int = 500) -> dict[str, Any]:
        try:
            return dashboard.read_log(name, lines)
        except FileNotFoundError as exc:
            raise HTTPException(404, "日志不存在") from exc

    @app.get("/api/v1/admin/calendar-image")
    def admin_calendar_image(name: str) -> FileResponse:
        try:
            return FileResponse(dashboard.calendar_image(name), media_type="image/png")
        except FileNotFoundError as exc:
            raise HTTPException(404, "日历验证截图不存在") from exc

    @app.get("/api/v1/admin/quality")
    def admin_quality() -> dict[str, Any]:
        return service.quality()

    # Legacy read-only API compatibility.
    @app.get("/api/status")
    def legacy_status() -> dict[str, Any]:
        return dashboard.snapshot()

    @app.get("/api/events")
    def legacy_events(limit: int = 300) -> dict[str, Any]:
        return dashboard.read_events(max(20, min(limit, 2000)))

    @app.get("/api/log")
    def legacy_log(name: str, lines: int = 500) -> dict[str, Any]:
        try:
            return dashboard.read_log(name, lines)
        except FileNotFoundError as exc:
            raise HTTPException(404, "日志不存在") from exc

    @app.get("/api/calendar-image")
    def legacy_calendar_image(name: str) -> FileResponse:
        try:
            return FileResponse(dashboard.calendar_image(name), media_type="image/png")
        except FileNotFoundError as exc:
            raise HTTPException(404, "日历验证截图不存在") from exc

    frontend_dir = Path(__file__).with_name("web_dist")
    if frontend_dir.is_dir() and (frontend_dir / "index.html").is_file():
        assets_dir = frontend_dir / "assets"
        if assets_dir.is_dir():
            app.mount(
                "/assets",
                StaticFiles(directory=assets_dir),
                name="frontend-assets",
            )

        @app.get("/{frontend_path:path}", include_in_schema=False)
        def frontend(frontend_path: str, request: Request) -> Response:
            if frontend_path.startswith("api/"):
                raise HTTPException(404, "接口不存在")
            candidate = (frontend_dir / frontend_path).resolve()
            try:
                candidate.relative_to(frontend_dir)
            except ValueError:
                candidate = frontend_dir / "index.html"
            if candidate.is_file() and candidate.name != "index.html":
                return FileResponse(candidate)
            html = (frontend_dir / "index.html").read_text(encoding="utf-8")
            origin = str(request.base_url).rstrip("/")
            return HTMLResponse(
                html.replace("__HEALTH_ARCHIVE_ORIGIN__", origin),
                headers={"Cache-Control": "no-store"},
            )
    else:
        @app.get("/")
        def missing_frontend() -> JSONResponse:
            return JSONResponse(
                {
                    "detail": "前端尚未构建，请先在 webapp 目录执行 npm run build。"
                },
                status_code=503,
            )
    return app


def serve_web(args: Any, *, initial_path: str = "/") -> int:
    workspace = Path.cwd()
    data_dir = Path(args.data_dir)
    database_path = data_dir / "health.sqlite3"
    if not database_path.is_file():
        raise ExporterError(f"健康数据库不存在：{database_path}")
    with Database(database_path, data_dir / "private"):
        pass

    lan = bool(getattr(args, "lan", False))
    # Binding all interfaces requires the explicit --lan flag and password auth.
    host = "0.0.0.0" if lan else str(  # nosec B104
        getattr(args, "host", "127.0.0.1")
    )
    if host not in {"127.0.0.1", "localhost", "::1"} and not lan:
        raise ExporterError("非本机监听必须显式添加 --lan。")
    auth = WebAuth(data_dir / "private" / "web-auth.json", enabled=lan)
    auth.configure_interactively(force=bool(getattr(args, "reset_password", False)))
    app = create_web_app(
        workspace,
        data_dir,
        serial=args.serial,
        auth=auth,
        lan_access=lan,
    )
    port = int(args.port)
    local_url = f"http://127.0.0.1:{port}{initial_path}"
    print(f"健康数据中枢：{local_url}", flush=True)
    if lan:
        print(
            f"局域网模式已启用，请使用本机局域网地址的 {port} 端口访问并登录。",
            flush=True,
        )
    else:
        print("仅监听本机；按 Ctrl+C 关闭。", flush=True)
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(local_url)).start()
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    return 0
