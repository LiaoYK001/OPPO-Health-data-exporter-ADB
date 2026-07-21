from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import __version__
from .adb import AdbClient, find_adb
from .artifacts import ArtifactStore
from .constants import (
    DEFAULT_SERIAL,
    DEFAULT_TIMEZONE,
    EXPECTED_APP_VERSION,
    EXPECTED_DENSITY,
    EXPECTED_SIZE,
    PACKAGE,
    PRIMARY_METRICS,
    UI_PROFILES,
)
from .db import Database
from .errors import ExporterError, RootRequiredError, VersionMismatchError
from .exporting import DailyExporter
from .frida_bridge import capture_database_key
from .normalization import (
    repair_activity_normalization,
    repair_sleep_intervals,
    repair_weight_units,
)
from .ui import UiCollector, local_id, node_label
from .vendor.base import SnapshotFile, SnapshotResult
from .vendor.oppo import OppoAdapter
from .verification import Verifier


def json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


class Runtime:
    def __init__(self, args: argparse.Namespace, *, require_adb: bool):
        self.args = args
        self.data_dir = args.data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.data_dir / "health.sqlite3", self.data_dir / "private")
        self.artifacts = ArtifactStore(self.data_dir, self.db)
        self.adb: AdbClient | None = None
        self.app_version = "unknown"
        self.source_id: int | None = None
        if require_adb:
            self.adb = AdbClient(find_adb(args.adb), args.serial)
            self.adb.connect()
            self.app_version = self.adb.package_version(PACKAGE) or "unknown"
            if (
                self.app_version != EXPECTED_APP_VERSION
                and not args.allow_version_mismatch
            ):
                raise VersionMismatchError(
                    f"当前 OPPO 健康版本为 {self.app_version}，UI 适配器仅验收过 "
                    f"{EXPECTED_APP_VERSION}。如已人工复核，可加 --allow-version-mismatch。"
                )
            installation_hash = self.db.pseudonymize(f"adb:{args.serial}")
            self.source_id = self.db.ensure_source(
                vendor="oppo",
                app_package=PACKAGE,
                app_version=self.app_version,
                installation_hash=installation_hash,
                account_hash=f"unknown:{installation_hash[:16]}",
                timezone=DEFAULT_TIMEZONE,
            )

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def require_device(self) -> tuple[AdbClient, int]:
        if self.adb is None or self.source_id is None:
            raise ExporterError("该命令需要 MuMu ADB。")
        return self.adb, self.source_id

    def display_profile(self) -> str:
        adb, _ = self.require_device()
        return f"{adb.shell('wm', 'size')}|{adb.shell('wm', 'density')}"


def doctor(args: argparse.Namespace) -> int:
    adb = AdbClient(find_adb(args.adb), args.serial)
    adb.connect()
    devices = adb.run("devices", "-l", use_serial=False).stdout
    size = adb.shell("wm", "size")
    density = adb.shell("wm", "density")
    android = adb.shell("getprop", "ro.build.version.release")
    locale = adb.shell("getprop", "persist.sys.locale", check=False)
    if not locale:
        # Android 15 / MuMu 15 may leave persist.sys.locale empty while the
        # effective locale is exposed through the read-only product property.
        locale = adb.shell("getprop", "ro.product.locale", check=False)
    version = adb.package_version(PACKAGE)
    installed = PACKAGE in adb.shell("pm", "list", "packages", PACKAGE)
    foreground = adb.foreground()
    root = adb.root_available()
    disk_probe = args.data_dir.resolve()
    while not disk_probe.exists() and disk_probe != disk_probe.parent:
        disk_probe = disk_probe.parent
    free = shutil.disk_usage(disk_probe)
    phase = args.phase
    if phase == "auto":
        phase = "source" if root else "ui"
    checks = [
        ("ADB 设备", args.serial in devices, args.serial),
        ("Android", bool(android), android or "未知"),
        ("屏幕分辨率", EXPECTED_SIZE in size, size),
        ("显示密度", EXPECTED_DENSITY in density, density),
        ("系统语言", locale.lower().startswith("zh"), locale or "未知"),
        ("OPPO 健康", installed, "已安装" if installed else "未安装"),
        ("APP 版本", version == EXPECTED_APP_VERSION, version or "未知"),
        (
            "Root 阶段状态",
            root if phase == "source" else not root,
            "已开启（适合源快照，OPPO 健康会退出）"
            if root
            else "已关闭（适合 UI 页面采集）",
        ),
        ("前台应用", foreground.startswith(PACKAGE + "/"), foreground),
        ("磁盘空间", free.free >= 10 * 1024**3, f"剩余 {free.free / 1024**3:.1f} GiB"),
    ]
    phase_name = "Root 源快照" if phase == "source" else "非 Root UI 采集"
    print(f"OPPO 健康导出器 {__version__} 环境检查：{phase_name}")
    for name, ok, detail in checks:
        print(f"  {'[OK]' if ok else '[!!]'} {name}: {detail}")
    if root:
        print(
            "\n检测到 Root：请只执行 snapshot-source/key-capture；"
            "OPPO 健康出现风险提示并退出属于应用的 Root 检测。"
        )
    else:
        print("\nRoot 已关闭：可以执行 inventory 或 collect --ui-only。")
    print("正式全量运行前还需在 MuMu 应用中心关闭 OPPO 健康自动更新。")
    hard = [0, 2, 3, 5, 6, 7, 9]
    if phase == "ui":
        hard.append(8)
    return 0 if all(checks[index][1] for index in hard) else 2


