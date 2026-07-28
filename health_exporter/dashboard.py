from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import sqlite3
import subprocess
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .adb import AdbClient, find_adb
from .constants import DEFAULT_SERIAL, PACKAGE
from .operation_log import EVENT_LOG_NAME, read_event_tail


METRIC_NAMES = {
    "activity": "每日活动",
    "heart_rate": "心率",
    "sleep": "睡眠",
    "spo2": "血氧",
    "blood_pressure": "血压",
    "weight": "体重",
    "stress": "压力",
}


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _xml_value(path: Path, resource_name: str) -> str | None:
    if not path.exists():
        return None
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    for node in root.iter("node"):
        if node.attrib.get("resource-id", "").split(":id/")[-1] == resource_name:
            return node.attrib.get("text") or node.attrib.get("content-desc") or None
    return None


@dataclass
class ProbeCache:
    value: dict[str, Any] | None = None
    observed_at: float = 0.0


class DashboardState:
    def __init__(self, workspace: Path, data_dir: Path, serial: str = DEFAULT_SERIAL):
        self.workspace = workspace.resolve()
        self.data_dir = data_dir.resolve()
        self.serial = serial
        self.database_path = self.data_dir / "health.sqlite3"
        self.log_dir = self.data_dir / "logs"
        self.operation_log_path = self.log_dir / EVENT_LOG_NAME
        self.proof_dir = self.workspace / ".diagnostics" / "calendar-proof"
        self._probe_cache = ProbeCache()
        self._probe_lock = threading.Lock()

    def connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.database_path.as_posix()}?mode=ro",
            uri=True,
            timeout=3,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def snapshot(self) -> dict[str, Any]:
        if not self.database_path.exists():
            return {
                "generated_at": time.time(),
                "error": f"数据库不存在：{self.database_path}",
                "system": self.system_probe(),
            }
        with self.connection() as connection:
            checkpoints = _rows(
                connection.execute(
                    """
                    SELECT c.source_id, c.canonical_metric, c.last_date,
                           c.state_json, c.updated_at
                      FROM checkpoints c
                     ORDER BY c.canonical_metric
                    """
                )
            )
            runs = _rows(
                connection.execute(
                    """
                    SELECT id, command, status, started_at, finished_at,
                           app_version, adb_serial, options_json, error
                      FROM ingest_runs
                     ORDER BY started_at DESC
                     LIMIT 30
                    """
                )
            )
            failures = _rows(
                connection.execute(
                    """
                    SELECT id, run_id, canonical_metric, local_date, stage,
                           error_type, message, evidence_json, created_at
                      FROM failures
                     WHERE resolved_at IS NULL
                     ORDER BY created_at DESC
                     LIMIT 100
                    """
                )
            )
            coverage_rows = _rows(
                connection.execute(
                    """
                    SELECT canonical_metric, status, COUNT(*) AS count
                      FROM coverage
                     GROUP BY canonical_metric, status
                     ORDER BY canonical_metric, status
                    """
                )
            )
            page_rows = _rows(
                connection.execute(
                    """
                    SELECT canonical_metric,
                           COUNT(*) AS page_count,
                           COUNT(DISTINCT local_date) AS date_count,
                           MIN(local_date) AS earliest_page_date,
                           MAX(local_date) AS latest_page_date,
                           MAX(created_at) AS last_capture_at
                      FROM page_captures
                     GROUP BY canonical_metric
                    """
                )
            )
            source_rows = _rows(
                connection.execute(
                    """
                    SELECT canonical_metric,
                           COUNT(*) AS record_count,
                           MIN(local_date) AS earliest_source_date,
                           MAX(local_date) AS latest_source_date
                      FROM metric_records
                     GROUP BY canonical_metric
                    """
                )
            )
            totals = dict(
                connection.execute(
                    """
                    SELECT
                      (SELECT COUNT(*) FROM artifacts) AS artifact_count,
                      (SELECT COALESCE(SUM(byte_size), 0) FROM artifacts) AS artifact_bytes,
                      (SELECT COUNT(*) FROM page_captures) AS page_capture_count,
                      (SELECT COUNT(*) FROM raw_snapshots) AS raw_snapshot_count,
                      (SELECT COUNT(*) FROM raw_record_versions) AS raw_version_count,
                      (SELECT COUNT(*) FROM metric_samples) AS sample_count,
                      (SELECT COUNT(*) FROM metric_intervals) AS interval_count
                    """
                ).fetchone()
            )

        checkpoint_by_metric = {row["canonical_metric"]: row for row in checkpoints}
        coverage_by_metric: dict[str, dict[str, int]] = {}
        for row in coverage_rows:
            coverage_by_metric.setdefault(row["canonical_metric"], {})[row["status"]] = int(
                row["count"]
            )
        pages_by_metric = {row["canonical_metric"]: row for row in page_rows}
        source_by_metric = {row["canonical_metric"]: row for row in source_rows}
        metric_keys = list(METRIC_NAMES)
        metrics = []
        for key in metric_keys:
            checkpoint = checkpoint_by_metric.get(key, {})
            metrics.append(
                {
                    "key": key,
                    "name": METRIC_NAMES[key],
                    "checkpoint": checkpoint.get("last_date"),
                    "checkpoint_updated_at": checkpoint.get("updated_at"),
                    "checkpoint_state": checkpoint.get("state_json"),
                    "coverage": coverage_by_metric.get(key, {}),
                    "pages": pages_by_metric.get(key, {}),
                    "source": source_by_metric.get(key, {}),
                }
            )

        system = self.system_probe()
        latest_run = runs[0] if runs else None
        collector_running = any(
            "ohealth_exporter.py" in str(item.get("command_line", ""))
            and " collect" in str(item.get("command_line", ""))
            for item in system.get("processes", [])
        )
        stop_marker = (self.data_dir / "STOP_UI_COLLECTION").exists()
        if stop_marker:
            phase = "paused"
        elif collector_running:
            phase = "collecting"
        elif latest_run and latest_run["status"] == "running":
            phase = "stale"
        else:
            phase = "idle"
        totals["database_bytes"] = self.database_path.stat().st_size
        wal = self.database_path.with_name(self.database_path.name + "-wal")
        totals["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
        return {
            "generated_at": time.time(),
            "phase": phase,
            "stop_marker": stop_marker,
            "latest_run": latest_run,
            "metrics": metrics,
            "runs": runs,
            "failures": failures,
            "totals": totals,
            "logs": self.list_logs(),
            "system": system,
            "calendar_proof": self.calendar_proof(),
        }

    def list_logs(self) -> list[dict[str, Any]]:
        if not self.log_dir.exists():
            return []
        result = []
        for path in sorted(
            (item for item in self.log_dir.iterdir() if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )[:100]:
            stat = path.stat()
            result.append(
                {
                    "name": path.name,
                    "bytes": stat.st_size,
                    "modified_at": stat.st_mtime,
                }
            )
        return result

    def read_log(self, name: str, lines: int) -> dict[str, Any]:
        safe_name = Path(name).name
        if safe_name != name:
            raise FileNotFoundError(name)
        path = (self.log_dir / safe_name).resolve()
        if path.parent != self.log_dir or not path.is_file():
            raise FileNotFoundError(name)
        payload = path.read_bytes()
        if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
            text, encoding = payload.decode("utf-16"), "utf-16"
        else:
            for candidate in ("utf-8-sig", "gb18030"):
                try:
                    text = payload.decode(candidate)
                    encoding = candidate
                    break
                except UnicodeDecodeError:
                    continue
            else:
                text, encoding = payload.decode("utf-8", errors="replace"), "utf-8-replace"
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        tail = deque(text.splitlines(keepends=True), maxlen=max(20, min(lines, 2000)))
        return {
            "name": safe_name,
            "text": "".join(tail),
            "encoding": encoding,
            "bytes": path.stat().st_size,
            "modified_at": path.stat().st_mtime,
        }

    def read_events(self, limit: int = 300) -> dict[str, Any]:
        return read_event_tail(self.operation_log_path, limit)

    def calendar_proof(self) -> dict[str, Any]:
        files = {
            "before": ("before.xml", "before.png"),
            "calendar_open": ("open.xml", "open.png"),
            "target_aligned": ("reopen.xml", "reopen.png"),
            "confirmed": ("one-tap-result.xml", "one-tap-result.png"),
        }
        stages: dict[str, Any] = {}
        for key, (xml_name, image_name) in files.items():
            xml_path = self.proof_dir / xml_name
            image_path = self.proof_dir / image_name
            stages[key] = {
                "date": _xml_value(xml_path, "tv_date"),
                "year": _xml_value(xml_path, "calendar_year_value"),
                "month": _xml_value(xml_path, "month_value"),
                "xml_sha256": _file_sha256(xml_path) if xml_path.exists() else None,
                "image_sha256": _file_sha256(image_path) if image_path.exists() else None,
                "image_url": f"/api/calendar-image?name={image_name}"
                if image_path.exists()
                else None,
            }
        confirmed = stages["confirmed"].get("date")
        return {
            "passed": confirmed == "2024年4月08日，周一",
            "target": "2024-04-08",
            "checkpoint_at_test": "2024-04-09",
            "stages": stages,
        }

    def calendar_image(self, name: str) -> Path:
        safe_name = Path(name).name
        if safe_name != name or not safe_name.lower().endswith(".png"):
            raise FileNotFoundError(name)
        path = (self.proof_dir / safe_name).resolve()
        if path.parent != self.proof_dir or not path.is_file():
            raise FileNotFoundError(name)
        return path

    def system_probe(self) -> dict[str, Any]:
        with self._probe_lock:
            now = time.monotonic()
            if self._probe_cache.value is not None and now - self._probe_cache.observed_at < 8:
                return self._probe_cache.value
            value: dict[str, Any] = {
                "adb": False,
                "serial": self.serial,
                "root": None,
                "foreground": None,
                "app_pid": None,
                "display": None,
                "processes": [],
                "errors": [],
            }
            try:
                adb = AdbClient(find_adb(), self.serial)
                adb.connect()
                value.update(
                    {
                        "adb": True,
                        "root": adb.root_available(),
                        "foreground": adb.foreground(),
                        "app_pid": adb.shell("pidof", PACKAGE, check=False) or None,
                        "display": f"{adb.shell('wm', 'size')} | {adb.shell('wm', 'density')}",
                    }
                )
            except Exception as exc:
                value["errors"].append(f"ADB: {exc}")
            try:
                script = r"""
$items = Get-CimInstance Win32_Process | Where-Object {
  $_.ProcessId -ne $PID -and $_.CommandLine -and (
    $_.CommandLine -match 'continue_after_heart\.ps1' -or
    ($_.CommandLine -match 'ohealth_exporter\.py' -and $_.CommandLine -match '(?i)(^|\s)collect(\s|$)')
  )
} | Select-Object ProcessId, Name, CommandLine
@($items) | ConvertTo-Json -Compress
"""
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                completed = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-Command", script],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=8,
                    creationflags=flags,
                    check=False,
                )
                if completed.returncode == 0 and completed.stdout.strip():
                    payload = json.loads(completed.stdout)
                    if isinstance(payload, dict):
                        payload = [payload]
                    value["processes"] = [
                        {
                            "pid": item.get("ProcessId"),
                            "name": item.get("Name"),
                            "command_line": item.get("CommandLine"),
                        }
                        for item in payload
                    ]
                elif completed.stderr.strip():
                    value["errors"].append(f"进程探测: {completed.stderr.strip()}")
            except Exception as exc:
                value["errors"].append(f"进程探测: {exc}")
            value["observed_at"] = time.time()
            self._probe_cache = ProbeCache(value, now)
            return value


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "OHealthDashboard/1.0"

    @property
    def state(self) -> DashboardState:
        return self.server.dashboard_state  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/status":
                self.send_json(self.state.snapshot())
                return
            if parsed.path == "/api/log":
                query = parse_qs(parsed.query)
                name = query.get("name", [""])[0]
                lines = int(query.get("lines", ["500"])[0])
                self.send_json(self.state.read_log(name, lines))
                return
            if parsed.path == "/api/events":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["300"])[0])
                self.send_json(self.state.read_events(limit))
                return
            if parsed.path == "/api/calendar-image":
                query = parse_qs(parsed.query)
                path = self.state.calendar_image(query.get("name", [""])[0])
                self.send_file(path)
                return
            if parsed.path in {"/", "/index.html"}:
                self.send_file(Path(__file__).with_name("web") / "index.html")
                return
            if parsed.path in {"/app.js", "/styles.css"}:
                self.send_file(Path(__file__).with_name("web") / parsed.path.lstrip("/"))
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except (FileNotFoundError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
        }:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve_dashboard(args: argparse.Namespace) -> int:
    from .webapp import serve_web

    return serve_web(args, initial_path="/admin/collector")