def inventory(args: argparse.Namespace) -> int:
    with Runtime(args, require_adb=True) as runtime:
        adb, source_id = runtime.require_device()
        run_id = runtime.db.start_run(
            source_id,
            "inventory",
            runtime.app_version,
            adb.serial,
            {"max_scrolls": args.max_scrolls},
        )
        collector = UiCollector(
            adb,
            runtime.db,
            runtime.artifacts,
            source_id,
            run_id,
            runtime.app_version,
            runtime.display_profile(),
        )
        try:
            ui_inventory = collector.inventory_home(args.max_scrolls)
            source_inventory: dict[str, Any]
            if adb.root_available():
                source_inventory = OppoAdapter(
                    adb,
                    runtime.db,
                    runtime.artifacts,
                    runtime.data_dir,
                    runtime.app_version,
                ).discover()
            else:
                source_inventory = {
                    "root": False,
                    "blocked": "MuMu Root 尚未开启，无法枚举应用私有数据库。",
                }
            manifest = {
                "run_id": run_id,
                "app_version": runtime.app_version,
                "display_profile": runtime.display_profile(),
                "ui": ui_inventory,
                "source": source_inventory,
            }
            path = runtime.data_dir / "manifests" / f"inventory-{run_id}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            runtime.artifacts.put_file(path, media_type="application/json")
            for title in ui_inventory["unknown"]:
                runtime.db.record_failure(
                    run_id,
                    "inventory",
                    f"发现尚未映射的健康卡片：{title}",
                    metric=f"unmapped:{title}",
                )
            if not ui_inventory["unknown"]:
                runtime.db.resolve_failures(
                    source_id,
                    resolution=f"首页重新盘点通过：{run_id}",
                    stage="inventory",
                )
            runtime.db.finish_run(run_id, "complete" if not ui_inventory["unknown"] else "partial")
            manifest["manifest_path"] = str(path.resolve())
            json_print(manifest)
            return 0 if not ui_inventory["unknown"] else 2
        except Exception as exc:
            runtime.db.record_failure(run_id, "inventory", exc)
            runtime.db.finish_run(run_id, "failed", str(exc))
            raise


def snapshot_source(args: argparse.Namespace) -> int:
    with Runtime(args, require_adb=True) as runtime:
        adb, source_id = runtime.require_device()
        if not adb.root_available():
            raise RootRequiredError("snapshot-source 必须先开启 MuMu Root。")
        run_id = runtime.db.start_run(
            source_id,
            "snapshot-source",
            runtime.app_version,
            adb.serial,
            {"restart_app": args.restart_app},
        )
        adapter = OppoAdapter(
            adb,
            runtime.db,
            runtime.artifacts,
            runtime.data_dir,
            runtime.app_version,
            restart_app=args.restart_app,
        )
        try:
            result = adapter.snapshot(run_id)
            runtime.db.finish_run(run_id, "complete")
            json_print(
                {
                    "run_id": run_id,
                    "manifest_path": str(result.manifest_path.resolve()),
                    "files": len(result.files),
                    "sqlite_files": sum(item.sqlite_status == "sqlite" for item in result.files),
                }
            )
            return 0
        except Exception as exc:
            runtime.db.record_failure(run_id, "snapshot", exc)
            runtime.db.finish_run(run_id, "failed", str(exc))
            raise


def key_capture(args: argparse.Namespace) -> int:
    with Runtime(args, require_adb=True) as runtime:
        adb, source_id = runtime.require_device()
        run_id = runtime.db.start_run(
            source_id,
            "key-capture",
            runtime.app_version,
            adb.serial,
            {"timeout": args.timeout},
        )
        try:
            plaintext = runtime.data_dir / "raw" / "oppo" / "decrypted" / run_id / "database.db"
            result = capture_database_key(
                adb,
                runtime.data_dir / "private",
                plaintext,
                args.timeout,
                args.process,
            )
            artifact = runtime.artifacts.put_file(
                plaintext, media_type="application/vnd.sqlite3"
            )
            adapter = OppoAdapter(
                adb,
                runtime.db,
                runtime.artifacts,
                runtime.data_dir,
                runtime.app_version,
                restart_app=False,
            )
            sqlite_status, tables, schema_hash, row_count = adapter._inspect_file(plaintext)
            if sqlite_status != "sqlite":
                raise ExporterError(
                    f"SQLCipher 导出文件不是可读 SQLite：{sqlite_status}"
                )
            runtime.db.add_raw_snapshot(
                run_id=run_id,
                source_path="frida://sqlcipher_export/database.db",
                artifact_sha256=artifact.sha256,
                sqlite_status=sqlite_status,
                schema_hash=schema_hash,
                table_count=len(tables),
                row_count=row_count,
            )
            result["artifact_sha256"] = artifact.sha256
            manifest_path = runtime.data_dir / "manifests" / f"oppo-decrypted-{run_id}.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "source": "frida://sqlcipher_export/database.db",
                        "local_path": plaintext.relative_to(runtime.data_dir).as_posix(),
                        "sha256": artifact.sha256,
                        "tables": tables,
                        "schema_hash": schema_hash,
                        "target_row_count": row_count,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            runtime.artifacts.put_file(manifest_path, media_type="application/json")
            snapshot = SnapshotResult(
                run_id,
                (
                    SnapshotFile(
                        remote_path="frida://sqlcipher_export/database.db",
                        local_path=plaintext,
                        artifact_sha256=artifact.sha256,
                        sqlite_status="sqlite",
                        tables=tuple(tables),
                    ),
                ),
                manifest_path,
            )
            result["source_extract"] = adapter.extract(snapshot, source_id, run_id)
            result["manifest_path"] = str(manifest_path.resolve())
            result["coverage_reconcile"] = runtime.db.reconcile_no_data_coverage(
                source_id, run_id, PRIMARY_METRICS
            )
            runtime.db.resolve_failures(
                source_id,
                resolution=f"SQLCipher 导出成功：{run_id}",
                stage="key_capture",
            )
            runtime.db.finish_run(run_id, "complete")
            json_print({"run_id": run_id, **result})
            return 0
        except Exception as exc:
            runtime.db.record_failure(run_id, "key_capture", exc)
            runtime.db.finish_run(run_id, "failed", str(exc))
            raise


def _latest_snapshot(data_dir: Path) -> SnapshotResult:
    candidates = sorted((data_dir / "manifests").glob("oppo-snapshot-*.json"))
    if not candidates:
        raise ExporterError("没有可复用的 OPPO 快照；请移除 --skip-snapshot。")
    path = candidates[-1]
    manifest = json.loads(path.read_text(encoding="utf-8"))
    files = tuple(
        SnapshotFile(
            remote_path=item["remote_path"],
            local_path=data_dir / item["local_path"],
            artifact_sha256=item["sha256"],
            sqlite_status=item["sqlite_status"],
            tables=tuple(item.get("tables", [])),
        )
        for item in manifest["files"]
    )
    return SnapshotResult(str(manifest["run_id"]), files, path)


def _parse_metrics(value: str) -> list[str]:
    if value == "all":
        return list(PRIMARY_METRICS)
    result = [item.strip() for item in value.split(",") if item.strip()]
    unknown = sorted(set(result) - set(PRIMARY_METRICS))
    if unknown:
        raise ExporterError(f"未知指标：{', '.join(unknown)}")
    return result


def execute_collect(args: argparse.Namespace) -> dict[str, Any]:
    with Runtime(args, require_adb=True) as runtime:
        adb, source_id = runtime.require_device()
        if not args.ui_only and not adb.root_available():
            raise RootRequiredError("严格 collect 必须开启 Root；仅调试 UI 时可显式使用 --ui-only。")
        if adb.root_available() and not args.skip_ui and not args.ui_only:
            raise ExporterError(
                "当前 OPPO 健康会在 Root 环境下主动退出，不能在同一次运行中完成源快照和 UI 遍历。"
                "请先执行 snapshot-source；关闭 Root 并重启 MuMu 后，再执行 "
                "collect --ui-only --all-history --resume。"
            )
        options = {
            "all_history": args.all_history,
            "resume": args.resume,
            "max_days": args.max_days,
            "metrics": args.metrics,
            "skip_snapshot": args.skip_snapshot,
            "skip_ui": args.skip_ui,
            "ui_only": args.ui_only,
        }
        run_id = runtime.db.start_run(
            source_id,
            "collect",
            runtime.app_version,
            adb.serial,
            options,
        )
        adapter = OppoAdapter(
            adb,
            runtime.db,
            runtime.artifacts,
            runtime.data_dir,
            runtime.app_version,
        )
        result: dict[str, Any] = {"run_id": run_id, "source_id": source_id}
        try:
            if not args.ui_only:
                snapshot = _latest_snapshot(runtime.data_dir) if args.skip_snapshot else adapter.snapshot(run_id)
                result["snapshot_manifest"] = str(snapshot.manifest_path.resolve())
                result["source_extract"] = adapter.extract(snapshot, source_id, run_id)
                if (
                    not result["source_extract"]["inspected"]
                    and result["source_extract"]["encrypted_files"]
                ):
                    raise ExporterError(
                        "发现加密/不可读数据库且没有成功解析目标健康表；需要进入只读动态提取阶段。"
                    )
            collector = UiCollector(
                adb,
                runtime.db,
                runtime.artifacts,
                source_id,
                run_id,
                runtime.app_version,
                runtime.display_profile(),
            )
            home = collector.inventory_home(args.max_scrolls)
            result["home_inventory"] = home
            for title in home["unknown"]:
                runtime.db.record_failure(
                    run_id,
                    "inventory",
                    f"未映射健康卡片：{title}",
                    metric=f"unmapped:{title}",
                )
            if not args.skip_ui:
                ui_results = []
                for metric in _parse_metrics(args.metrics):
                    date_range = runtime.db.metric_date_range(source_id, metric)
                    profile = UI_PROFILES[metric]
                    home_card_present = any(
                        title in home["included"] for title in profile.home_titles
                    )
                    if date_range is None and not home_card_present:
                        runtime.db.set_coverage(
                            source_id,
                            metric,
                            dt.date.today().isoformat(),
                            "no_data",
                            {
                                "database": False,
                                "ui": False,
                                "home_card_present": False,
                                "basis": "source_empty_and_home_catalog_absent",
                            },
                            run_id,
                        )
                        runtime.db.connection.commit()
                        runtime.db.resolve_failures(
                            source_id,
                            metric=metric,
                            resolution=f"源表为空且首页目录无卡片：{run_id}",
                        )
                        ui_results.append(
                            {
                                "metric": metric,
                                "captured": 0,
                                "no_data": True,
                                "reason": "source_empty_and_home_catalog_absent",
                            }
                        )
                        continue
                    earliest = (
                        dt.date.fromisoformat(date_range[0])
                        if date_range
                        else dt.date.today()
                    )
                    try:
                        capture_result = collector.capture_history(
                                metric,
                                earliest_date=earliest,
                                max_days=args.max_days,
                                resume=args.resume,
                                capture_subpages=not args.no_subpages,
                                max_scrolls=args.max_scrolls,
                            )
                        ui_results.append(capture_result)
                        if capture_result["failures"] == 0 and capture_result["captured"] > 0:
                            runtime.db.resolve_failures(
                                source_id,
                                metric=metric,
                                resolution=f"UI 重新采集通过：{run_id}",
                            )
                    except Exception as exc:
                        runtime.db.record_failure(run_id, "metric_ui", exc, metric=metric)
                        runtime.db.set_coverage(
                            source_id,
                            metric,
                            dt.date.today().isoformat(),
                            "failed",
                            {"error": str(exc)},
                            run_id,
                        )
                        runtime.db.connection.commit()
                        ui_results.append({"metric": metric, "failed": str(exc)})
                result["ui"] = ui_results
            result["coverage_reconcile"] = runtime.db.reconcile_no_data_coverage(
                source_id, run_id, PRIMARY_METRICS
            )
            runtime.db.finish_run(run_id, "complete")
            return result
        except KeyboardInterrupt:
            runtime.db.finish_run(run_id, "interrupted", "KeyboardInterrupt")
            raise
        except Exception as exc:
            runtime.db.record_failure(run_id, "collect", exc)
            runtime.db.finish_run(run_id, "failed", str(exc))
            raise


def collect(args: argparse.Namespace) -> int:
    json_print(execute_collect(args))
    return 0


def capture_current(args: argparse.Namespace) -> int:
    with Runtime(args, require_adb=True) as runtime:
        adb, source_id = runtime.require_device()
        run_id = runtime.db.start_run(
            source_id,
            "capture",
            runtime.app_version,
            adb.serial,
            {"metric": args.metric, "page_key": args.page_key},
        )
        try:
            collector = UiCollector(
                adb,
                runtime.db,
                runtime.artifacts,
                source_id,
                run_id,
                runtime.app_version,
                runtime.display_profile(),
            )
            capture_id = collector.capture_page(
                args.metric,
                args.date or dt.date.today().isoformat(),
                args.page_key,
                args.max_scrolls,
            )
            runtime.db.finish_run(run_id, "complete")
            json_print({"run_id": run_id, "page_capture_id": capture_id})
            return 0
        except Exception as exc:
            runtime.db.record_failure(run_id, "capture", exc, metric=args.metric)
            runtime.db.finish_run(run_id, "failed", str(exc))
            raise


def probe_heart(args: argparse.Namespace) -> int:
    with Runtime(args, require_adb=True) as runtime:
        adb, source_id = runtime.require_device()
        run_id = runtime.db.start_run(source_id, "probe-heart", runtime.app_version, adb.serial, {})
        collector = UiCollector(
            adb,
            runtime.db,
            runtime.artifacts,
            source_id,
            run_id,
            runtime.app_version,
            runtime.display_profile(),
        )
        with tempfile.TemporaryDirectory(prefix="ohealth-heart-probe-") as temp:
            tree, _ = collector.dump_tree(Path(temp), "heart")
            values: dict[str, Any] = {}
            for node in tree.nodes:
                resource = local_id(node)
                label = node_label(node)
                if resource in {"tv_date", "tv_heart_rate_range", "tv_title", "tv_status"} and label:
                    values.setdefault(resource, []).append(label)
            if not any(value == "心率" for value in values.get("tv_title", [])):
                runtime.db.finish_run(run_id, "failed", "not heart page")
                raise ExporterError("当前不是心率详情页。")
            runtime.db.finish_run(run_id, "complete")
            json_print(values)
            return 0


def export_data(args: argparse.Namespace) -> int:
    with Runtime(args, require_adb=False) as runtime:
        formats = {"csv", "xlsx"} if args.format == "both" else {args.format}
        manifest = DailyExporter(runtime.db, runtime.data_dir).export(
            formats,
            source_id=args.source_id,
            output_dir=args.output_dir,
        )
        json_print(manifest)
        return 0


def repair_normalized(args: argparse.Namespace) -> int:
    with Runtime(args, require_adb=False) as runtime:
        source = (
            runtime.db.connection.execute(
                "SELECT * FROM sources WHERE id=?",
                (args.source_id,),
            ).fetchone()
            if args.source_id is not None
            else runtime.db.latest_source()
        )
        if source is None:
            raise ExporterError("没有可修复的本地数据源。")
        source_id = int(source["id"])
        run_id = runtime.db.start_run(
            source_id,
            "repair-normalized",
            str(source["app_version"]),
            "offline",
            {
                "repairs": [
                    "oppo_weight_grams_to_kg",
                    "oppo_activity_aggregate_and_subunits",
                    "oppo_sleep_consolidated_intervals",
                    "coverage_best_fidelity_from_evidence",
                ]
            },
        )
        try:
            result = repair_weight_units(runtime.db, source_id, run_id)
            result.update(repair_activity_normalization(runtime.db, source_id, run_id))
            result.update(repair_sleep_intervals(runtime.db, source_id, run_id))
            result.update(runtime.db.repair_coverage_statuses(source_id))
            runtime.db.finish_run(run_id, "complete")
            json_print({"run_id": run_id, "source_id": source_id, **result})
            return 0
        except Exception as exc:
            runtime.db.record_failure(
                run_id,
                "repair_normalized",
                exc,
                metric="weight",
            )
            runtime.db.finish_run(run_id, "failed", str(exc))
            raise


def verify_data(args: argparse.Namespace) -> int:
    with Runtime(args, require_adb=False) as runtime:
        report = Verifier(runtime.db, runtime.data_dir).verify(
            source_id=args.source_id,
            verify_all_artifacts=not args.fast,
        )
        json_print(report)
        return 0 if report["passed"] else 3


def run_all(args: argparse.Namespace) -> int:
    collect_result = execute_collect(args)
    with Runtime(args, require_adb=False) as runtime:
        report = Verifier(runtime.db, runtime.data_dir).verify(
            source_id=collect_result["source_id"]
        )
        export_manifest = DailyExporter(runtime.db, runtime.data_dir).export(
            {"csv", "xlsx"}, source_id=collect_result["source_id"]
        )
    json_print(
        {"collect": collect_result, "verification": report, "export": export_manifest}
    )
    return 0 if report["passed"] else 3


def _add_collect_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--all-history", action="store_true", default=True)
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--metrics", default="all", help="all 或逗号分隔指标")
    parser.add_argument("--skip-snapshot", action="store_true")
    parser.add_argument("--skip-ui", action="store_true")
    parser.add_argument("--ui-only", action="store_true")
    parser.add_argument("--no-subpages", action="store_true")
    parser.add_argument("--max-scrolls", type=int, default=12)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OPPO 健康 Root 数据库 + ADB 页面证据导出器"
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--adb", help="adb.exe 路径")
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--allow-version-mismatch", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor_parser = commands.add_parser("doctor", help="按源快照/UI 阶段检查运行环境")
    doctor_parser.add_argument(
        "--phase",
        choices=("auto", "source", "ui"),
        default="auto",
        help="auto 根据 Root 状态判断；source 要求 Root；ui 要求关闭 Root",
    )
    inventory_parser = commands.add_parser("inventory", help="盘点健康卡片和 Root 数据源")
    inventory_parser.add_argument("--max-scrolls", type=int, default=12)
    snapshot_parser = commands.add_parser("snapshot-source", help="建立不可变 Root 数据库快照")
    snapshot_parser.add_argument(
        "--restart-app",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="快照后重新启动 APP；Root 开启时默认关闭，避免触发风险退出",
    )
    key_parser = commands.add_parser("key-capture", help="短暂 Root 下只读截取 SQLCipher 密钥")
    key_parser.add_argument("--timeout", type=int, default=45)
    key_parser.add_argument(
        "--process",
        default=PACKAGE,
        help="仅附加到已存在的 APP 进程名，不会启动进程",
    )
    collect_parser = commands.add_parser("collect", help="采集并规范化全部历史")
    _add_collect_arguments(collect_parser)
    run_parser = commands.add_parser("run", help="采集、校验并导出 CSV/XLSX")
    _add_collect_arguments(run_parser)

    capture_parser = commands.add_parser("capture", help="保存当前页全部滚动证据")
    capture_parser.add_argument("--metric", default="diagnostic")
    capture_parser.add_argument("--page-key", default="current")
    capture_parser.add_argument("--date")
    capture_parser.add_argument("--max-scrolls", type=int, default=12)
    commands.add_parser("probe-heart", help="兼容命令：读取当前心率页文本")

    export_parser = commands.add_parser("export", help="从 SQLite 重建日级 CSV/XLSX")
    export_parser.add_argument("--format", choices=("csv", "xlsx", "both"), default="both")
    export_parser.add_argument("--source-id", type=int)
    export_parser.add_argument("--output-dir", type=Path)
    repair_parser = commands.add_parser(
        "repair-normalized",
        help="从不可变原始层审计并修正规范层单位/派生字段",
    )
    repair_parser.add_argument("--source-id", type=int)
    verify_parser = commands.add_parser("verify", help="校验素材哈希和指标日期覆盖")
    verify_parser.add_argument("--source-id", type=int)
    verify_parser.add_argument("--fast", action="store_true", help="跳过全量 SHA-256 复算")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    handlers = {
        "doctor": doctor,
        "inventory": inventory,
        "snapshot-source": snapshot_source,
        "key-capture": key_capture,
        "collect": collect,
        "run": run_all,
        "capture": capture_current,
        "probe-heart": probe_heart,
        "export": export_data,
        "repair-normalized": repair_normalized,
        "verify": verify_data,
    }
    try:
        return handlers[args.command](args)
    except (ExporterError, subprocess.TimeoutExpired) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断；checkpoint 和已提交数据均保留。", file=sys.stderr)
        return 130
